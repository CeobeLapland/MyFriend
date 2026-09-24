"""``POST /v1/chat/turns`` —— 同步跑完整个 turn。

**为什么单独一个端点而不是给 ``/v1/chat/messages`` 加 ``?wait=true``**：
阻塞与非阻塞是两种契约（状态码、超时、失败语义都不同），塞进一个路径会让
响应模型变成联合类型。这个端点是 M1 的验收工具与调试入口（ADR 0016）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from astra.api.dependencies import get_conversations, get_orchestrator
from astra.api.schemas import (
    AcceptedBody,
    ActionBody,
    AssistantResponseBody,
    EmotionBody,
    FailureBody,
    TimingsBody,
    TurnRequestBody,
    TurnResponse,
    VoiceBody,
)
from astra.application.conversation_service import ConversationService
from astra.application.orchestrator import Orchestrator, TurnRequest, TurnResult
from astra.domain.ids import new_id

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chat", tags=["chat"])

OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
ConversationsDep = Annotated[ConversationService, Depends(get_conversations)]

#: 火后不管的后台 turn 任务。
#: 持引用是为了避免 asyncio 在任务完成前把它回收掉（官方推荐做法）。
_BACKGROUND_TURNS: set[asyncio.Task[TurnResult]] = set()


@router.post(
    "/turns",
    response_model=TurnResponse,
    summary="同步执行一次对话（返回完整结构化回复）",
)
async def create_turn(body: TurnRequestBody, orchestrator: OrchestratorDep) -> TurnResponse:
    result = await orchestrator.run_turn(
        TurnRequest(text=body.content, conversation_id=body.conversation_id)
    )
    return _to_response(result)


@router.post(
    "/messages",
    response_model=AcceptedBody,
    status_code=status.HTTP_202_ACCEPTED,
    summary="投递一条消息，立即返回；结果通过 WebSocket 推送",
)
async def accept_message(
    body: TurnRequestBody,
    orchestrator: OrchestratorDep,
    conversations: ConversationsDep,
) -> AcceptedBody:
    """立即返回 202，真正的 turn 在后台跑。

    这里先解析出 ``conversation_id`` 并分配 ``turn_id``，是为了让客户端**立刻**
    知道该监听哪一段会话——否则它得等 ``turn.started`` 才知道。
    """
    resolved = await conversations.resolve_or_create(body.conversation_id)
    turn_id = new_id()

    task = asyncio.create_task(
        orchestrator.run_turn(
            TurnRequest(
                text=body.content,
                conversation_id=resolved.conversation.id,
                turn_id=turn_id,
            )
        )
    )
    _BACKGROUND_TURNS.add(task)
    task.add_done_callback(_BACKGROUND_TURNS.discard)

    return AcceptedBody(turn_id=turn_id, conversation_id=resolved.conversation.id)


def _to_response(result: TurnResult) -> TurnResponse:
    return TurnResponse(
        turn_id=result.turn_id,
        conversation_id=result.conversation_id,
        status="ok" if result.succeeded else "failed",
        reply=result.reply_text,
        response=(
            AssistantResponseBody(
                reply=result.response.reply,
                emotion=EmotionBody(
                    primary=result.response.emotion.primary,
                    intensity=result.response.emotion.intensity,
                ),
                action=ActionBody(
                    name=result.response.action.name,
                    intensity=result.response.action.intensity,
                ),
                voice=VoiceBody(
                    style=result.response.voice.style,
                    speed=result.response.voice.speed,
                    pitch=result.response.voice.pitch,
                ),
            )
            if result.response is not None
            else None
        ),
        failure=(
            FailureBody(
                code=result.failure.code.value,
                message=result.failure.message,
                recoverable=result.failure.recoverable,
            )
            if result.failure is not None
            else None
        ),
        model=result.model,
        retries=result.retries,
        degraded=result.degraded,
        stubs_used=sorted(result.stubs_used),
        timings=TimingsBody(
            total_ms=result.timings.total_ms,
            llm_ms=result.timings.llm_ms,
        ),
        memories_retrieved=result.memories_retrieved,
        memories_created=result.memories_created,
    )
