"""``POST /v1/chat/turns`` —— 同步跑完整个 turn。

**为什么单独一个端点而不是给 ``/v1/chat/messages`` 加 ``?wait=true``**：
阻塞与非阻塞是两种契约（状态码、超时、失败语义都不同），塞进一个路径会让
响应模型变成联合类型。这个端点是 M1 的验收工具与调试入口（ADR 0016）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from astra.api.dependencies import get_orchestrator
from astra.api.schemas import (
    ActionBody,
    AssistantResponseBody,
    EmotionBody,
    FailureBody,
    TimingsBody,
    TurnRequestBody,
    TurnResponse,
    VoiceBody,
)
from astra.application.orchestrator import Orchestrator, TurnRequest, TurnResult

router = APIRouter(prefix="/v1/chat", tags=["chat"])

OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]


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
    )
