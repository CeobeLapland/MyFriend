"""``GET /v1/conversations/{id}/messages`` —— 读取历史。

M1 的用途：验证"重启后历史还在"（`docs/10_TESTING_AND_ACCEPTANCE.md` §4）。
未知会话返回 **404**——这是客户端的 bug 或被删的数据，不该静默返回空列表。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from astra.api.dependencies import get_conversations
from astra.api.schemas import ConversationHistoryBody, MessageBody
from astra.application.conversation_service import ConversationService
from astra.domain.errors import ConversationNotFoundError

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
    try:
        messages = await conversations.load_history(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code.value, "message": exc.message},
        ) from exc

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
