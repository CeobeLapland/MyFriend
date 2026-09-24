"""``SQLiteMemoryStore`` 的单元测试（真实 sqlite-vec，临时库）。

**用真实扩展而不是替身**：这一层的价值恰恰在于"事实表 + embedding + vec0 索引
三者是否真的同步"，替身会把要验的东西替掉。

维度固定 768——``memory_vectors`` 的 DDL 把它写死了，测试跟着走才算真的覆盖
（装配期的维度校验另见 ``test_embeddings_provider`` / ``test_settings``）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.domain.memory import Memory, MemoryFilter, MemoryType
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.memory_repository import SQLiteMemoryStore
from astra.infrastructure.db.migrations import upgrade

_DIM = 768
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_USER = "local"
_COMPANION = "astra"


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _vec(*head: float) -> tuple[float, ...]:
    """构造 768 维向量，只让前几维非零——写测试时不必数 768 个 0。"""
    assert len(head) <= _DIM
    return (*head, *([0.0] * (_DIM - len(head))))


def _memory(memory_id: str, **overrides: object) -> Memory:
    defaults: dict[str, object] = {
        "id": memory_id,
        "user_id": _USER,
        "companion_id": _COMPANION,
        "type": MemoryType.PREFERENCE,
        "content": "用户喜欢深夜写代码",
        "created_at": _NOW,
        "importance": 0.8,
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return Memory(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "astra.db"
    upgrade(path)
    return path


@pytest.fixture
def store(db_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path, embedding_model="fake-embedding", embedding_dim=_DIM)


def _table_count(db_path: Path, table: str) -> int:
    with open_connection(db_path, load_extensions=True) as conn:
        row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
    return int(row["n"])


# ── 往返 ────────────────────────────────────────────────


def test_add_and_get_round_trip(store: SQLiteMemoryStore) -> None:
    memory = _memory(
        "m1",
        type=MemoryType.EVENT,
        content="用户完成了 M1",
        summary="M1 完成",
        importance=0.42,
        confidence=0.37,
        source_message_id="msg-1",
        source_event_id="evt-1",
        expires_at=_NOW + timedelta(days=30),
        metadata={"repetition": 2},
    )

    _run(store.add(memory, _vec(1.0)))

    loaded = _run(store.get("m1"))
    assert loaded == memory


def test_get_missing_returns_none(store: SQLiteMemoryStore) -> None:
    assert _run(store.get("nope")) is None


def test_get_many_preserves_input_order_and_skips_missing(store: SQLiteMemoryStore) -> None:
    for name in ("m1", "m2", "m3"):
        _run(store.add(_memory(name), _vec(1.0)))

    loaded = _run(store.get_many(["m3", "missing", "m1"]))

    assert [memory.id for memory in loaded] == ["m3", "m1"]


def test_get_many_with_empty_input_does_not_query(store: SQLiteMemoryStore) -> None:
    assert _run(store.get_many([])) == ()


def test_timestamps_are_timezone_aware_after_round_trip(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))

    loaded = _run(store.get("m1"))

    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at == _NOW


# ── 向量检索 ────────────────────────────────────────────


def test_search_finds_the_exact_vector_first(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0, 0.0)))
    _run(store.add(_memory("m2"), _vec(0.0, 1.0)))

    hits = _run(
        store.search_similar(
            _vec(1.0, 0.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5
        )
    )

    assert [memory_id for memory_id, _ in hits] == ["m1", "m2"]
    assert hits[0][1] == pytest.approx(0.0)
    # 余弦距离：正交 = 1.0（L2 会是 √2）
    assert hits[1][1] == pytest.approx(1.0)


def test_search_ranks_by_cosine_proximity(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("near"), _vec(1.0, 0.1)))
    _run(store.add(_memory("far"), _vec(0.0, 1.0)))
    _run(store.add(_memory("mid"), _vec(1.0, 0.5)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5)
    )

    assert [memory_id for memory_id, _ in hits] == ["near", "mid", "far"]


def test_search_respects_limit(store: SQLiteMemoryStore) -> None:
    for index in range(4):
        _run(store.add(_memory(f"m{index}"), _vec(1.0, index * 0.1)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=2)
    )

    assert len(hits) == 2


def test_search_with_non_positive_limit_returns_nothing(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=0)
    )

    assert hits == ()


def test_search_excludes_archived(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1", archived_at=_NOW), _vec(1.0)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5)
    )

    assert hits == ()


def test_search_excludes_expired(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1", expires_at=_NOW - timedelta(seconds=1)), _vec(1.0)))
    _run(store.add(_memory("m2", expires_at=_NOW + timedelta(days=1)), _vec(1.0)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5)
    )

    assert [memory_id for memory_id, _ in hits] == ["m2"]


def test_search_is_scoped_to_user_and_companion(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("mine"), _vec(1.0)))
    _run(store.add(_memory("theirs", user_id="someone-else"), _vec(1.0)))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5)
    )

    assert [memory_id for memory_id, _ in hits] == ["mine"]


def test_search_with_wrong_dimension_is_rejected(store: SQLiteMemoryStore) -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        _run(
            store.search_similar(
                (1.0, 0.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=1
            )
        )


# ── 删除：必须三处一起删 ────────────────────────────────


def test_add_writes_all_three_stores(store: SQLiteMemoryStore, db_path: Path) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))

    assert _table_count(db_path, "memories") == 1
    assert _table_count(db_path, "memory_embeddings") == 1
    assert _table_count(db_path, "memory_vectors") == 1


def test_delete_removes_facts_embedding_and_vector(store: SQLiteMemoryStore, db_path: Path) -> None:
    """验收第 5 步（删除后不再检索到）的根：漏删任何一处都会静默失败。"""
    _run(store.add(_memory("m1"), _vec(1.0)))

    assert _run(store.delete("m1")) is True

    assert _table_count(db_path, "memories") == 0
    assert _table_count(db_path, "memory_embeddings") == 0
    assert _table_count(db_path, "memory_vectors") == 0
    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=5)
    )
    assert hits == ()


def test_delete_unknown_id_returns_false(store: SQLiteMemoryStore) -> None:
    assert _run(store.delete("nope")) is False


def test_delete_all_clears_only_the_given_scope(store: SQLiteMemoryStore, db_path: Path) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))
    _run(store.add(_memory("m2"), _vec(1.0)))
    _run(store.add(_memory("other", user_id="someone-else"), _vec(1.0)))

    removed = _run(store.delete_all(user_id=_USER, companion_id=_COMPANION))

    assert removed == 2
    assert _table_count(db_path, "memories") == 1
    assert _table_count(db_path, "memory_embeddings") == 1
    assert _table_count(db_path, "memory_vectors") == 1


def test_delete_all_on_empty_store_returns_zero(store: SQLiteMemoryStore) -> None:
    assert _run(store.delete_all(user_id=_USER, companion_id=_COMPANION)) == 0


def test_deleting_a_memory_cascades_to_its_embedding_row(
    store: SQLiteMemoryStore, db_path: Path
) -> None:
    """``memory_embeddings`` 靠 ``ON DELETE CASCADE``——这条锁住外键真的开着。"""
    _run(store.add(_memory("m1"), _vec(1.0)))

    _run(store.delete("m1"))

    assert _table_count(db_path, "memory_embeddings") == 0


# ── 更新 ────────────────────────────────────────────────


def test_update_changes_scalar_fields_without_touching_created_at(
    store: SQLiteMemoryStore,
) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))
    later = _NOW + timedelta(days=1)

    _run(
        store.update(
            _memory(
                "m1",
                content="用户改成了早起写代码",
                importance=0.2,
                confidence=0.3,
                created_at=later,  # 应被忽略
                archived_at=later,
            )
        )
    )

    loaded = _run(store.get("m1"))
    assert loaded is not None
    assert loaded.content == "用户改成了早起写代码"
    assert loaded.importance == 0.2
    assert loaded.archived_at == later
    assert loaded.created_at == _NOW


def test_update_without_embedding_keeps_the_old_vector(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0, 0.0)))

    _run(store.update(_memory("m1", content="只改文字")))

    hits = _run(
        store.search_similar(_vec(1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=1)
    )
    assert [memory_id for memory_id, _ in hits] == ["m1"]
    assert hits[0][1] == pytest.approx(0.0)


def test_update_with_embedding_replaces_the_vector(store: SQLiteMemoryStore, db_path: Path) -> None:
    _run(store.add(_memory("m1"), _vec(1.0, 0.0)))

    _run(store.update(_memory("m1", content="换成另一个方向"), _vec(0.0, 1.0)))

    hits = _run(
        store.search_similar(
            _vec(0.0, 1.0), user_id=_USER, companion_id=_COMPANION, now=_NOW, limit=1
        )
    )
    assert [memory_id for memory_id, _ in hits] == ["m1"]
    assert hits[0][1] == pytest.approx(0.0)
    # 先删后插，不能留下重复的索引行
    assert _table_count(db_path, "memory_vectors") == 1


def test_update_with_wrong_dimension_is_rejected(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))

    with pytest.raises(ValueError, match="dimension mismatch"):
        _run(store.update(_memory("m1"), (1.0, 0.0)))


# ── 列表与统计 ──────────────────────────────────────────


def test_list_orders_by_id(store: SQLiteMemoryStore) -> None:
    for name in ("m3", "m1", "m2"):
        _run(store.add(_memory(name), _vec(1.0)))

    listed = _run(store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION)))

    assert [memory.id for memory in listed] == ["m1", "m2", "m3"]


def test_list_filters_by_type(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1", type=MemoryType.EVENT), _vec(1.0)))
    _run(store.add(_memory("m2", type=MemoryType.PREFERENCE), _vec(1.0)))

    listed = _run(
        store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION, types=(MemoryType.EVENT,)))
    )

    assert [memory.id for memory in listed] == ["m1"]


def test_list_filters_by_min_importance(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("low", importance=0.2), _vec(1.0)))
    _run(store.add(_memory("high", importance=0.9), _vec(1.0)))

    listed = _run(
        store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION, min_importance=0.5))
    )

    assert [memory.id for memory in listed] == ["high"]


def test_list_filters_by_created_after(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("old", created_at=_NOW - timedelta(days=10)), _vec(1.0)))
    _run(store.add(_memory("new", created_at=_NOW), _vec(1.0)))

    listed = _run(
        store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION, created_after=_NOW))
    )

    assert [memory.id for memory in listed] == ["new"]


def test_list_excludes_archived_and_expired_by_default(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("live"), _vec(1.0)))
    _run(store.add(_memory("archived", archived_at=_NOW), _vec(1.0)))
    _run(store.add(_memory("expired", expires_at=_NOW - timedelta(days=1)), _vec(1.0)))

    listed = _run(store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION)))

    assert [memory.id for memory in listed] == ["live"]


def test_list_can_include_archived_and_expired(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("archived", archived_at=_NOW), _vec(1.0)))
    _run(store.add(_memory("expired", expires_at=_NOW - timedelta(days=1)), _vec(1.0)))

    listed = _run(
        store.list(
            MemoryFilter(
                user_id=_USER,
                companion_id=_COMPANION,
                include_archived=True,
                include_expired=True,
            )
        )
    )

    assert [memory.id for memory in listed] == ["archived", "expired"]


def test_list_respects_limit(store: SQLiteMemoryStore) -> None:
    for index in range(3):
        _run(store.add(_memory(f"m{index}"), _vec(1.0)))

    listed = _run(store.list(MemoryFilter(user_id=_USER, companion_id=_COMPANION, limit=2)))

    assert [memory.id for memory in listed] == ["m0", "m1"]


def test_count_ignores_archived(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))
    _run(store.add(_memory("m2", archived_at=_NOW), _vec(1.0)))

    assert _run(store.count(user_id=_USER, companion_id=_COMPANION)) == 1


def test_touch_accessed_sets_timestamp(store: SQLiteMemoryStore) -> None:
    _run(store.add(_memory("m1"), _vec(1.0)))
    later = _NOW + timedelta(hours=1)

    _run(store.touch_accessed(["m1"], at=later))

    loaded = _run(store.get("m1"))
    assert loaded is not None
    assert loaded.last_accessed_at == later


def test_touch_accessed_increments_the_access_counter(store: SQLiteMemoryStore) -> None:
    """``docs/06`` §4 的 ``access_frequency`` 需要次数，不只是"最近一次时间"。"""
    _run(store.add(_memory("m1"), _vec(1.0)))

    _run(store.touch_accessed(["m1"], at=_NOW))
    _run(store.touch_accessed(["m1"], at=_NOW))

    loaded = _run(store.get("m1"))
    assert loaded is not None
    assert loaded.access_count == 2


def test_touch_accessed_preserves_other_metadata(store: SQLiteMemoryStore) -> None:
    """自增 access_count 不能把 metadata 里的其他键冲掉。"""
    _run(store.add(_memory("m1", metadata={"repetition": 3}), _vec(1.0)))

    _run(store.touch_accessed(["m1"], at=_NOW))

    loaded = _run(store.get("m1"))
    assert loaded is not None
    assert loaded.repetition == 3
    assert loaded.access_count == 1


def test_touch_accessed_with_empty_input_is_a_noop(store: SQLiteMemoryStore) -> None:
    _run(store.touch_accessed([], at=_NOW))


# ── 运行时开关 ──────────────────────────────────────────


def test_flag_defaults_to_none(store: SQLiteMemoryStore) -> None:
    assert _run(store.get_flag("memory.enabled")) is None


def test_flag_round_trips_and_overwrites(store: SQLiteMemoryStore) -> None:
    _run(store.set_flag("memory.enabled", "false", at=_NOW))
    assert _run(store.get_flag("memory.enabled")) == "false"

    _run(store.set_flag("memory.enabled", "true", at=_NOW))
    assert _run(store.get_flag("memory.enabled")) == "true"


def test_flags_survive_reconnect(store: SQLiteMemoryStore, db_path: Path) -> None:
    """模拟进程重启：开关是运行时可改的，必须落库而不是留在内存里。"""
    _run(store.set_flag("memory.enabled", "false", at=_NOW))

    fresh = SQLiteMemoryStore(db_path, embedding_model="fake", embedding_dim=_DIM)
    assert _run(fresh.get_flag("memory.enabled")) == "false"
