"""记忆管理器（`docs/02_ARCHITECTURE.md` §2.4 的 Memory Manager）。

写入与检索的**唯一入口**。Orchestrator 只调这里，不直接碰 ``MemoryStore``。

**四条不变量**：

1. **失败即降级**。embedding 不可达、存储出错都只记日志并返回空——LLM 已经成功，
   用户必须拿到回复。这个 ``try/except`` 是记忆子系统的唯一降级点，与
   ``Orchestrator`` 对 LLM 的处理同构（ADR 0014）。
2. **禁用时不检索也不写入**（`docs/06` §9）。开关存 ``app_flags``，运行时生效。
3. **命中 no-store 短语时本轮完全不写入**（`docs/06` §9）。这是短语匹配，
   不是语义理解——措辞变体会漏判，用户仍可用 ``DELETE /v1/memories/{id}`` 事后删。
4. **相似候选合并而非新建**（`docs/06` §6）：相似度超阈值就更新既有记忆的
   importance / confidence、累加 repetition、保留 provenance。

**打分与重排**：importance / confidence 由 ``memory_scoring`` 按 `docs/06` §3 算；
检索排序用 `docs/06` §4 的加权公式，五个权重全部来自 ``Settings``（文档明文要求
"不能硬编码在多个文件"）。

**为什么准入与排序分开**（M2 实测得出的结论）：

`docs/06` §4 的加权公式是**排序**用的——它把"是不是同一件事"（semantic）与
"重不重要 / 新不新"（importance / recency）揉成一个分。用它当**准入闸门**会出问题：
一条完全不相干的记忆，只要够重要够新，也能靠非语义项凑过阈值，被塞进 prompt。

实测（`text-embedding-nomic-embed-text-v1.5`，短中文文本）：

```text
相关对: 0.6378 – 0.8308  (mean 0.7064)
无关对: 0.6156 – 0.7439  (mean 0.6780)
```

两组**几乎完全重叠**——模型对短中文的余弦区分度很弱，没有哪个阈值能干净切开。
所以这里采取：

1. **准入**用原始余弦相似度硬闸门（``min_similarity``，默认 0.70）。
   宁可漏掉一条相关记忆，也不要把不相干的记忆塞进 prompt——**编造式的"想起来"
   比想不起来更伤**（`docs/01` §4 的诚实原则）。
2. **排序**才用 `docs/06` §4 的加权总分。

**已知局限**：重叠意味着这个闸门必然同时误杀与漏放。真正的解法是换一个中文
区分度更好的 embedding 模型——那需要新迁移（维度可能变化）与重建索引，
留待后续里程碑。README 的"已知限制"里记录了这个测量结果。
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from astra.application.memory_scoring import score_confidence, score_importance
from astra.domain.errors import AstraError
from astra.domain.ids import new_id
from astra.domain.memory import (
    Memory,
    MemoryCandidate,
    MemoryFilter,
    MemoryHit,
    MemoryType,
    expiry_for,
)
from astra.domain.ports import EmbeddingProvider, MemoryStore
from astra.domain.state import clamp_unit

_logger = logging.getLogger(__name__)

#: ``app_flags`` 里"记忆是否启用"的键。缺省时回落到 ``MemoryConfig.enabled``。
ENABLED_FLAG = "memory.enabled"

#: 用户**明确表示不要记住**的说法（`docs/06` §9）。
_NO_STORE_RE = re.compile(
    r"不要记住|别记住|不用记|不要记|别记|忘记这件事|忘掉这件事|不要记录|别记录|不用记录"
)

#: ``access_frequency`` 归一化：命中这么多次即拿满分。
_ACCESS_SATURATION = 5

#: ``recency`` 的半衰期（天）。越久没被创建的记忆衰减越多。
_RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    """`docs/06` §4 的五个权重。和应为 1.0（``Settings`` 里配置）。"""

    semantic: float = 0.50
    importance: float = 0.20
    recency: float = 0.15
    access: float = 0.05
    relationship: float = 0.10


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """``MemoryService`` 的策略参数（全部来自 ``Settings``）。"""

    user_id: str
    companion_id: str
    enabled: bool = True
    top_k: int = 5
    #: KNN 过采样数：先取这么多候选，重排序后再截断到 ``top_k``。
    candidate_limit: int = 20
    max_candidates_per_turn: int = 3
    dedup_threshold: float = 0.90
    #: **准入闸门**：原始余弦相似度低于它的记忆根本不进候选。
    #: 与下面的排序总分是两件事——见模块文档字符串「为什么准入与排序分开」。
    min_similarity: float = 0.70
    context_max_chars: int = 2000
    weights: RetrievalWeights = RetrievalWeights()


def detect_no_store(text: str) -> bool:
    """用户是否明确说了"不要记住这件事"（`docs/06` §9）。"""
    return _NO_STORE_RE.search(text) is not None


class MemoryService:
    """记忆的读写入口。"""

    def __init__(
        self,
        store: MemoryStore,
        embeddings: EmbeddingProvider | None,
        *,
        config: MemoryConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._config = config
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))

    # ── 检索 ────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        *,
        now: datetime | None = None,
        relationship_relevance: float = 0.0,
    ) -> tuple[MemoryHit, ...]:
        """检索与 ``query`` 相关的记忆，按 `docs/06` §4 的公式排序。

        ``relationship_relevance`` 是 `docs/06` §4 五项里的最后一项，由
        ``Orchestrator`` 从关系状态算出后传进来（M3 起有值；M2 时恒为 0）。
        这样 ``MemoryService`` 仍然不认识状态模块。

        任何失败都返回空元组（见模块文档字符串的不变量 1）。
        """
        if self._embeddings is None or not await self.is_enabled():
            return ()

        moment = now if now is not None else self._clock()

        try:
            vectors = await self._embeddings.embed([query])
        except AstraError as exc:
            self._log_failure("retrieve.embed", exc)
            return ()

        if not vectors:
            return ()

        try:
            nearby = await self._store.search_similar(
                vectors[0],
                user_id=self._config.user_id,
                companion_id=self._config.companion_id,
                now=moment,
                limit=self._config.candidate_limit,
            )
            memories = await self._store.get_many([memory_id for memory_id, _ in nearby])
        except (AstraError, ValueError) as exc:
            self._log_failure("retrieve.search", exc)
            return ()

        similarity_by_id = {memory_id: _similarity(distance) for memory_id, distance in nearby}

        # **准入与排序分开**：先按原始相似度筛掉"根本不是同一件事"的记忆，
        # 再用 docs/06 §4 的加权总分排序。理由见模块文档字符串。
        admitted = [
            memory
            for memory in memories
            if similarity_by_id.get(memory.id, 0.0) >= self._config.min_similarity
        ]
        if len(admitted) < len(memories):
            _logger.debug(
                "memory candidates rejected by the similarity gate",
                extra={
                    "event": "memory.retrieval.gated",
                    "count": len(memories) - len(admitted),
                },
            )

        scored = [
            self._score(
                memory,
                similarity=similarity_by_id.get(memory.id, 0.0),
                relationship=relationship_relevance,
                now=moment,
            )
            for memory in admitted
        ]
        ranked = tuple(
            sorted(
                scored,
                # 同分按 id 升序：prompt 必须逐字节确定（docs/10 §2）
                key=lambda hit: (-hit.score, hit.memory.id),
            )[: self._config.top_k]
        )

        if ranked:
            await self._touch([hit.memory.id for hit in ranked], moment)

        _logger.info(
            "memory retrieved",
            extra={
                "event": "memory.retrieved",
                "count": len(ranked),
                "detail": [hit.memory.id for hit in ranked],
            },
        )
        return ranked

    def _score(
        self, memory: Memory, *, similarity: float, relationship: float, now: datetime
    ) -> MemoryHit:
        weights = self._config.weights
        importance = memory.importance
        recency = _recency(memory.created_at, now)
        access = min(memory.access_count / _ACCESS_SATURATION, 1.0)

        components = {
            "semantic": similarity * weights.semantic,
            "importance": importance * weights.importance,
            "recency": recency * weights.recency,
            "access": access * weights.access,
            "relationship": clamp_unit(relationship) * weights.relationship,
        }

        return MemoryHit(memory=memory, score=sum(components.values()), components=components)

    async def _touch(self, memory_ids: Sequence[str], now: datetime) -> None:
        try:
            await self._store.touch_accessed(memory_ids, at=now)
        except AstraError as exc:
            # 访问统计失败不该让整次检索失败——它只影响后续排序
            self._log_failure("retrieve.touch", exc)

    # ── 写入 ────────────────────────────────────────────

    async def ingest_candidates(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        user_text: str,
        source_message_id: str | None = None,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Memory, ...]:
        """把一轮对话提取出的候选落库（去重 / 合并 / 打分 / 向量化）。

        **向量化是一次批量请求**（全部候选一起送），不是每条一次——第三方
        embedding 按请求计价，一轮最多 3 条候选就是最多 3 次计费。

        返回本次**新建**的记忆（合并到既有的不算），供 Orchestrator 计数与发布事件。
        """
        if self._embeddings is None or not candidates:
            return ()

        if not await self.is_enabled():
            return ()

        if detect_no_store(user_text):
            # docs/06 §9：显式 no-store。记日志是刻意的——用户要能验证它生效了
            _logger.info(
                "memory store suppressed by user request",
                extra={"event": "memory.suppressed", "count": len(candidates)},
            )
            return ()

        moment = now if now is not None else self._clock()
        selected = tuple(candidates)[: self._config.max_candidates_per_turn]
        if not selected:
            return ()

        if len(selected) < len(candidates):
            _logger.info(
                "truncated memory candidates",
                extra={
                    "event": "memory.candidates.truncated",
                    "count": len(candidates),
                    "detail": self._config.max_candidates_per_turn,
                },
            )

        # **一次请求向量化全部候选**（原先每条一次）。这不只是省往返：第三方
        # embedding 按请求计价，一轮最多 3 条候选意味着最多 3 次计费。
        try:
            vectors = await self._embeddings.embed([candidate.content for candidate in selected])
        except AstraError as exc:
            self._log_failure("ingest.embed", exc)
            return ()

        if len(vectors) != len(selected):
            # 数量对不上就无法把向量与候选对应起来——静默错位会写错记忆
            _logger.warning(
                "embedding provider returned a mismatched number of vectors",
                extra={
                    "event": "memory.failed",
                    "detail": {
                        "stage": "ingest.embed",
                        "expected": len(selected),
                        "got": len(vectors),
                    },
                },
            )
            return ()

        created: list[Memory] = []
        for candidate, vector in zip(selected, vectors, strict=True):
            try:
                stored = await self._ingest_one(
                    candidate,
                    vector,
                    user_text=user_text,
                    source_message_id=source_message_id,
                    correlation_id=correlation_id,
                    now=moment,
                )
            except (AstraError, ValueError) as exc:
                # 单条候选失败不影响其余候选，更不影响 turn
                self._log_failure("ingest.candidate", exc)
                continue
            if stored is not None:
                created.append(stored)

        return tuple(created)

    async def _ingest_one(
        self,
        candidate: MemoryCandidate,
        vector: Sequence[float],
        *,
        user_text: str,
        source_message_id: str | None,
        correlation_id: str | None,
        now: datetime,
    ) -> Memory | None:
        """落库**一条**候选。向量由调用方批量算好传进来（见 :meth:`ingest_candidates`）。"""
        existing = await self._find_duplicate(vector, now)
        if existing is not None:
            await self._merge(existing, candidate, vector=vector, now=now)
            return None

        memory = Memory(
            id=new_id(),
            user_id=self._config.user_id,
            companion_id=self._config.companion_id,
            type=candidate.type,
            content=candidate.content,
            created_at=now,
            importance=score_importance(candidate, user_text=user_text),
            confidence=score_confidence(candidate, user_text=user_text),
            source_message_id=source_message_id,
            expires_at=expiry_for(candidate.type, now),
            metadata={"correlation_id": correlation_id} if correlation_id else {},
        )
        await self._store.add(memory, vector)

        _logger.info(
            "memory created",
            extra={
                "event": "memory.created",
                "memory_id": memory.id,
                "memory_type": memory.type.value,
            },
        )
        return memory

    async def _find_duplicate(self, vector: Sequence[float], now: datetime) -> Memory | None:
        """找相似度超阈值的既有记忆（`docs/06` §6）。"""
        nearby = await self._store.search_similar(
            vector,
            user_id=self._config.user_id,
            companion_id=self._config.companion_id,
            now=now,
            limit=1,
        )
        if not nearby:
            return None

        memory_id, distance = nearby[0]
        if _similarity(distance) < self._config.dedup_threshold:
            return None

        return await self._store.get(memory_id)

    async def _merge(
        self,
        existing: Memory,
        candidate: MemoryCandidate,
        *,
        vector: Sequence[float],
        now: datetime,
    ) -> None:
        """合并到既有记忆：取较高的分数、累加 repetition、保留 provenance。

        ``docs/06`` §6 的三步：更新 confidence / importance、保留 provenance、
        不新建互不相关的重复条目。
        """
        metadata = dict(existing.metadata)
        metadata["repetition"] = existing.repetition + 1
        if "access_count" in existing.metadata:
            metadata["access_count"] = existing.access_count

        merged = replace(
            existing,
            # 重复观察会抬分，但不会因为一次更弱的表述而降分——取较大值
            importance=max(
                existing.importance,
                score_importance(
                    candidate, user_text=candidate.content, repetition=metadata["repetition"]
                ),
            ),
            confidence=max(
                existing.confidence, score_confidence(candidate, user_text=candidate.content)
            ),
            # 更晚观察到的 TTL 覆盖旧的：事实仍然有效
            expires_at=expiry_for(candidate.type, now),
            metadata=metadata,
        )
        await self._store.update(merged, vector)

        _logger.info(
            "memory updated",
            extra={
                "event": "memory.updated",
                "memory_id": merged.id,
                "memory_type": merged.type.value,
            },
        )

    # ── 管理（docs/06 §9）───────────────────────────────

    async def list_memories(self, query: MemoryFilter | None = None) -> tuple[Memory, ...]:
        return await self._store.list(query or self.default_filter())

    async def get(self, memory_id: str) -> Memory | None:
        return await self._store.get(memory_id)

    async def export(self) -> tuple[Memory, ...]:
        """导出**全部**记忆，含已归档与已过期——导出是备份，不是检索。"""
        return await self._store.list(
            MemoryFilter(
                user_id=self._config.user_id,
                companion_id=self._config.companion_id,
                include_archived=True,
                include_expired=True,
            )
        )

    async def count(self) -> int:
        return await self._store.count(
            user_id=self._config.user_id, companion_id=self._config.companion_id
        )

    async def delete(self, memory_id: str) -> bool:
        """硬删除：事实 + embedding + 向量索引（`docs/04` §2）。"""
        removed = await self._store.delete(memory_id)
        if removed:
            _logger.info(
                "memory deleted",
                extra={"event": "memory.deleted", "memory_id": memory_id},
            )
        return removed

    async def clear(self) -> int:
        """清空该用户的全部记忆（`docs/06` §9）。"""
        removed = await self._store.delete_all(
            user_id=self._config.user_id, companion_id=self._config.companion_id
        )
        _logger.info(
            "memory cleared",
            extra={"event": "memory.deleted", "count": removed},
        )
        return removed

    async def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        summary: str | None = None,
        memory_type: MemoryType | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        now: datetime | None = None,
    ) -> Memory | None:
        """编辑一条记忆。``content`` 变了就**重新向量化并更新索引**。

        只传要改的字段，其余保持不变（``None`` = 不改）。
        """
        existing = await self._store.get(memory_id)
        if existing is None:
            return None

        updated = replace(
            existing,
            content=content if content is not None else existing.content,
            summary=summary if summary is not None else existing.summary,
            type=memory_type if memory_type is not None else existing.type,
            importance=importance if importance is not None else existing.importance,
            confidence=confidence if confidence is not None else existing.confidence,
        )

        vector: Sequence[float] | None = None
        if content is not None and content != existing.content:
            if self._embeddings is None:
                # 没有 embedding 能力时只改文字，索引保持旧向量——不静默写错向量
                vector = None
            else:
                vectors = await self._embeddings.embed([content])
                vector = vectors[0] if vectors else None

        await self._store.update(updated, vector)
        _logger.info(
            "memory updated",
            extra={
                "event": "memory.updated",
                "memory_id": updated.id,
                "memory_type": updated.type.value,
            },
        )
        return updated

    async def is_enabled(self) -> bool:
        """记忆是否启用。``app_flags`` 没有记录时回落到 ``Settings`` 的初值。"""
        flag = await self._store.get_flag(ENABLED_FLAG)
        if flag is None:
            return self._config.enabled
        return flag == "true"

    async def set_enabled(self, enabled: bool) -> None:
        await self._store.set_flag(ENABLED_FLAG, "true" if enabled else "false", at=self._clock())

    def default_filter(self) -> MemoryFilter:
        return MemoryFilter(user_id=self._config.user_id, companion_id=self._config.companion_id)

    # ── 内部 ────────────────────────────────────────────

    @staticmethod
    def _log_failure(stage: str, error: Exception) -> None:
        _logger.warning(
            "memory subsystem failed; degrading to no-memory",
            extra={"event": "memory.failed", "detail": {"stage": stage, "error": str(error)}},
        )


def _similarity(distance: float) -> float:
    """余弦距离 → 相似度，并夹到 ``[0, 1]``。

    反向向量会得到距离 2.0（相似度 -1），直接当权重用会变成负贡献。
    """
    return min(max(1.0 - distance, 0.0), 1.0)


def _recency(created_at: datetime, now: datetime) -> float:
    """指数衰减，半衰期 30 天。未来时间戳（时钟回拨）夹到 1.0。"""
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / _RECENCY_HALF_LIFE_DAYS)
