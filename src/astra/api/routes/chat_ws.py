"""``WS /ws/v1/session/{session_id}`` —— 实时事件流。

**客户端发 JSON 文本帧**：

```json
{"type": "input.user_text", "version": 1, "payload": {"text": "你好"}}
```

**客户端也可以发二进制帧**（M4）：音频字节。它必须紧跟一条
``input.audio.started``（声明格式），直到 ``input.audio.stopped`` 为止。
没有上下文时收到二进制帧 → ``error.protocol``（只报一次），**不关连接**。

服务端推 ``turn.started`` … ``turn.completed``（或 ``turn.failed`` /
``turn.interrupted``），语音模式下中间还夹 ``tts.*`` 事件与音频二进制帧。

**协议错误不关连接**：回一条 ``error.protocol``，一个手滑不该让用户掉线。

**M4 起 turn 跑在后台 task 里**，接收循环不再被 turn 阻塞。这是 barge-in 的前提：
能被打断的前提是"还能收到新帧"。代价是同一连接内的消息不再天然保序——
顺序由 Orchestrator 的每会话锁 + 取消前先 await 旧 task 退出保证。

**断线不取消正在跑的 turn**（M1b 的既有契约）：用户关掉页面不该让她的话说一半
就不落库。所以 turn task 会被"放生"到一个模块级集合里，跑完为止。

**所有出站帧都走事件总线**（包括 ``error.protocol``）。M4 之前协议错误是直接
``websocket.send_json`` 的，而那会与 ConnectionManager 的 sender task **并发写同一个
socket**——音频帧一多，两个协程在 ``await`` 点交错就可能把帧写坏。
统一走总线后，每条连接只有一个发送者。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from astra.api.connection_manager import ConnectionManager
from astra.api.dependencies import (
    get_app_settings,
    get_connection_manager,
    get_event_bus,
    get_orchestrator,
    get_stt_provider,
)
from astra.application.event_bus import EventBus
from astra.application.orchestrator import Orchestrator, TurnRequest, TurnResult
from astra.domain.audio import AudioEncoding, AudioFormat
from astra.domain.errors import AstraError
from astra.domain.events import ClientFrameType, EventType, make_event
from astra.domain.ids import new_id
from astra.domain.ports import STTProvider
from astra.infrastructure.audio.vad import EnergyVad, VadConfig
from astra.settings import Settings

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
ConnectionManagerDep = Annotated[ConnectionManager, Depends(get_connection_manager)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
STTDep = Annotated[STTProvider | None, Depends(get_stt_provider)]

#: 被放生的 turn task（客户端已断开但 turn 还要跑完）。
#: 持引用是为了避免 asyncio 在任务完成前把它回收掉。
_ORPHANED_TURNS: set[asyncio.Task[TurnResult]] = set()


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


class AudioStartedPayload(BaseModel):
    """``input.audio.started`` 的 payload。字段名就是协议字段名。"""

    model_config = ConfigDict(extra="ignore")

    encoding: AudioEncoding = "pcm16"
    sample_rate: int
    channels: int = 1
    conversation_id: str | None = None


class VoiceModePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool


@dataclass(slots=True)
class _AudioBuffer:
    """一段正在采集的 utterance。"""

    format: AudioFormat
    correlation_id: str
    conversation_id: str | None
    vad: EnergyVad
    #: 超过它就截断（防呆：客户端 bug 不该把内存吃满）
    max_bytes: int
    chunks: list[bytes] = field(default_factory=list)
    byte_count: int = 0


@dataclass(slots=True)
class _SessionState:
    """每条连接一份。"""

    voice_mode: bool = False
    audio: _AudioBuffer | None = None
    turn_task: asyncio.Task[TurnResult] | None = None
    #: 已经为"没有 ``input.audio.started`` 的二进制帧"报过一次协议错误。
    #: **报一次就够**：VAD 自动断句之后客户端往往还会多送几帧（它还没收到
    #: ``audio.input.stopped``），每帧回一条 ``error.protocol`` 会把连接刷屏。
    orphan_reported: bool = False


@router.websocket("/ws/v1/session/{session_id}")
async def session_socket(
    websocket: WebSocket,
    session_id: str,
    orchestrator: OrchestratorDep,
    manager: ConnectionManagerDep,
    settings: SettingsDep,
    bus: EventBusDep,
    stt: STTDep,
) -> None:
    connection = await manager.connect(session_id, websocket)
    state = _SessionState()

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                await _handle_binary(state, settings, bus, stt, orchestrator, session_id, data)
                continue

            text = message.get("text")
            if text is not None:
                await _handle_text(state, settings, bus, stt, orchestrator, session_id, text)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # 断开之后再调 receive() 会抛 RuntimeError；语义等同于断开
        pass
    finally:
        # **刻意不取消正在跑的 turn**：客户端断线不该中断它
        # （test_turn_completes_even_if_client_disconnects 锁住这条契约）
        _orphan(state.turn_task)
        await manager.disconnect(connection)


# ── 文本帧 ──────────────────────────────────────────────


async def _handle_text(
    state: _SessionState,
    settings: Settings,
    bus: EventBus,
    stt: STTProvider | None,
    orchestrator: Orchestrator,
    session_id: str,
    raw: str,
) -> None:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        _report_error(bus, "invalid_frame", "frame is not valid JSON")
        return

    try:
        frame = ClientFrame.model_validate(decoded)
    except ValidationError as exc:
        _report_error(bus, "invalid_frame", _validation_message("frame", exc))
        return

    if frame.type == ClientFrameType.INPUT_USER_TEXT.value:
        await _handle_user_text(bus, state, orchestrator, session_id, frame.payload)
    elif frame.type == ClientFrameType.INPUT_AUDIO_STARTED.value:
        await _handle_audio_started(state, settings, bus, frame.payload)
    elif frame.type == ClientFrameType.INPUT_AUDIO_STOPPED.value:
        await _finish_audio(state, bus, stt, orchestrator, session_id, reason="client_stop")
    elif frame.type == ClientFrameType.VOICE_MODE_SET.value:
        _handle_voice_mode(state, bus, frame.payload)
    else:
        _report_error(bus, "invalid_frame", f"unsupported frame type: {frame.type}")


async def _handle_user_text(
    bus: EventBus,
    state: _SessionState,
    orchestrator: Orchestrator,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        parsed = UserTextPayload.model_validate(payload)
    except ValidationError as exc:
        _report_error(bus, "invalid_frame", _validation_message("payload", exc))
        return

    await _start_turn(
        state,
        orchestrator,
        text=parsed.text,
        conversation_id=parsed.conversation_id,
        session_id=session_id,
    )


def _handle_voice_mode(state: _SessionState, bus: EventBus, payload: dict[str, Any]) -> None:
    """记录"这一轮要不要合成语音"。

    `docs/11` §2 要求只有用户显式进 voice mode 才采集麦克风；而"该不该把回复
    合成成语音"是同一个开关的另一面，所以用同一个标志。
    """
    try:
        parsed = VoiceModePayload.model_validate(payload)
    except ValidationError as exc:
        _report_error(bus, "invalid_frame", _validation_message("payload", exc))
        return

    state.voice_mode = parsed.enabled


# ── 音频输入 ────────────────────────────────────────────


async def _handle_audio_started(
    state: _SessionState,
    settings: Settings,
    bus: EventBus,
    payload: dict[str, Any],
) -> None:
    try:
        parsed = AudioStartedPayload.model_validate(payload)
    except ValidationError as exc:
        _report_error(bus, "invalid_frame", _validation_message("payload", exc))
        return

    try:
        fmt = AudioFormat(
            encoding=parsed.encoding,
            sample_rate=parsed.sample_rate,
            channels=parsed.channels,
        )
    except ValueError as exc:
        _report_error(bus, "invalid_frame", str(exc))
        return

    # **不做服务端重采样**：格式不符就明确拒绝，而不是悄悄按错采样率识别
    if fmt.sample_rate != settings.audio_input_sample_rate:
        _report_error(
            bus,
            "invalid_frame",
            f"unsupported sample_rate {fmt.sample_rate}; "
            f"this build expects {settings.audio_input_sample_rate}",
        )
        return
    if fmt.channels != settings.audio_input_channels:
        _report_error(
            bus,
            "invalid_frame",
            f"unsupported channels {fmt.channels}; "
            f"this build expects {settings.audio_input_channels}",
        )
        return

    correlation_id = new_id()
    state.orphan_reported = False
    state.audio = _AudioBuffer(
        format=fmt,
        correlation_id=correlation_id,
        conversation_id=parsed.conversation_id,
        vad=EnergyVad(
            VadConfig(
                enabled=settings.vad_enabled,
                silence_ms=settings.vad_silence_ms,
                energy_threshold=settings.vad_energy_threshold,
            ),
            sample_rate=fmt.sample_rate,
            channels=fmt.channels,
        ),
        max_bytes=int(fmt.bytes_per_second * settings.audio_max_utterance_seconds),
    )

    _publish(
        bus,
        EventType.AUDIO_INPUT_STARTED,
        correlation_id,
        {**fmt.to_dict(), "conversation_id": parsed.conversation_id},
    )


async def _handle_binary(
    state: _SessionState,
    settings: Settings,
    bus: EventBus,
    stt: STTProvider | None,
    orchestrator: Orchestrator,
    session_id: str,
    data: bytes,
) -> None:
    buffer = state.audio
    if buffer is None:
        if not state.orphan_reported:
            state.orphan_reported = True
            _report_error(
                bus, "invalid_frame", "binary frame without a preceding input.audio.started"
            )
        return

    if len(data) > settings.audio_chunk_max_bytes:
        _report_error(
            bus,
            "invalid_frame",
            f"audio chunk of {len(data)} bytes exceeds the "
            f"{settings.audio_chunk_max_bytes} byte limit",
        )
        return

    buffer.chunks.append(data)
    buffer.byte_count += len(data)

    if buffer.byte_count >= buffer.max_bytes:
        _logger.warning(
            "audio utterance truncated at the configured limit",
            extra={"event": "audio.truncated", "detail": {"limit": buffer.max_bytes}},
        )
        await _finish_audio(state, bus, stt, orchestrator, session_id, reason="truncated")
        return

    if buffer.vad.feed(data):
        await _finish_audio(state, bus, stt, orchestrator, session_id, reason="silence")


async def _finish_audio(
    state: _SessionState,
    bus: EventBus,
    stt: STTProvider | None,
    orchestrator: Orchestrator,
    session_id: str,
    *,
    reason: str,
) -> None:
    """结束一段 utterance：转写 → 起新 turn。"""
    buffer = state.audio
    state.audio = None
    if buffer is None:
        return

    _publish(
        bus,
        EventType.AUDIO_INPUT_STOPPED,
        buffer.correlation_id,
        {"reason": reason, "bytes": buffer.byte_count},
    )

    # 全程静音就别浪费一次 STT（也避免把一段环境噪声识别成一句话）
    if not buffer.vad.has_speech:
        return

    if stt is None:
        _report_error(
            bus,
            "stt_unavailable",
            "no speech-to-text provider is configured",
            correlation_id=buffer.correlation_id,
        )
        return

    pcm = b"".join(buffer.chunks)
    try:
        transcript = await stt.transcribe(pcm, buffer.format.sample_rate)
    except AstraError as exc:
        _logger.warning(
            "stt failed", extra={"event": "stt.failed", "detail": {"code": exc.code.value}}
        )
        _report_error(bus, exc.code.value, exc.message, correlation_id=buffer.correlation_id)
        return

    # 只记长度，不记内容——用户说的话不该进日志（docs/11 §6 的同一精神）
    _logger.info(
        "stt finished",
        extra={
            "event": "stt.final",
            "detail": {
                "chars": len(transcript.text),
                "has_hint": transcript.emotion_hint is not None,
            },
        },
    )
    _publish(
        bus,
        EventType.STT_FINAL,
        buffer.correlation_id,
        {
            "text": transcript.text,
            "language": transcript.language,
            "confidence": transcript.confidence,
            "emotion_hint": transcript.emotion_hint,
        },
    )

    if not transcript.text:
        return

    await _start_turn(
        state,
        orchestrator,
        text=transcript.text,
        conversation_id=buffer.conversation_id,
        session_id=session_id,
        correlation_id=buffer.correlation_id,
    )


# ── turn 生命周期 ───────────────────────────────────────


async def _start_turn(
    state: _SessionState,
    orchestrator: Orchestrator,
    *,
    text: str,
    conversation_id: str | None,
    session_id: str,
    correlation_id: str | None = None,
) -> None:
    """起一轮 turn；**先打断上一轮**。

    `docs/01` §4.6："被打断时优先停止 TTS 和长任务，进入新的 turn"。
    打断是"取消 + 等它真的退出"，不是"发个 cancel 就走"——否则新 turn 会与
    正在 unwind 的旧 turn 抢每会话锁，顺序就不确定了。
    """
    await _interrupt(state)

    state.turn_task = asyncio.create_task(
        orchestrator.run_turn(
            TurnRequest(
                text=text,
                conversation_id=conversation_id,
                session_id=session_id,
                correlation_id=correlation_id,
                synthesize_audio=state.voice_mode,
            )
        )
    )
    state.turn_task.add_done_callback(_log_turn_failure)


async def _interrupt(state: _SessionState) -> None:
    """取消并等待正在跑的 turn（如果有）。"""
    task = state.turn_task
    state.turn_task = None
    if task is None or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # 预期路径：被取消的 task 必然抛这个
        return
    except Exception:
        # turn 自己抛的异常已经由 _log_turn_failure 记过；这里只是等它退出
        _logger.warning(
            "interrupted turn raised", exc_info=True, extra={"event": "ws.turn.interrupted"}
        )


def _log_turn_failure(task: asyncio.Task[TurnResult]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        _logger.error(
            "turn task raised",
            exc_info=exception,
            extra={"event": "ws.turn.failed"},
        )


def _orphan(task: asyncio.Task[TurnResult] | None) -> None:
    """放生一个 turn task：客户端走了，但它还要跑完。"""
    if task is None or task.done():
        return
    _ORPHANED_TURNS.add(task)
    task.add_done_callback(_ORPHANED_TURNS.discard)


# ── 事件与错误 ──────────────────────────────────────────


def _publish(
    bus: EventBus, event_type: EventType, correlation_id: str, payload: dict[str, Any]
) -> None:
    bus.publish(make_event(event_type, correlation_id=correlation_id, payload=payload))


def _validation_message(what: str, exc: ValidationError) -> str:
    """统一的校验失败文案。

    刻意**只给条数、不给 pydantic 的原始错误结构**——那个可能很长，
    而且会把内部字段名泄漏给客户端。
    """
    return f"{what} failed validation: {exc.error_count()} error(s)"


def _report_error(
    bus: EventBus,
    code: str,
    message: str,
    *,
    correlation_id: str | None = None,
) -> None:
    """发一条 ``error.protocol``。**不关连接**（一个手滑不该掉会话）。

    无 turn 上下文时用 ``event_id`` 作为 ``correlation_id``（ADR 0012）。
    ``code`` 默认是 ``invalid_frame``；音频后端的失败会带上
    ``stt_unavailable`` 这类稳定错误码，客户端可以据此分支。

    **走总线而不是直接写 socket**：见模块文档字符串。
    """
    event_id = new_id()
    bus.publish(
        make_event(
            EventType.ERROR_PROTOCOL,
            correlation_id=correlation_id or event_id,
            payload={"code": code, "message": message},
            event_id=event_id,
        )
    )
