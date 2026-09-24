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

from fastapi import APIRouter, Depends, status

from astra.api.dependencies import get_conversations
from astra.api.schemas import ConversationBody, ConversationHistoryBody, MessageBody
from astra.application.conversation_service import ConversationService

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

ConversationsDep = Annotated[ConversationService, Depends(get_conversations)]


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
