"""``GET /v1/conversations/{id}/messages`` —— 读取历史。

M1 的用途：验证"重启后历史还在"（`docs/10_TESTING_AND_ACCEPTANCE.md` §4）。

未知会话由 ``ConversationService.load_history`` 抛
:class:`~astra.domain.errors.ConversationNotFoundError`，再由 ``app.py`` 注册的
全局处理器统一转成 **404**——这是客户端的 bug 或被删的数据，不该静默返回空列表。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from astra.api.dependencies import get_conversations
from astra.api.schemas import ConversationHistoryBody, MessageBody
from astra.application.conversation_service import ConversationService

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

ConversationsDep = Annotated[ConversationService, Depends(get_conversations)]


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
