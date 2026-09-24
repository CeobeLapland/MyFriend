"""会话与消息的 SQLite 仓储（ADR 0004）。

- 内部是**同步**实现，方法入口用 ``asyncio.to_thread`` 脱离事件循环。
- **排序一律 ``ORDER BY id``**：实测本机时钟分辨率 15.625ms，同一 turn 的
  user 与 assistant 消息 ``created_at`` 可能完全相同（ADR 0012）。
- 时间戳按 ISO-8601 UTC 字符串存取，不使用 ``detect_types`` 自动转换。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from astra.domain.conversation import Conversation, ConversationStatus, Message, Role
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.timestamps import from_iso as _from_iso
from astra.infrastructure.db.timestamps import to_iso as _to_iso

_CONVERSATION_COLUMNS = "id, user_id, companion_id, started_at, ended_at, summary, status"
_MESSAGE_COLUMNS = "id, conversation_id, turn_id, role, content, metadata_json, created_at"


class SQLiteConversationRepository:
    """:class:`astra.domain.ports.ConversationRepository` 的 SQLite 实现。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    # ── 会话 ────────────────────────────────────────────

    async def create(self, conversation: Conversation) -> None:
        await asyncio.to_thread(self._create_sync, conversation)

    async def get(self, conversation_id: str) -> Conversation | None:
        return await asyncio.to_thread(self._get_sync, conversation_id)

    async def find_active(self, *, user_id: str, companion_id: str) -> Conversation | None:
        return await asyncio.to_thread(self._find_active_sync, user_id, companion_id)

    async def end(self, conversation_id: str, *, ended_at: datetime) -> None:
        await asyncio.to_thread(self._end_sync, conversation_id, ended_at)

    async def last_message_at(self, conversation_id: str) -> datetime | None:
        return await asyncio.to_thread(self._last_message_at_sync, conversation_id)

    # ── 消息 ────────────────────────────────────────────

    async def append_messages(self, messages: Sequence[Message]) -> tuple[str, ...]:
        """幂等写入。已存在同 ``(turn_id, role)`` 的消息会被跳过。"""
        return await asyncio.to_thread(self._append_messages_sync, tuple(messages))

    async def load_messages(self, conversation_id: str) -> tuple[Message, ...]:
        return await asyncio.to_thread(self._load_messages_sync, conversation_id)

    # ── 同步实现 ────────────────────────────────────────

    def _create_sync(self, conversation: Conversation) -> None:
        with open_connection(self._db_path) as conn:
            conn.execute(
                f"INSERT INTO conversations ({_CONVERSATION_COLUMNS}) "  # noqa: S608 - 列名是模块常量
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.user_id,
                    conversation.companion_id,
                    _to_iso(conversation.started_at),
                    _to_iso(conversation.ended_at) if conversation.ended_at else None,
                    conversation.summary,
                    conversation.status.value,
                ),
            )

    def _get_sync(self, conversation_id: str) -> Conversation | None:
        with open_connection(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",  # noqa: S608
                (conversation_id,),
            ).fetchone()

        return _row_to_conversation(row) if row is not None else None

    def _find_active_sync(self, user_id: str, companion_id: str) -> Conversation | None:
        with open_connection(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "  # noqa: S608
                "WHERE user_id = ? AND companion_id = ? AND status = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, companion_id, ConversationStatus.ACTIVE.value),
            ).fetchone()

        return _row_to_conversation(row) if row is not None else None

    def _end_sync(self, conversation_id: str, ended_at: datetime) -> None:
        with open_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, ended_at = ? WHERE id = ?",
                (ConversationStatus.ENDED.value, _to_iso(ended_at), conversation_id),
            )

    def _last_message_at_sync(self, conversation_id: str) -> datetime | None:
        with open_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT created_at FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()

        return _from_iso(str(row["created_at"])) if row is not None else None

    def _append_messages_sync(self, messages: Sequence[Message]) -> tuple[str, ...]:
        inserted: list[str] = []

        with open_connection(self._db_path) as conn:
            conn.execute("BEGIN")
            try:
                for message in messages:
                    if self._already_stored(conn, message):
                        continue
                    conn.execute(
                        f"INSERT INTO messages ({_MESSAGE_COLUMNS}) "  # noqa: S608 - 列名是模块常量
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            message.id,
                            message.conversation_id,
                            message.turn_id,
                            message.role.value,
                            message.content,
                            json.dumps(message.metadata, ensure_ascii=False),
                            _to_iso(message.created_at),
                        ),
                    )
                    inserted.append(message.id)
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        return tuple(inserted)

    @staticmethod
    def _already_stored(conn: sqlite3.Connection, message: Message) -> bool:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE turn_id = ? AND role = ? LIMIT 1",
            (message.turn_id, message.role.value),
        ).fetchone()
        return row is not None

    def _load_messages_sync(self, conversation_id: str) -> tuple[Message, ...]:
        with open_connection(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages "  # noqa: S608 - 列名是模块常量
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()

        return tuple(_row_to_message(row) for row in rows)


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    ended_at = row["ended_at"]
    return Conversation(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        companion_id=str(row["companion_id"]),
        started_at=_from_iso(str(row["started_at"])),
        status=ConversationStatus(str(row["status"])),
        ended_at=_from_iso(str(ended_at)) if ended_at else None,
        summary=str(row["summary"]) if row["summary"] is not None else None,
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    raw_metadata = str(row["metadata_json"])
    return Message(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        turn_id=str(row["turn_id"]),
        role=Role(str(row["role"])),
        content=str(row["content"]),
        created_at=_from_iso(str(row["created_at"])),
        metadata=json.loads(raw_metadata) if raw_metadata else {},
    )
