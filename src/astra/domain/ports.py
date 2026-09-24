"""领域端口（Protocol）。

本模块**只**定义接口，实现一律放在 ``astra.infrastructure`` / ``astra.providers``。
禁止在本层 import fastapi / starlette / sqlite3 / httpx 或任何 infrastructure 模块，
由 ``tests/unit/test_architecture_rules.py`` 强制执行（ADR 0008）。

端口方法声明为 ``async def``，这是对**调用方**的契约；实现内部可以是同步的
（例如 SQLite 适配器内部用同步 sqlite3，只在方法入口 ``await asyncio.to_thread``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from astra.domain.audio import AudioArtifact, Transcript, TTSRequest
from astra.domain.conversation import Conversation, Message
from astra.domain.health import ComponentHealth
from astra.domain.llm import LLMRequest, LLMResponse
from astra.domain.memory import Memory, MemoryFilter
from astra.domain.state import StateSnapshot


class HealthProbe(Protocol):
    """单个外部依赖的健康探针。

    实现方应自行捕获 I/O 异常并返回 ``DOWN`` 状态。若实现意外抛出异常，
    ``HealthService`` 会把该组件记为 ``DOWN``，而不会让 ``/health`` 整体失败。
    """

    @property
    def name(self) -> str:
        """组件名，必须是 :data:`astra.domain.health.CANONICAL_COMPONENTS` 之一。"""
        ...

    async def check(self) -> ComponentHealth:
        """探测并返回当前状态。不得阻塞事件循环。"""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """文本生成后端。

    失败时抛 :mod:`astra.domain.errors` 里的异常，而不是返回错误对象——
    Orchestrator 的 ``try/except`` 是唯一的降级点，与 ``HealthService``
    隔离探针异常是同一个模式。

    ``runtime_checkable`` 只让测试能写 ``isinstance(FakeLLMProvider(), LLMProvider)``
    （仅检查方法存在性），不用于生产分支。
    """

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """执行一次生成。实现必须自行应用超时，且不得阻塞事件循环。"""
        ...


@runtime_checkable
class STTProvider(Protocol):
    """语音识别后端（M4）。

    输入是**已经解码的 int16 PCM**（不是 WAV 容器、不是浮点）——容器格式是
    provider 的实现细节（CosyVoice 雨落版需要传文件，所以由它自己包 WAV）。

    **端口是"整段"的，不是流式的**：M4 不做流式识别，也没有 ``stt.partial``。
    这与 ``TTSProvider`` 的非流式设计同源（ADR 0009 / 0022）。

    失败时抛 :class:`astra.domain.errors.STTUnavailableError`——与 ``LLMProvider``
    同一约定（抛异常而不是返回错误对象），由调用方统一降级。
    """

    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        language_hint: str | None = None,
    ) -> Transcript:
        """把一段 PCM 转成文字。实现必须自行应用超时。"""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """语音合成后端（M4）。

    **刻意是非流式的**：`docs/07` §3 写的接口是
    ``synthesize_stream -> AsyncIterator[bytes]``，但首选后端（CosyVoice3 雨落版）
    是 Gradio 应用，接口是**文件级**的——它返回一个生成完毕的 ``.wav`` 路径，
    做不到"首包即播"。接口必须按真实能力设计，不能假装能流式（ADR 0009）。

    所以 M4 的"低等待感"只能靠**整段合成 + 尽快推流**实现，
    不引入 ``supports_streaming`` 之类的能力开关：现在没有任何消费者，
    留一个没人读的标志就是死代码。将来真接入流式后端时**扩端口**，而不是提前留位。

    失败时抛 :class:`astra.domain.errors.TTSUnavailableError`。调用方必须
    **捕获它并继续**——文本回复已经送达，TTS 挂掉不该让 turn 失败
    （`docs/10` §4「TTS failure」）。
    """

    async def synthesize(self, request: TTSRequest) -> AudioArtifact:
        """把文本合成为 int16 PCM。实现必须自行应用超时。"""
        ...


class ConversationRepository(Protocol):
    """会话与消息的持久化端口。

    实现位于 ``astra.infrastructure.db``。方法声明为 ``async def``（对调用方的
    契约），SQLite 实现内部是同步的，只在方法入口 ``await asyncio.to_thread``。
    """

    async def create(self, conversation: Conversation) -> None:
        """新建一个会话。"""
        ...

    async def get(self, conversation_id: str) -> Conversation | None:
        """按 id 读取会话；不存在返回 ``None``。"""
        ...

    async def find_active(self, *, user_id: str, companion_id: str) -> Conversation | None:
        """取该角色最近一个 ``active`` 会话；没有则返回 ``None``。"""
        ...

    async def end(self, conversation_id: str, *, ended_at: datetime) -> None:
        """把会话置为 ``ended``。"""
        ...

    async def last_message_at(self, conversation_id: str) -> datetime | None:
        """该会话最后一条消息的时间；空会话返回 ``None``。"""
        ...

    async def append_messages(self, messages: Sequence[Message]) -> tuple[str, ...]:
        """**幂等**写入一批消息，返回本次真正插入的消息 id。

        已存在同 ``(turn_id, role)`` 的消息会被跳过（`docs/04` §6 事件幂等性）。
        """
        ...

    async def load_messages(self, conversation_id: str) -> tuple[Message, ...]:
        """按 **id 升序**返回该会话的全部消息（ADR 0012：不能用 created_at 排序）。"""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本向量化后端（M2）。

    ``dim`` 是启动期校验的关键：``memory_vectors`` 的 vec0 表把维度写死在 DDL 里
    （迁移不可改），所以装配期必须比对，不一致就快速失败而不是运行时出错（ADR 0018）。
    """

    @property
    def model(self) -> str:
        """模型标识，落进 ``memory_embeddings.embedding_model``。"""
        ...

    @property
    def dim(self) -> int:
        """向量维度。"""
        ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """批量向量化，返回顺序与 ``texts`` 一一对应。

        失败时抛 :mod:`astra.domain.errors` 里的异常——与 ``LLMProvider`` 同一约定，
        由调用方（``MemoryService``）统一降级，而不是返回错误对象。
        """
        ...


class MemoryStore(Protocol):
    """记忆的持久化端口：**事实表 + 向量索引 + 运行时开关**。

    三者放在同一个端口下是刻意的：它们在同一份 SQLite 文件里，而
    ``docs/04`` §2 / ``docs/11`` §4 要求"删除记忆时必须同时处理向量索引"。
    拆成多个端口会让这条不变量失去单一入口。
    """

    async def add(self, memory: Memory, embedding: Sequence[float]) -> None:
        """写入一条记忆及其向量（事实 + embedding + 索引，同一事务）。"""
        ...

    async def get(self, memory_id: str) -> Memory | None:
        """按 id 读取；不存在返回 ``None``。"""
        ...

    async def get_many(self, memory_ids: Sequence[str]) -> tuple[Memory, ...]:
        """批量读取。**返回顺序跟随入参顺序**，缺失的 id 被跳过。"""
        ...

    async def list(self, query: MemoryFilter) -> tuple[Memory, ...]:
        """按过滤条件列出（``ORDER BY id``）。"""
        ...

    async def update(self, memory: Memory, embedding: Sequence[float] | None = None) -> None:
        """更新一条记忆。``embedding`` 非空时同时更新向量（内容变了才需要）。"""
        ...

    async def delete(self, memory_id: str) -> bool:
        """**真正删除**事实、embedding 与向量索引（`docs/04` §2）。返回是否存在。"""
        ...

    async def delete_all(self, *, user_id: str, companion_id: str) -> int:
        """清空该用户的全部记忆，返回删除条数。"""
        ...

    async def touch_accessed(self, memory_ids: Sequence[str], *, at: datetime) -> None:
        """更新 ``last_accessed_at``（`docs/06` §4 的 ``access_frequency`` 需要它）。"""
        ...

    async def search_similar(
        self,
        vector: Sequence[float],
        *,
        user_id: str,
        companion_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """向量近邻检索，返回 ``(memory_id, distance)``，按距离升序。

        只返回**可检索**的记忆（未归档、未过期）；重排序（importance / recency /
        access / relationship）不在这里做——那需要 ``docs/06`` §4 的完整公式，
        由 ``MemoryService`` 负责。
        """
        ...

    async def count(self, *, user_id: str, companion_id: str) -> int:
        """该用户当前未归档的记忆条数。"""
        ...

    async def get_flag(self, key: str) -> str | None:
        """读 ``app_flags``（M2 只用到 ``memory.enabled`` 一个键）。"""
        ...

    async def set_flag(self, key: str, value: str, *, at: datetime) -> None:
        """写 ``app_flags``。"""
        ...


class StateStore(Protocol):
    """情绪 / 关系状态的持久化端口（M3）。

    **只有"读整份快照"和"写整份快照"两个动作**——状态是每个 companion 一行，
    不存在部分更新的语义；把衰减、增量、clamp 全放在 ``StateService`` 里，
    仓储只负责搬运。这样"状态是怎么变的"只有一个地方可读。
    """

    async def load(self, companion_id: str) -> StateSnapshot | None:
        """读整份快照；**从没有过记录**时返回 ``None``（调用方用基线兜底）。"""
        ...

    async def save(self, companion_id: str, snapshot: StateSnapshot) -> None:
        """原子地写回两份状态（同一个事务）。"""
        ...

    async def reset(self, companion_id: str) -> None:
        """删掉记录。下次读会回到基线（调试用）。"""
        ...
