"""``WS /ws/v1/session/{session_id}`` —— 实时事件流。

客户端发：

```json
{"type": "input.user_text", "version": 1, "payload": {"text": "你好"}}
```

服务端推 ``turn.started`` … ``turn.completed``（或 ``turn.failed``）。

**协议错误不关连接**：回一条 ``error.protocol``，一个手滑不该让用户掉线。

**turn 是内联 await 的**：这样同一连接上的消息天然保序，也不会出现"客户端以为
上一条还没完就又发一条"。跨连接的顺序由 Orchestrator 的每会话锁保证。
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from astra.api.connection_manager import ConnectionManager
from astra.api.dependencies import get_connection_manager, get_orchestrator
from astra.application.orchestrator import Orchestrator, TurnRequest
from astra.domain.events import ClientFrameType, EventType, make_event
from astra.domain.ids import new_id

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
ConnectionManagerDep = Annotated[ConnectionManager, Depends(get_connection_manager)]


class ClientFrame(BaseModel):
    """客户端帧的信封。"""

    model_config = ConfigDict(extra="ignore")

    type: str
    version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)


class UserTextPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)
    conversation_id: str | None = None


@router.websocket("/ws/v1/session/{session_id}")
async def session_socket(
    websocket: WebSocket,
    session_id: str,
    orchestrator: OrchestratorDep,
    manager: ConnectionManagerDep,
) -> None:
    connection = await manager.connect(session_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_frame(websocket, orchestrator, session_id, raw)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(connection)


async def _handle_frame(
    websocket: WebSocket,
    orchestrator: Orchestrator,
    session_id: str,
    raw: str,
) -> None:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        await _send_protocol_error(websocket, "frame is not valid JSON")
        return

    try:
        frame = ClientFrame.model_validate(decoded)
    except ValidationError as exc:
        await _send_protocol_error(
            websocket, f"frame failed validation: {exc.error_count()} error(s)"
        )
        return

    if frame.type != ClientFrameType.INPUT_USER_TEXT.value:
        await _send_protocol_error(websocket, f"unsupported frame type: {frame.type}")
        return

    try:
        payload = UserTextPayload.model_validate(frame.payload)
    except ValidationError as exc:
        await _send_protocol_error(
            websocket, f"payload failed validation: {exc.error_count()} error(s)"
        )
        return

    await orchestrator.run_turn(
        TurnRequest(
            text=payload.text,
            conversation_id=payload.conversation_id,
            session_id=session_id,
        )
    )


async def _send_protocol_error(websocket: WebSocket, message: str) -> None:
    # 无 turn 上下文的事件用 event_id 作为 correlation_id（ADR 0012）
    event_id = new_id()
    event = make_event(
        EventType.ERROR_PROTOCOL,
        correlation_id=event_id,
        payload={"code": "invalid_frame", "message": message},
        event_id=event_id,
    )

    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.send_json(event.to_dict())
