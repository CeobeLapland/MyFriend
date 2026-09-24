"""``SQLiteStateStore`` 的单元测试（真实临时库）。

重点：往返保真、UPSERT 不产生第二行、两份状态在同一事务、``reset`` 后回到"没有记录"。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.domain.state import (
    EMOTION_DIMS,
    RELATIONSHIP_DIMS,
    EmotionState,
    RelationshipState,
    StateSnapshot,
    baseline_emotion,
    baseline_relationship,
)
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.db.state_repository import SQLiteStateStore

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_COMPANION = "astra"


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> StateSnapshot:
    params: dict[str, object] = {
        "emotion": baseline_emotion(),
        "relationship": baseline_relationship(),
        "emotion_updated_at": _NOW,
        "relationship_updated_at": _NOW,
    }
    params.update(overrides)
    return StateSnapshot(**params)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "astra.db"
    upgrade(path)
    return path


@pytest.fixture
def store(db_path: Path) -> SQLiteStateStore:
    return SQLiteStateStore(db_path)


def _row_counts(db_path: Path) -> tuple[int, int]:
    with open_connection(db_path) as conn:
        emotion = conn.execute("SELECT count(*) AS n FROM emotion_states").fetchone()["n"]
        relationship = conn.execute("SELECT count(*) AS n FROM relationship_states").fetchone()["n"]
    return int(emotion), int(relationship)


# ── 读取 ────────────────────────────────────────────────


def test_load_returns_none_when_there_is_no_record(store: SQLiteStateStore) -> None:
    """ "从没有过记录"与"记录是基线值"必须能区分——前者用基线兜底，后者是真实数据。"""
    assert _run(store.load(_COMPANION)) is None


def test_load_after_save_round_trips(store: SQLiteStateStore) -> None:
    emotion = EmotionState(
        happiness=0.91,
        energy=0.12,
        curiosity=0.33,
        social_need=0.44,
        stress=0.05,
        embarrassment=0.66,
        excitement=0.77,
    )
    relationship = RelationshipState(
        familiarity=0.8, trust=0.7, affection=0.6, reliance=0.5, conflict=0.4
    )
    later = _NOW + timedelta(hours=3)
    _run(
        store.save(
            _COMPANION,
            _snapshot(
                emotion=emotion,
                relationship=relationship,
                emotion_updated_at=later,
                relationship_updated_at=later,
            ),
        )
    )

    loaded = _run(store.load(_COMPANION))

    assert loaded is not None
    assert loaded.emotion == emotion
    assert loaded.relationship == relationship
    assert loaded.emotion_updated_at == later
    assert loaded.relationship_updated_at == later


def test_timestamps_are_timezone_aware_after_round_trip(store: SQLiteStateStore) -> None:
    _run(store.save(_COMPANION, _snapshot()))

    loaded = _run(store.load(_COMPANION))

    assert loaded is not None
    assert loaded.emotion_updated_at.tzinfo is not None
    assert loaded.emotion_updated_at == _NOW


def test_load_is_scoped_by_companion(store: SQLiteStateStore) -> None:
    _run(store.save(_COMPANION, _snapshot()))

    assert _run(store.load("someone-else")) is None


# ── 写入 ────────────────────────────────────────────────


def test_save_twice_upserts_instead_of_inserting_a_second_row(
    store: SQLiteStateStore, db_path: Path
) -> None:
    """状态是每个 companion 一行——重复保存必须覆盖，不能堆行。"""
    _run(store.save(_COMPANION, _snapshot()))
    _run(
        store.save(
            _COMPANION,
            _snapshot(emotion=baseline_emotion().pushed_toward({"happiness": 1.0}, strength=0.3)),
        )
    )

    assert _row_counts(db_path) == (1, 1)
    loaded = _run(store.load(_COMPANION))
    assert loaded is not None
    assert loaded.emotion.happiness == pytest.approx(0.685)


def test_save_writes_both_tables(store: SQLiteStateStore, db_path: Path) -> None:
    _run(store.save(_COMPANION, _snapshot()))

    assert _row_counts(db_path) == (1, 1)


def test_save_keeps_the_two_states_independent(store: SQLiteStateStore) -> None:
    _run(store.save(_COMPANION, _snapshot()))
    later = _NOW + timedelta(days=1)
    _run(
        store.save(
            _COMPANION,
            _snapshot(
                relationship=baseline_relationship().with_deltas({"trust": 0.4}),
                relationship_updated_at=later,
            ),
        )
    )

    loaded = _run(store.load(_COMPANION))

    assert loaded is not None
    assert loaded.relationship.trust == pytest.approx(0.4)
    assert loaded.emotion_updated_at == _NOW  # 情绪的时间戳没被动过
    assert loaded.relationship_updated_at == later


def test_save_stores_every_dimension(store: SQLiteStateStore, db_path: Path) -> None:
    """逐列断言一次：漏写一个维度是"看起来正常但状态永远不变"的经典 bug。"""
    _run(store.save(_COMPANION, _snapshot()))

    with open_connection(db_path) as conn:
        emotion_row = conn.execute("SELECT * FROM emotion_states").fetchone()
        relationship_row = conn.execute("SELECT * FROM relationship_states").fetchone()

    assert all(emotion_row[name] is not None for name in EMOTION_DIMS)
    assert all(relationship_row[name] is not None for name in RELATIONSHIP_DIMS)


# ── reset ───────────────────────────────────────────────


def test_reset_removes_both_rows(store: SQLiteStateStore, db_path: Path) -> None:
    _run(store.save(_COMPANION, _snapshot()))

    _run(store.reset(_COMPANION))

    assert _row_counts(db_path) == (0, 0)
    assert _run(store.load(_COMPANION)) is None


def test_reset_does_not_touch_other_companions(store: SQLiteStateStore) -> None:
    _run(store.save(_COMPANION, _snapshot()))
    _run(store.save("other", _snapshot()))

    _run(store.reset(_COMPANION))

    assert _run(store.load(_COMPANION)) is None
    assert _run(store.load("other")) is not None


def test_reset_on_empty_store_is_a_noop(store: SQLiteStateStore) -> None:
    _run(store.reset(_COMPANION))


# ── 缺失关系行时的兜底 ──────────────────────────────────


def test_missing_relationship_row_falls_back_to_baseline(
    store: SQLiteStateStore, db_path: Path
) -> None:
    """关系行缺失时不能把整份快照判成"没有"——丢掉已累积的情绪更糟。"""
    _run(
        store.save(
            _COMPANION,
            _snapshot(emotion=baseline_emotion().pushed_toward({"happiness": 1.0}, strength=0.3)),
        )
    )
    with open_connection(db_path) as conn:
        conn.execute("DELETE FROM relationship_states")

    loaded = _run(store.load(_COMPANION))

    assert loaded is not None
    assert loaded.emotion.happiness == pytest.approx(0.685)
    assert loaded.relationship == baseline_relationship()
