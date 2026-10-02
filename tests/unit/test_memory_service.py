"""``MemoryService`` 的单元测试。

用**真实** ``SQLiteMemoryStore``（临时库）+ ``FakeEmbeddingProvider``：
去重/合并/检索重排全都要真的走一遍向量索引，用替身会把要验的东西替掉。

维度必须 768——``memory_vectors`` 的 DDL 写死了它，用别的维度连插入都会失败。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.application.memory_service import (
    ENABLED_FLAG,
    MemoryConfig,
    MemoryService,
    RetrievalWeights,
    detect_no_store,
)
from astra.domain.errors import LLMUnavailableError
from astra.domain.memory import MemoryCandidate, MemoryFilter, MemoryType
from astra.infrastructure.db.memory_repository import SQLiteMemoryStore
from astra.infrastructure.db.migrations import upgrade
from tests.fakes import FakeEmbeddingProvider

_DIM = 768
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_USER = "local"
_COMPANION = "astra"

#: 两条语义几乎相同的文本：余弦相似度 ≈ 0.995（> 0.90 阈值）
_SIMILAR_A = "用户喜欢深夜写代码"
_SIMILAR_B = "用户习惯在深夜写程序"
_UNRELATED = "完全不相关的另一个话题"
_PROJECT = "用户在做本地 AI 项目"
_CAT = "用户在养一只猫"


def _v(*head: float) -> tuple[float, ...]:
    """768 维向量，只让前几维非零。"""
    return (*head, *([0.0] * (_DIM - len(head))))


_VECTORS = {
    _SIMILAR_A: _v(1.0, 0.0),
    _SIMILAR_B: _v(1.0, 0.1),
    _PROJECT: _v(0.0, 1.0),
    _CAT: _v(0.0, 0.0, 1.0),
    _UNRELATED: _v(0.0, 0.0, 0.0, 1.0),
}


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMemoryStore:
    db_path = tmp_path / "astra.db"
    upgrade(db_path)
    return SQLiteMemoryStore(db_path, embedding_model="fake-embedding", embedding_dim=_DIM)


@pytest.fixture
def embeddings() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dim=_DIM, vectors=_VECTORS)


@pytest.fixture
def service(store: SQLiteMemoryStore, embeddings: FakeEmbeddingProvider) -> MemoryService:
    return MemoryService(
        store,
        embeddings,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION),
        clock=lambda: _NOW,
    )


def _candidate(content: str, memory_type: MemoryType = MemoryType.PREFERENCE) -> MemoryCandidate:
    return MemoryCandidate(type=memory_type, content=content)


# ── no-store 检测 ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "我明天要去医院，这件事不要记住。",
        "这个别记住",
        "不用记，我随便说说",
        "忘掉这件事吧",
        "不要记录我的地址",
    ],
)
def test_no_store_phrases_are_detected(text: str) -> None:
    assert detect_no_store(text) is True


@pytest.mark.parametrize("text", ["请记住我喜欢深夜写代码", "今天天气不错", "我记住了"])
def test_ordinary_text_is_not_no_store(text: str) -> None:
    assert detect_no_store(text) is False


# ── 写入 ────────────────────────────────────────────────


def test_ingest_creates_a_memory(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    assert len(created) == 1
    memory = created[0]
    assert memory.content == _SIMILAR_A
    assert memory.type is MemoryType.PREFERENCE
    assert memory.user_id == _USER
    assert 0.0 <= memory.importance <= 1.0
    assert 0.0 <= memory.confidence <= 1.0
    assert memory.expires_at is None  # preference 是长期记忆


def test_ingest_applies_ttl_for_short_term_types(service: MemoryService) -> None:
    created = _run(
        service.ingest_candidates(
            [_candidate("用户今天在复习数据库", MemoryType.EPISODIC)],
            user_text="今天我在复习数据库",
        )
    )

    assert created[0].expires_at == _NOW + timedelta(days=7)


def test_ingest_records_provenance(service: MemoryService) -> None:
    created = _run(
        service.ingest_candidates(
            [_candidate(_SIMILAR_A)],
            user_text=_SIMILAR_A,
            source_message_id="msg-1",
            correlation_id="turn-1",
        )
    )

    assert created[0].source_message_id == "msg-1"
    assert created[0].metadata["correlation_id"] == "turn-1"


def test_candidates_are_embedded_in_one_request(
    service: MemoryService, embeddings: FakeEmbeddingProvider
) -> None:
    """一轮的候选**一次**送进 embedding（原先一条一次）。

    这不只是省往返——第三方 embedding 按请求计价，一轮三条候选就是三次计费。
    """
    created = _run(
        service.ingest_candidates(
            [_candidate(_SIMILAR_A), _candidate(_CAT), _candidate(_PROJECT)],
            user_text="随便聊几句",
        )
    )

    assert len(created) == 3
    assert embeddings.calls == [(_SIMILAR_A, _CAT, _PROJECT)]


def test_similar_candidates_are_merged_not_duplicated(service: MemoryService) -> None:
    """docs/06 §6：相似度超阈值必须 merge，不能创建两条互不相关的记忆。"""
    first = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))
    second = _run(service.ingest_candidates([_candidate(_SIMILAR_B)], user_text=_SIMILAR_B))

    assert len(first) == 1
    assert second == ()  # 合并，不算新建

    stored = _run(service.list_memories())
    assert len(stored) == 1
    assert stored[0].content == _SIMILAR_A  # 保留原有表述（provenance）
    assert stored[0].repetition == 1


def test_merge_raises_importance_monotonically(service: MemoryService) -> None:
    first = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))
    before = first[0].importance

    _run(service.ingest_candidates([_candidate(_SIMILAR_B)], user_text=_SIMILAR_B))

    after = _run(service.list_memories())[0].importance
    assert after >= before


def test_dissimilar_candidates_create_separate_memories(service: MemoryService) -> None:
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))
    _run(service.ingest_candidates([_candidate("用户在养一只猫")], user_text="我在养一只猫"))

    assert len(_run(service.list_memories())) == 2


def test_no_store_prevents_writing(service: MemoryService) -> None:
    created = _run(
        service.ingest_candidates([_candidate(_SIMILAR_A)], user_text="我喜欢深夜写代码，但别记住")
    )

    assert created == ()
    assert _run(service.list_memories()) == ()


def test_disabled_memory_does_not_write(service: MemoryService) -> None:
    _run(service.set_enabled(False))

    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    assert created == ()
    assert _run(service.list_memories()) == ()


def test_candidate_count_is_capped(
    store: SQLiteMemoryStore, embeddings: FakeEmbeddingProvider
) -> None:
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION, max_candidates_per_turn=2),
        clock=lambda: _NOW,
    )
    candidates = [_candidate(f"事实 {index}") for index in range(5)]

    created = _run(service.ingest_candidates(candidates, user_text="记住这些"))

    assert len(created) == 2


def test_ingest_without_embeddings_is_a_noop(store: SQLiteMemoryStore) -> None:
    service = MemoryService(
        store, None, config=MemoryConfig(user_id=_USER, companion_id=_COMPANION)
    )

    assert _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A)) == ()


def test_embedding_failure_does_not_raise(store: SQLiteMemoryStore) -> None:
    """embedding 挂了只降级——LLM 已经成功，用户必须拿到回复。"""
    broken = FakeEmbeddingProvider(dim=_DIM, error=LLMUnavailableError("down"))
    service = MemoryService(
        store,
        broken,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION),
        clock=lambda: _NOW,
    )

    assert _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A)) == ()


def test_empty_candidates_is_a_noop(service: MemoryService) -> None:
    assert _run(service.ingest_candidates([], user_text="你好")) == ()


# ── 检索 ────────────────────────────────────────────────


def test_retrieve_finds_the_relevant_memory(service: MemoryService) -> None:
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    hits = _run(service.retrieve(_SIMILAR_B))

    assert len(hits) == 1
    assert hits[0].memory.content == _SIMILAR_A
    assert hits[0].score > 0.0


def test_retrieve_returns_empty_when_nothing_is_close_enough(store: SQLiteMemoryStore) -> None:
    """``min_similarity`` 之下不该硬塞无关记忆进 prompt。"""
    embeddings = FakeEmbeddingProvider(dim=_DIM, vectors=_VECTORS)
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION, min_similarity=0.95),
        clock=lambda: _NOW,
    )
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    assert _run(service.retrieve(_UNRELATED)) == ()


def test_gate_rejects_an_important_but_irrelevant_memory(store: SQLiteMemoryStore) -> None:
    """**回归测试**：准入闸门必须只看语义，不能被 importance / recency 凑过去。

    这是 M2 实机验收时暴露的问题——原先用加权总分当闸门，一条"很重要很新但
    完全不相干"的记忆会靠非语义项过线，被塞进 prompt。
    """
    embeddings = FakeEmbeddingProvider(
        dim=_DIM,
        vectors={_UNRELATED: _v(0.0, 0.0, 0.0, 1.0), "查询": _v(1.0)},
    )
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(
            user_id=_USER,
            companion_id=_COMPANION,
            min_similarity=0.70,
            # 就算排序权重全给 importance，也不该放它进来
            weights=RetrievalWeights(
                semantic=0.0,
                importance=1.0,
                recency=0.0,
                access=0.0,
                relationship=0.0,
            ),
        ),
        clock=lambda: _NOW,
    )
    created = _run(service.ingest_candidates([_candidate(_UNRELATED)], user_text=_UNRELATED))
    _run(service.update(created[0].id, importance=1.0))

    # 时间上很新、importance 满分，但语义上正交 → 必须被闸门拦下
    assert _run(service.retrieve("查询")) == ()


def test_retrieve_ranks_by_the_documented_formula(store: SQLiteMemoryStore) -> None:
    """两个都相关时，importance 更高的排前面（docs/06 §4 的加权和）。

    ``dedup_threshold=1.0`` 是为了让两条相近的记忆能共存——否则它们会先被合并，
    就测不到排序了。
    """
    embeddings = FakeEmbeddingProvider(
        dim=_DIM, vectors={"查询": _v(1.0), "事实甲": _v(1.0), "事实乙": _v(1.0, 0.05)}
    )
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(
            user_id=_USER, companion_id=_COMPANION, min_similarity=0.0, dedup_threshold=1.0
        ),
        clock=lambda: _NOW,
    )
    first = _run(service.ingest_candidates([_candidate("事实甲")], user_text="事实甲"))
    second = _run(service.ingest_candidates([_candidate("事实乙")], user_text="事实乙"))
    assert len(first) == 1
    assert len(second) == 1

    _run(service.update(first[0].id, importance=0.95))
    _run(service.update(second[0].id, importance=0.05))

    hits = _run(service.retrieve("查询"))

    assert [hit.memory.id for hit in hits] == [first[0].id, second[0].id]


def test_retrieve_touches_access_stats(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    _run(service.retrieve(_SIMILAR_B))

    reloaded = _run(service.get(created[0].id))
    assert reloaded is not None
    assert reloaded.access_count == 1
    assert reloaded.last_accessed_at == _NOW


def test_retrieve_respects_top_k(store: SQLiteMemoryStore) -> None:
    embeddings = FakeEmbeddingProvider(
        dim=_DIM, vectors={"查询": _v(1.0), "a": _v(1.0, 0.01), "b": _v(1.0, 0.02)}
    )
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(
            user_id=_USER,
            companion_id=_COMPANION,
            top_k=1,
            min_similarity=0.0,
            dedup_threshold=1.0,
        ),
        clock=lambda: _NOW,
    )
    _run(service.ingest_candidates([_candidate("a")], user_text="a"))
    _run(service.ingest_candidates([_candidate("b")], user_text="b"))

    assert len(_run(service.retrieve("查询"))) == 1


def test_retrieve_excludes_expired_memories(service: MemoryService) -> None:
    created = _run(
        service.ingest_candidates(
            [_candidate("用户今天在复习数据库", MemoryType.EPISODIC)],
            user_text="今天我在复习数据库",
        )
    )
    # TTL 是 7 天，把时钟推到 8 天后
    later = _NOW + timedelta(days=8)

    hits = _run(service.retrieve("复习数据库", now=later))

    assert hits == ()
    assert _run(service.get(created[0].id)) is not None  # 事实还在，只是检索不到


def test_retrieve_returns_empty_when_disabled(service: MemoryService) -> None:
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))
    _run(service.set_enabled(False))

    assert _run(service.retrieve(_SIMILAR_B)) == ()


def test_retrieve_returns_empty_without_embeddings(store: SQLiteMemoryStore) -> None:
    service = MemoryService(
        store, None, config=MemoryConfig(user_id=_USER, companion_id=_COMPANION)
    )

    assert _run(service.retrieve("随便什么")) == ()


def test_retrieve_survives_embedding_failure(store: SQLiteMemoryStore) -> None:
    good = FakeEmbeddingProvider(dim=_DIM, vectors=_VECTORS)
    service = MemoryService(
        store, good, config=MemoryConfig(user_id=_USER, companion_id=_COMPANION), clock=lambda: _NOW
    )
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    service._embeddings = FakeEmbeddingProvider(dim=_DIM, error=LLMUnavailableError("down"))

    assert _run(service.retrieve(_SIMILAR_B)) == ()


def test_hit_components_explain_the_score(service: MemoryService) -> None:
    """只有总分的话调权重就是盲调——分项必须留痕。"""
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    hit = _run(service.retrieve(_SIMILAR_B))[0]

    assert set(hit.components) == {"semantic", "importance", "recency", "access", "relationship"}
    assert hit.score == pytest.approx(sum(hit.components.values()))
    # M3 之前 relationship 恒为 0——显式写死，不假装有数据
    assert hit.components["relationship"] == 0.0


def test_custom_weights_are_honoured(store: SQLiteMemoryStore) -> None:
    embeddings = FakeEmbeddingProvider(dim=_DIM, vectors=_VECTORS)
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(
            user_id=_USER,
            companion_id=_COMPANION,
            min_similarity=0.0,
            weights=RetrievalWeights(
                semantic=1.0,
                importance=0.0,
                recency=0.0,
                access=0.0,
                relationship=0.0,
            ),
        ),
        clock=lambda: _NOW,
    )
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    hit = _run(service.retrieve(_SIMILAR_B))[0]

    assert hit.score == pytest.approx(hit.components["semantic"])


# ── 管理 ────────────────────────────────────────────────


def test_list_returns_only_retrievable_by_default(service: MemoryService) -> None:
    """默认过滤条件只返回「当前可检索」的记忆。

    **必须用不过期的类型**（PREFERENCE）：这里的 ``_NOW`` 是钉死的 2026-09-24，
    而 ``MemoryStore.list`` 判过期用的是**真实时钟**（它没有 ``now`` 形参，
    与 ``search_similar`` 不同）。用 EPISODIC（TTL 7 天）的话，这条记忆会在
    真实时间走过 2026-10-01 之后变成"已过期"，测试就会在某个日历日突然变红。
    过期过滤本身由 ``test_export_includes_archived_and_expired`` 覆盖。
    """
    _run(
        service.ingest_candidates(
            [_candidate("用户今天在复习数据库", MemoryType.PREFERENCE)],
            user_text="今天我在复习数据库",
        )
    )

    assert len(_run(service.list_memories())) == 1
    assert (
        len(_run(service.list_memories(MemoryFilter(user_id=_USER, companion_id=_COMPANION)))) == 1
    )


def test_export_includes_archived_and_expired(service: MemoryService) -> None:
    """导出是备份，不是检索——归档与过期的也要在里面。"""
    # 30 天前创建的 episodic：TTL 7 天，此刻早已过期
    created = _run(
        service.ingest_candidates(
            [_candidate("用户那天在复习数据库", MemoryType.EPISODIC)],
            user_text="今天我在复习数据库",
            now=_NOW - timedelta(days=30),
        )
    )
    assert created[0].expires_at is not None
    assert created[0].is_expired(_NOW) is True

    assert _run(service.list_memories()) == ()
    assert len(_run(service.export())) == 1


def test_archived_memories_are_hidden_but_exportable(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    _run(service._store.update(replace(created[0], archived_at=_NOW)))

    assert _run(service.list_memories()) == ()
    assert len(_run(service.export())) == 1


def test_delete_removes_the_memory(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    assert _run(service.delete(created[0].id)) is True
    assert _run(service.get(created[0].id)) is None
    assert _run(service.retrieve(_SIMILAR_B)) == ()


def test_delete_unknown_id_returns_false(service: MemoryService) -> None:
    assert _run(service.delete("nope")) is False


def test_clear_removes_everything(service: MemoryService) -> None:
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))
    _run(service.ingest_candidates([_candidate("用户在养一只猫")], user_text="我在养一只猫"))

    assert _run(service.clear()) == 2
    assert _run(service.count()) == 0
    assert _run(service.retrieve(_SIMILAR_B)) == ()


def test_count_ignores_archived(service: MemoryService) -> None:
    _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    assert _run(service.count()) == 1


def test_update_changes_fields(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    updated = _run(
        service.update(
            created[0].id,
            content="用户改成了早起写代码",
            summary="作息偏好",
            importance=0.33,
            confidence=0.44,
        )
    )

    assert updated is not None
    assert updated.content == "用户改成了早起写代码"
    assert updated.summary == "作息偏好"
    assert updated.importance == 0.33
    assert updated.confidence == 0.44


def test_update_reembeds_when_content_changes(service: MemoryService) -> None:
    created = _run(service.ingest_candidates([_candidate(_SIMILAR_A)], user_text=_SIMILAR_A))

    _run(service.update(created[0].id, content="用户在养一只猫"))

    hits = _run(service.retrieve("用户在养一只猫"))
    assert [hit.memory.id for hit in hits] == [created[0].id]


def test_update_unknown_id_returns_none(service: MemoryService) -> None:
    assert _run(service.update("nope", content="x")) is None


def test_enabled_flag_round_trips(service: MemoryService) -> None:
    assert _run(service.is_enabled()) is True

    _run(service.set_enabled(False))
    assert _run(service.is_enabled()) is False

    _run(service.set_enabled(True))
    assert _run(service.is_enabled()) is True


def test_enabled_flag_falls_back_to_config(
    store: SQLiteMemoryStore, embeddings: FakeEmbeddingProvider
) -> None:
    """``app_flags`` 没有记录时用 ``Settings`` 的初值。"""
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION, enabled=False),
    )

    assert _run(service.is_enabled()) is False
    assert _run(store.get_flag(ENABLED_FLAG)) is None


def test_flag_overrides_config_default(
    store: SQLiteMemoryStore, embeddings: FakeEmbeddingProvider
) -> None:
    service = MemoryService(
        store,
        embeddings,
        config=MemoryConfig(user_id=_USER, companion_id=_COMPANION, enabled=False),
    )

    _run(service.set_enabled(True))

    assert _run(service.is_enabled()) is True
