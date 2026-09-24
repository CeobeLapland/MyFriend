"""WebSocket 音频通道的端到端测试（真实 uvicorn）。

覆盖 M4 的验收标准 ``speak -> transcript -> response -> audio`` 的**接线部分**：
二进制帧的分派、格式校验、VAD 自动断句、STT → turn → TTS 的事件序列。

**不打桩就不打桩**：STT/TTS 用 ``FakeSTTProvider`` / ``FakeTTSProvider`` 注入。
真实 CosyVoice 需要 GPU 与一个外部进程，CI 不该依赖它——真机验收里才跑。
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

import websockets

from astra.domain.events import EventType
from tests.fakes import FakeSTTProvider, FakeTTSProvider

_TERMINATORS = {
    EventType.TURN_COMPLETED.value,
    EventType.TURN_FAILED.value,
    EventType.TURN_INTERRUPTED.value,
}
_RECV_TIMEOUT_S = 30.0
_SESSION = "01AUDIO"


def _pcm(samples: int, value: int) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def _frame(frame_type: str, payload: dict[str, Any]) -> str:
    return json.dumps({"type": frame_type, "version": 1, "payload": payload}, ensure_ascii=False)


def _start_audio(**overrides: Any) -> str:
    payload: dict[str, Any] = {"encoding": "pcm16", "sample_rate": 16000, "channels": 1}
    payload.update(overrides)
    return _frame("input.audio.started", payload)


class _Inbox:
    """把一条连接上收到的东西分成"事件"与"二进制帧"。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.binary: list[bytes] = []

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def find(self, event_type: str) -> dict[str, Any] | None:
        return next((event for event in self.events if event["type"] == event_type), None)

    async def drain(self, socket: Any) -> _Inbox:
        """读到本轮结束。

        **不在 ``error.protocol`` 上停**：VAD 自动断句之后客户端可能还会多送几帧，
        那条错误与 ``audio.input.stopped`` 谁先到是竞态。按类型查找才稳。
        """
        while True:
            message = await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S)
            if isinstance(message, bytes):
                self.binary.append(message)
                continue
            event = json.loads(message)
            self.events.append(event)
            if event["type"] in _TERMINATORS:
                return self

    async def recv_error(self, socket: Any) -> dict[str, Any]:
        """读到一条 ``error.protocol``（跳过其它事件）。"""
        while True:
            event = await self.recv_one(socket)
            if event["type"] == EventType.ERROR_PROTOCOL.value:
                return event

    async def recv_one(self, socket: Any) -> dict[str, Any]:
        message = await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S)
        assert isinstance(message, str), "expected a JSON event, got a binary frame"
        event = json.loads(message)
        self.events.append(event)
        return event


# ── 完整链路 ────────────────────────────────────────────


def test_speak_to_audio_round_trip(run_server) -> None:
    """验收：说话 → 转写 → 回复 → 音频。"""
    stt = FakeSTTProvider(text="你好呀")
    tts = FakeTTSProvider(samples=1600)
    server = run_server(stt=stt, tts=tts)

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_frame("session.voice_mode.set", {"enabled": True}))
            await socket.send(_start_audio())
            for _ in range(3):
                await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())
    types = inbox.types

    # 输入侧
    assert EventType.AUDIO_INPUT_STARTED.value in types
    assert EventType.AUDIO_INPUT_STOPPED.value in types
    assert EventType.STT_FINAL.value in types
    # 输出侧
    assert EventType.TTS_STARTED.value in types
    assert EventType.TTS_COMPLETED.value in types
    assert types[-1] == EventType.TURN_COMPLETED.value

    # 顺序：stt.final 在 turn.started 之前；tts.* 夹在 assistant 与 turn.completed 之间
    assert types.index(EventType.STT_FINAL.value) < types.index(EventType.TURN_STARTED.value)
    assert (
        types.index(EventType.ASSISTANT_TEXT_COMPLETED.value)
        < types.index(EventType.TTS_STARTED.value)
        < types.index(EventType.TURN_COMPLETED.value)
    )

    # 音频字节真的到了客户端
    assert b"".join(inbox.binary) == tts.pcm


def test_transcript_is_what_the_llm_receives(run_server) -> None:
    stt = FakeSTTProvider(text="这是语音转写的内容")
    server = run_server(stt=stt)

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())
    final = inbox.find(EventType.STT_FINAL.value)

    assert final is not None
    assert final["payload"]["text"] == "这是语音转写的内容"
    # 转写进入 turn（回复文本来自 FakeLLMProvider，与转写无关，但 turn 必须存在）
    assert inbox.find(EventType.TURN_STARTED.value) is not None


def test_stt_receives_the_accumulated_pcm(run_server) -> None:
    stt = FakeSTTProvider()
    server = run_server(stt=stt)

    async def scenario() -> None:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            for _ in range(3):
                await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            await _Inbox().drain(socket)

    asyncio.run(scenario())

    assert stt.call_count == 1
    assert len(stt.audio[0]) == 3 * 1600 * 2
    assert stt.sample_rates == [16000]


def test_no_tts_without_voice_mode(run_server) -> None:
    """没开 voice mode 就只回文本——`docs/11` §2 的显式开关。"""
    tts = FakeTTSProvider()
    server = run_server(stt=FakeSTTProvider(), tts=tts)

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())

    assert EventType.TTS_STARTED.value not in inbox.types
    assert tts.call_count == 0
    assert inbox.binary == []


# ── VAD ─────────────────────────────────────────────────


def test_vad_auto_finishes_the_utterance_on_silence(run_server) -> None:
    """用户忘了松手时，静音超时兜底（默认 800 ms）。

    **VAD 默认是关的**（真机验收发现它在自然停顿处就会截断），所以这里显式打开。
    """
    stt = FakeSTTProvider()
    server = run_server(stt=stt, vad_enabled=True, vad_silence_ms=300)

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))  # 100 ms 语音
            for _ in range(6):
                await socket.send(_pcm(1600, 0))  # 600 ms 静音 > 300 ms
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())

    assert stt.call_count == 1
    stopped = inbox.find(EventType.AUDIO_INPUT_STOPPED.value)
    assert stopped is not None
    assert stopped["payload"]["reason"] == "silence"


def test_vad_is_off_by_default(run_server) -> None:
    """**默认关闭**——真机验收实测它在自然停顿处就会截断正常对话。

    M4 的输入路径本来就是 push-to-talk，客户端一定会发 ``input.audio.stopped``；
    开着 VAD 只会主动把话截断。这条测试守住那个默认值别再被改回去。
    """
    stt = FakeSTTProvider()
    server = run_server(stt=stt)  # 不传 vad_* → 用默认值

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))  # 100 ms 语音
            for _ in range(10):
                await socket.send(_pcm(1600, 0))  # 1 秒静音，超过默认的 800 ms
            await socket.send(_frame("input.audio.stopped", {}))
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())
    stopped = inbox.find(EventType.AUDIO_INPUT_STOPPED.value)

    assert stt.call_count == 1
    assert stopped is not None
    assert stopped["payload"]["reason"] == "client_stop"


def test_silence_only_utterance_never_reaches_stt(run_server) -> None:
    """全程静音不该浪费一次 STT，也不该起一轮 turn。"""
    stt = FakeSTTProvider()
    server = run_server(stt=stt)

    async def scenario() -> list[dict[str, Any]]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            for _ in range(4):
                await socket.send(_pcm(1600, 0))
            await socket.send(_frame("input.audio.stopped", {}))
            received: list[dict[str, Any]] = []
            while True:
                message = await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S)
                assert isinstance(message, str)
                received.append(json.loads(message))
                if received[-1]["type"] == EventType.AUDIO_INPUT_STOPPED.value:
                    return received

    events = asyncio.run(scenario())

    assert stt.call_count == 0
    assert [event["type"] for event in events] == [
        EventType.AUDIO_INPUT_STARTED.value,
        EventType.AUDIO_INPUT_STOPPED.value,
    ]


# ── 协议错误 ────────────────────────────────────────────


def test_binary_frame_without_started_is_a_protocol_error(run_server) -> None:
    server = run_server(stt=FakeSTTProvider())

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_pcm(1600, 8000))
            return await _Inbox().recv_error(socket)

    error = asyncio.run(scenario())

    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert "input.audio.started" in error["payload"]["message"]


def test_unsupported_sample_rate_is_rejected(run_server) -> None:
    """**不做服务端重采样**：格式不符就明确拒绝，而不是悄悄按错采样率识别。"""
    server = run_server(stt=FakeSTTProvider())

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio(sample_rate=48000))
            return await _Inbox().recv_error(socket)

    error = asyncio.run(scenario())

    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert "sample_rate" in error["payload"]["message"]


def test_unsupported_encoding_is_rejected(run_server) -> None:
    server = run_server(stt=FakeSTTProvider())

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio(encoding="opus"))
            return await _Inbox().recv_error(socket)

    error = asyncio.run(scenario())

    # 编码在 payload 模型那一层就被 Literal 拦掉了，所以消息是通用校验失败文案
    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert error["payload"]["code"] == "invalid_frame"


def test_oversized_chunk_is_rejected(run_server) -> None:
    server = run_server(stt=FakeSTTProvider(), audio_chunk_max_bytes=2048)

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(4096, 8000))  # 8192 字节 > 2048
            return await _Inbox().recv_error(socket)

    error = asyncio.run(scenario())

    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert "exceeds" in error["payload"]["message"]


def test_orphaned_frames_after_vad_auto_finish_are_reported_once(run_server) -> None:
    """VAD 自动断句后客户端还会多送几帧——只报一次，不刷屏。"""
    server = run_server(stt=FakeSTTProvider(), vad_enabled=True, vad_silence_ms=200)

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            for _ in range(12):
                await socket.send(_pcm(1600, 0))
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())
    errors = [event for event in inbox.events if event["type"] == EventType.ERROR_PROTOCOL.value]

    assert len(errors) == 1


def test_protocol_error_does_not_close_the_connection(run_server) -> None:
    """一个手滑不该掉会话——紧接着还能正常跑一轮文本对话。"""
    server = run_server(stt=FakeSTTProvider())

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_pcm(1600, 8000))
            await _Inbox().recv_error(socket)
            await socket.send(
                json.dumps(
                    {"type": "input.user_text", "version": 1, "payload": {"text": "你好"}},
                    ensure_ascii=False,
                )
            )
            return await _Inbox().drain(socket)

    inbox = asyncio.run(scenario())

    assert inbox.types[-1] == EventType.TURN_COMPLETED.value


def test_stt_failure_is_reported_without_starting_a_turn(run_server) -> None:
    from astra.domain.errors import STTUnavailableError

    server = run_server(stt=FakeSTTProvider(error=STTUnavailableError("backend down")))

    async def scenario() -> _Inbox:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            inbox = _Inbox()
            await inbox.recv_error(socket)
            return inbox

    inbox = asyncio.run(scenario())
    error = inbox.find(EventType.ERROR_PROTOCOL.value)

    assert error is not None
    assert error["payload"]["code"] == "stt_unavailable"
    assert EventType.TURN_STARTED.value not in inbox.types


def test_stt_unavailable_when_not_configured(run_server) -> None:
    """``ASTRA_STT_PROVIDER=none``：语音输入缺席，键盘输入仍然可用。"""
    server = run_server()  # 不注入 stt

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            return await _Inbox().recv_error(socket)

    error = asyncio.run(scenario())

    assert error["payload"]["code"] == "stt_unavailable"


def test_audio_and_text_share_the_same_conversation(run_server) -> None:
    """语音与键盘是同一段对话——不然用户会在两个会话里各说一半。"""
    server = run_server(stt=FakeSTTProvider(text="用语音说的话"))

    async def scenario() -> tuple[str, str]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_start_audio())
            await socket.send(_pcm(1600, 8000))
            await socket.send(_frame("input.audio.stopped", {}))
            first = await _Inbox().drain(socket)
            await socket.send(
                json.dumps(
                    {"type": "input.user_text", "version": 1, "payload": {"text": "用键盘说的话"}},
                    ensure_ascii=False,
                )
            )
            second = await _Inbox().drain(socket)
        return (
            first.events[0]["correlation_id"],
            second.find(EventType.TURN_COMPLETED.value)["correlation_id"],
        )

    voice_correlation, text_correlation = asyncio.run(scenario())

    # 不同 turn 的 correlation_id 必须不同（ADR 0012）
    assert voice_correlation != text_correlation
