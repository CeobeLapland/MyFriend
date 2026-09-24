"""状态（情绪 / 关系）的 SQLite 仓储（ADR 0004 / 0020）。

**两张表、一个事务**：``emotion_states`` 与 ``relationship_states`` 都以
``companion_id`` 为主键，各自只有一行。写入用 UPSERT，两份状态在**同一个事务**里
落库——否则一次崩溃可能留下"情绪更新了、关系没更新"的半截状态。

**仓储只负责搬运**：衰减、增量、clamp 全在 ``domain/state.py`` 与
``StateService`` 里。这里不做任何业务判断，只做序列化与 UPSERT。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from astra.domain.state import (
    EMOTION_DIMS,
    RELATIONSHIP_DIMS,
    EmotionState,
    RelationshipState,
    StateSnapshot,
    baseline_relationship,
)
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.timestamps import from_iso as _from_iso
from astra.infrastructure.db.timestamps import to_iso as _to_iso

_EMOTION_COLUMNS = ", ".join(("companion_id", *EMOTION_DIMS, "updated_at"))
_RELATIONSHIP_COLUMNS = ", ".join(("companion_id", *RELATIONSHIP_DIMS, "updated_at"))
_EMOTION_PLACEHOLDERS = ", ".join("?" for _ in range(len(EMOTION_DIMS) + 2))
_RELATIONSHIP_PLACEHOLDERS = ", ".join("?" for _ in range(len(RELATIONSHIP_DIMS) + 2))


class SQLiteStateStore:
    """:class:`astra.domain.ports.StateStore` 的 SQLite 实现。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def load(self, companion_id: str) -> StateSnapshot | None:
        return await asyncio.to_thread(self._load_sync, companion_id)

    async def save(self, companion_id: str, snapshot: StateSnapshot) -> None:
        await asyncio.to_thread(self._save_sync, companion_id, snapshot)

    async def reset(self, companion_id: str) -> None:
        await asyncio.to_thread(self._reset_sync, companion_id)

    # ── 同步实现 ────────────────────────────────────────

    def _load_sync(self, companion_id: str) -> StateSnapshot | None:
        with open_connection(self._db_path) as conn:
            emotion_row = conn.execute(
                f"SELECT {_EMOTION_COLUMNS} FROM emotion_states WHERE companion_id = ?",  # noqa: S608
                (companion_id,),
            ).fetchone()
            if emotion_row is None:
                # 情绪行是"这个 companion 有没有状态记录"的判定依据：
                # 写入时两张表在同一事务里，所以缺它就是真的从没有过记录。
                return None

            relationship_row = conn.execute(
                f"SELECT {_RELATIONSHIP_COLUMNS} FROM relationship_states "  # noqa: S608
                "WHERE companion_id = ?",
                (companion_id,),
            ).fetchone()

        # 关系行理论上一并存在；真缺了就用基线兜底，而不是把整份快照判成"没有"——
        # 丢掉已经累积的情绪状态比丢掉关系状态更糟。
        relationship = (
            _row_to_relationship(relationship_row)
            if relationship_row is not None
            else baseline_relationship()
        )
        relationship_at = (
            _from_iso(str(relationship_row["updated_at"]))
            if relationship_row is not None
            else _from_iso(str(emotion_row["updated_at"]))
        )

        return StateSnapshot(
            emotion=_row_to_emotion(emotion_row),
            relationship=relationship,
            emotion_updated_at=_from_iso(str(emotion_row["updated_at"])),
            relationship_updated_at=relationship_at,
        )

    def _save_sync(self, companion_id: str, snapshot: StateSnapshot) -> None:
        emotion_values = _emotion_values(companion_id, snapshot)
        relationship_values = _relationship_values(companion_id, snapshot)

        with open_connection(self._db_path) as conn:
            conn.execute("BEGIN")
            conn.execute(
                f"INSERT INTO emotion_states ({_EMOTION_COLUMNS}) "  # noqa: S608 - 列名是模块常量
                f"VALUES ({_EMOTION_PLACEHOLDERS}) "
                f"ON CONFLICT(companion_id) DO UPDATE SET {_emotion_update_clause()}",
                emotion_values,
            )
            conn.execute(
                f"INSERT INTO relationship_states ({_RELATIONSHIP_COLUMNS}) "  # noqa: S608
                f"VALUES ({_RELATIONSHIP_PLACEHOLDERS}) "
                f"ON CONFLICT(companion_id) DO UPDATE SET {_relationship_update_clause()}",
                relationship_values,
            )
            conn.execute("COMMIT")

    def _reset_sync(self, companion_id: str) -> None:
        with open_connection(self._db_path) as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM emotion_states WHERE companion_id = ?", (companion_id,))
            conn.execute("DELETE FROM relationship_states WHERE companion_id = ?", (companion_id,))
            conn.execute("COMMIT")


def _emotion_update_clause() -> str:
    assignments = [f"{name} = excluded.{name}" for name in (*EMOTION_DIMS, "updated_at")]
    return ", ".join(assignments)


def _relationship_update_clause() -> str:
    assignments = [f"{name} = excluded.{name}" for name in (*RELATIONSHIP_DIMS, "updated_at")]
    return ", ".join(assignments)


def _emotion_values(companion_id: str, snapshot: StateSnapshot) -> tuple[object, ...]:
    return (
        companion_id,
        *(getattr(snapshot.emotion, name) for name in EMOTION_DIMS),
        _to_iso(snapshot.emotion_updated_at),
    )


def _relationship_values(companion_id: str, snapshot: StateSnapshot) -> tuple[object, ...]:
    return (
        companion_id,
        *(getattr(snapshot.relationship, name) for name in RELATIONSHIP_DIMS),
        _to_iso(snapshot.relationship_updated_at),
    )


def _row_to_emotion(row: sqlite3.Row) -> EmotionState:
    return EmotionState(**{name: float(row[name]) for name in EMOTION_DIMS})


def _row_to_relationship(row: sqlite3.Row) -> RelationshipState:
    return RelationshipState(**{name: float(row[name]) for name in RELATIONSHIP_DIMS})
