"""``/v1/conversations`` —— 读历史与开新会话。

``GET /{id}/messages`` 的用途：验证"重启后历史还在"
（`docs/10_TESTING_AND_ACCEPTANCE.md` §4）。

``POST /v1/conversations``（M4 追加）：**结束当前会话并立刻开一段新的**。
没有它的话，一段被写坏的历史只能靠 60 分钟空闲超时才能脱身——
实测踩到过模型照抄历史里堆叠的降级回复。

未知会话由 ``ConversationService.load_history`` 抛
:class:`~astra.domain.errors.ConversationNotFoundError`，再由 ``app.py`` 注册的
全局处理器统一转成 **404**——这是客户端的 bug 或被删的数据，不该静默返回空列表。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from astra.api.dependencies import get_conversations
from astra.api.schemas import (
    ConversationBody,
    ConversationHistoryBody,
    ConversationListBody,
    ConversationSummaryBody,
    MessageBody,
)
from astra.application.conversation_service import ConversationService

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

ConversationsDep = Annotated[ConversationService, Depends(get_conversations)]

#: 列表里"最后一条消息"截多长——只够认出来是哪一段就行（完整内容走 /messages）
_PREVIEW_CHARS = 80


@router.post(
    "",
    response_model=ConversationBody,
    status_code=status.HTTP_201_CREATED,
    summary="开一段新会话（当前会话被置为 ended，不删除任何数据）",
)
async def create_conversation(conversations: ConversationsDep) -> ConversationBody:
    conversation = await conversations.start_new()

    return ConversationBody(
        conversation_id=conversation.id,
        started_at=conversation.started_at,
    )


@router.get(
    "",
    response_model=ConversationListBody,
    summary="最近的会话列表（含消息数与最后一条摘要；按最近有过消息倒序）",
)
async def list_conversations(
    conversations: ConversationsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConversationListBody:
    """Studio 的历史栏用它来列"过往的每一段"。

    只返回当前 ``(user_id, companion_id)`` 的会话——多人格时天然只看到这一位的。
    """
    summaries = await conversations.list_summaries(limit=limit)

    return ConversationListBody(
        conversations=[
            ConversationSummaryBody(
                conversation_id=summary.conversation.id,
                status=summary.conversation.status.value,
                started_at=summary.conversation.started_at,
                ended_at=summary.conversation.ended_at,
                message_count=summary.message_count,
                last_message_at=summary.last_message_at,
                last_message_role=(
                    summary.last_message.role.value if summary.last_message is not None else None
                ),
                last_message_preview=(
                    _preview(summary.last_message.content)
                    if summary.last_message is not None
                    else None
                ),
            )
            for summary in summaries
        ]
    )


def _preview(content: str) -> str:
    """列表里的一行摘要：换行压成空格，超长截断。"""
    flattened = " ".join(content.split())
    if len(flattened) <= _PREVIEW_CHARS:
        return flattened
    return flattened[:_PREVIEW_CHARS] + "…"


@router.get(
    "/{conversation_id}/messages",
    response_model=ConversationHistoryBody,
    summary="读取一段会话的全部消息（按 id 升序）",
)
async def read_messages(
    conversation_id: str, conversations: ConversationsDep
) -> ConversationHistoryBody:
    messages = await conversations.load_history(conversation_id)

    return ConversationHistoryBody(
        conversation_id=conversation_id,
        messages=[
            MessageBody(
                id=message.id,
                conversation_id=message.conversation_id,
                turn_id=message.turn_id,
                role=message.role.value,
                content=message.content,
                created_at=message.created_at,
                metadata=message.metadata,
            )
            for message in messages
        ],
    )
