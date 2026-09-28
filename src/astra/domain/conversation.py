"""会话与消息的领域值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    """消息角色。

    ``messages`` 表的 CHECK 约束还允许 ``tool``，那是 M6 Tool Registry 的预留值，
    M1 不产生也不消费它。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class Conversation:
    """一次持久的对话线程。

    注意与 ``session_id`` 的区别：session 是 WebSocket 传输层的临时概念，
    不落库（ADR 0012）。
    """

    id: str
    user_id: str
    companion_id: str
    started_at: datetime
    status: ConversationStatus = ConversationStatus.ACTIVE
    ended_at: datetime | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """会话列表里的一行（Studio 的历史栏用）。

    **不是"会话 + 全部消息"**：只带计数与最后一条消息，用来画列表；
    完整历史仍走 ``load_messages``。
    """

    conversation: Conversation
    message_count: int
    last_message_at: datetime | None
    last_message: Message | None


@dataclass(frozen=True, slots=True)
class Message:
    """一条已持久化的消息。"""

    id: str
    conversation_id: str
    turn_id: str
    role: Role
    content: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
