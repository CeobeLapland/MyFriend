"""会话解析与 turn 持久化。

``resolve_or_create`` 的规则（ADR 0012）：

1. 显式给了 ``conversation_id``，且存在、属于 ``(user_id, companion_id)``、
   状态为 ``active`` → 用它
2. 显式给了但不存在 / 已结束 / 不属于该用户 → **拒绝**（``ConversationNotFoundError``）。
   **不静默新建**——客户端传未知 id 是 bug 或被删的数据，静默新建会掩盖它
3. 未给 → 取该角色最近一个 ``active`` 会话；若其最后一条消息早于
   ``idle_timeout`` → 把它置为 ``ended`` 并新建一个

**为什么需要空闲切分**：否则所有对话永远堆在同一个 conversation 里，M2 的上下文
窗口只能机械截断；有了边界，M2 才能对已结束的会话做摘要（L1 episodes）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from astra.domain.conversation import (
    Conversation,
    ConversationStatus,
    ConversationSummary,
    Message,
    Role,
)
from astra.domain.errors import ConversationNotFoundError
from astra.domain.ids import new_id
from astra.domain.ports import ConversationRepository


@dataclass(frozen=True, slots=True)
class ResolvedConversation:
    conversation: Conversation
    #: 是否是本次新建的（用于发 ``conversation.created`` 事件）
    created: bool


class ConversationService:
    """会话生命周期的唯一入口。HTTP 与 WebSocket 共用同一套解析规则。"""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        user_id: str,
        companion_id: str,
        idle_timeout: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        self._companion_id = companion_id
        self._idle_timeout = idle_timeout
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))

    async def resolve_or_create(self, conversation_id: str | None) -> ResolvedConversation:
        """解析出本次 turn 所属的会话。"""
        now = self._clock()

        if conversation_id is not None:
            return ResolvedConversation(
                conversation=await self._require_active(conversation_id),
                created=False,
            )

        existing = await self._repository.find_active(
            user_id=self._user_id,
            companion_id=self._companion_id,
        )

        if existing is not None and not await self._is_idle(existing, now):
            return ResolvedConversation(conversation=existing, created=False)

        if existing is not None:
            await self._repository.end(existing.id, ended_at=now)

        return ResolvedConversation(
            conversation=await self._create_active(now),
            created=True,
        )

    async def start_new(self) -> Conversation:
        """结束当前 active 会话（如果有）并**立即**开一段新的。

        **为什么需要它**：``resolve_or_create`` 只在**空闲超时**后才切分会话，
        所以一段被写坏的历史会把模型带偏很久，而用户没有任何办法脱身。
        实测踩到过：早期 ``llm_provider=none`` 时产生的降级回复
        （"我现在还没有接上语言模型，所以没法回话"）被当成正常 assistant 消息
        写进历史，堆了十几条之后，4B 模型开始**照抄**那句最频繁的回复。

        显式开新会话本来就是客户端该有的能力——Unity 端也需要"开一段新对话"。

        **不删除任何数据**：旧会话只是被置为 ``ended``，历史、记忆、状态都还在
        （``GET /v1/conversations/{id}/messages`` 仍可读）。
        """
        now = self._clock()
        existing = await self._repository.find_active(
            user_id=self._user_id,
            companion_id=self._companion_id,
        )
        if existing is not None:
            await self._repository.end(existing.id, ended_at=now)

        return await self._create_active(now)

    async def _create_active(self, now: datetime) -> Conversation:
        conversation = Conversation(
            id=new_id(),
            user_id=self._user_id,
            companion_id=self._companion_id,
            started_at=now,
            status=ConversationStatus.ACTIVE,
        )
        await self._repository.create(conversation)
        return conversation

    async def list_summaries(self, *, limit: int = 50) -> tuple[ConversationSummary, ...]:
        """最近的会话列表（Studio 的历史栏用）。

        **按"最近有过消息"倒序**，只带计数与最后一条消息——完整历史仍走
        :meth:`load_history`。多人格时这里天然只返回当前 ``companion_id`` 的会话。
        """
        return await self._repository.list_summaries(
            user_id=self._user_id,
            companion_id=self._companion_id,
            limit=limit,
        )

    async def load_history(self, conversation_id: str) -> tuple[Message, ...]:
        """读取该会话的全部消息（按 id 升序）。

        未知会话抛 :class:`ConversationNotFoundError`——静默返回空列表会把
        "客户端传了错 id"伪装成"这段对话是空的"。

        M1 不加 limit：空闲 60 分钟就切分会话，单个 conversation 不会很长。
        真需要时在仓储层加 ``LIMIT``（``ORDER BY id DESC LIMIT n`` 再反转）。
        """
        if await self._repository.get(conversation_id) is None:
            raise ConversationNotFoundError(f"conversation not found: {conversation_id}")

        return await self._repository.load_messages(conversation_id)

    async def append_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_text: str,
        reply_text: str,
        reply_metadata: Mapping[str, Any],
        correlation_id: str,
    ) -> tuple[Message, Message]:
        """在一个事务里写入本次 turn 的 user 与 assistant 两条消息。

        消息 id 在此处生成，**先 user 后 assistant**——ULID 在同一毫秒内单调递增
        （ADR 0012），所以 ``ORDER BY id`` 一定等于插入顺序。

        幂等：同一 ``(turn_id, role)`` 已存在则跳过（``docs/04`` §6）。
        """
        now = self._clock()
        metadata = {"correlation_id": correlation_id, **reply_metadata}

        user_message = Message(
            id=new_id(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            role=Role.USER,
            content=user_text,
            created_at=now,
            metadata={"correlation_id": correlation_id},
        )
        assistant_message = Message(
            id=new_id(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            role=Role.ASSISTANT,
            content=reply_text,
            created_at=now,
            metadata=metadata,
        )

        await self._repository.append_messages((user_message, assistant_message))

        return user_message, assistant_message

    # ── 内部 ────────────────────────────────────────────

    async def _require_active(self, conversation_id: str) -> Conversation:
        conversation = await self._repository.get(conversation_id)

        if conversation is None:
            raise ConversationNotFoundError(
                f"conversation not found: {conversation_id}",
            )
        if conversation.status is not ConversationStatus.ACTIVE:
            raise ConversationNotFoundError(
                f"conversation is not active: {conversation_id}",
            )
        if conversation.user_id != self._user_id or conversation.companion_id != self._companion_id:
            raise ConversationNotFoundError(
                f"conversation belongs to another user or companion: {conversation_id}",
            )

        return conversation

    async def _is_idle(self, conversation: Conversation, now: datetime) -> bool:
        last_activity = await self._repository.last_message_at(conversation.id)
        reference = last_activity if last_activity is not None else conversation.started_at
        return now - reference >= self._idle_timeout
