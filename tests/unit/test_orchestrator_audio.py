"""``Orchestrator`` 的 M4 语音阶段与打断行为。

这一层只验证**编排**，不碰真实 TTS（那在真机验收里跑）：

- 什么时候**不该**合成（未配置 / 这一轮不要语音）；
- 事件顺序必须是 ``tts.started`` → 二进制帧 → ``tts.completed`` → ``turn.completed``；
- **TTS 失败绝不让 turn 失败**（`docs/10` §4「TTS failure」）；
- 被打断时发 ``turn.interrupted`` 并把 ``CancelledError`` 继续抛出去。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.application.conversation_service import ConversationService
from astra.application.event_bus import EventBus, Subscription
from astra.application.orchestrator import Orchestrator, TurnConfig, TurnRequest
from astra.application.prompt_builder import PromptBudget
from astra.domain.errors import TTSUnavailableError
from astra.domain.events import BinaryFrame, EventEnvelope, EventType
from astra.domain.llm import ReasoningEffort
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.persona.toml_loader import load_persona
from tests.fakes import FakeLLMProvider, FakeTTSProvider, llm_response

_BASE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFIG = TurnConfig(
    user_id="local",
    companion_id="astra",
    temperature=0.7,
    max_tokens=400,
    request_timeout_s=30.0,
    reasoning_effort=ReasoningEffort.NONE,
    history_budget=PromptBudget(20, 12000),
    tts_voice="开拓者",
    audio_chunk_bytes=1024,
)


@pytest.fixture
def persona():
    return load_persona(_REPO_ROOT / "personas" / "astra.toml")


@pytest.fixture
def conversations(tmp_path: Path) -> ConversationService:
    path = tmp_path / "astra.db"
    upgrade(path)
    return ConversationService(
        SQLiteConversationRepository(path),
        user_id="local",
        companion_id="astra",
        idle_timeout=timedelta(minutes=60),
        clock=lambda: _BASE,
    )


def _orchestrator(
    conversations: ConversationService,
    persona,
    *,
    tts: object = None,
    bus: EventBus | None = None,
    provider: FakeLLMProvider | None = None,
    **config_overrides: object,
) -> Orchestrator:
    return Orchestrator(
        llm=provider if provider is not None else FakeLLMProvider([llm_response()]),
        conversations=conversations,
        persona=persona,
        config=replace(_CONFIG, **config_overrides),  # type: ignore[arg-type]
        bus=bus,
        tts=tts,  # type: ignore[arg-type]
        clock=lambda: _BASE,
    )


async def _drain(subscription: Subscription) -> list[object]:
    frames: list[object] = []
    while subscription.pending:
        frame = await subscription.get()
        assert frame is not None
        frames.append(frame)
    return frames


def _event_types(frames: list[object]) -> list[str]:
    return [frame.type for frame in frames if isinstance(frame, EventEnvelope)]


# ── 什么时候不该合成 ────────────────────────────────────


def test_no_tts_events_when_this_turn_does_not_want_audio(
    conversations: ConversationService, persona
) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    tts = FakeTTSProvider()
    orchestrator = _orchestrator(conversations, persona, tts=tts, bus=bus)

    asyncio.run(orchestrator.run_turn(TurnRequest(text="你好")))
    types = _event_types(asyncio.run(_drain(subscription)))

    assert "tts.started" not in types
    assert tts.call_count == 0


def test_no_tts_events_when_tts_is_not_configured(
    conversations: ConversationService, persona
) -> None:
    """``ASTRA_TTS_PROVIDER=none`` 的形态：整个语音子系统缺席，turn 照常。"""
    bus = EventBus()
    subscription = bus.subscribe()
    orchestrator = _orchestrator(conversations, persona, tts=None, bus=bus)

    result = asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))
    types = _event_types(asyncio.run(_drain(subscription)))

    assert result.succeeded is True
    assert "tts.started" not in types


# ── 正常路径 ────────────────────────────────────────────


def test_tts_events_wrap_the_binary_frames_and_precede_turn_completed(
    conversations: ConversationService, persona
) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    tts = FakeTTSProvider(samples=1600)  # 3200 字节
    orchestrator = _orchestrator(conversations, persona, tts=tts, bus=bus)

    asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))
    frames = asyncio.run(_drain(subscription))

    assert _event_types(frames) == [
        # 新会话的第一轮会多一条 conversation.created 打头（docs/04 §3）
        "conversation.created",
        "turn.started",
        "conversation.message.created",
        "conversation.message.created",
        "assistant.text.completed",
        "avatar.expression.set",
        "avatar.action.play",
        "tts.started",
        "tts.completed",
        "turn.completed",
    ]
    # 音频帧必须夹在 tts.started 与 tts.completed 之间
    binary_indexes = [index for index, frame in enumerate(frames) if isinstance(frame, BinaryFrame)]
    started_index = next(i for i, f in enumerate(frames) if getattr(f, "type", "") == "tts.started")
    completed_index = next(
        i for i, f in enumerate(frames) if getattr(f, "type", "") == "tts.completed"
    )
    assert all(started_index < index < completed_index for index in binary_indexes)


def test_binary_frames_reassemble_into_the_original_pcm(
    conversations: ConversationService, persona
) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    tts = FakeTTSProvider(samples=1600)
    orchestrator = _orchestrator(conversations, persona, tts=tts, bus=bus)

    asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))
    frames = asyncio.run(_drain(subscription))

    payload = b"".join(frame.payload for frame in frames if isinstance(frame, BinaryFrame))
    assert payload == tts.pcm


def test_chunks_respect_the_configured_frame_size(
    conversations: ConversationService, persona
) -> None:
    """3200 字节按 1024 切 → 4 帧（1024 + 1024 + 1024 + 128）。"""
    bus = EventBus()
    subscription = bus.subscribe()
    orchestrator = _orchestrator(conversations, persona, tts=FakeTTSProvider(samples=1600), bus=bus)

    asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))
    frames = asyncio.run(_drain(subscription))

    chunks = [frame.payload for frame in frames if isinstance(frame, BinaryFrame)]
    assert [len(chunk) for chunk in chunks] == [1024, 1024, 1024, 128]

    started = next(
        frame
        for frame in frames
        if isinstance(frame, EventEnvelope) and frame.type == "tts.started"
    )
    assert started.payload["chunk_count"] == 4
    assert started.payload["encoding"] == "pcm16"
    assert started.payload["sample_rate"] == 24000


def test_tts_receives_the_reply_text_and_voice_speed(
    conversations: ConversationService, persona
) -> None:
    tts = FakeTTSProvider()
    orchestrator = _orchestrator(conversations, persona, tts=tts)

    asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))

    assert tts.call_count == 1
    assert tts.requests[0].text == "你好呀，今天过得怎么样？"
    assert tts.requests[0].voice_id == "开拓者"
    assert tts.requests[0].speed == 1.0


# ── 失败隔离 ────────────────────────────────────────────


def test_tts_failure_does_not_fail_the_turn(conversations: ConversationService, persona) -> None:
    """`docs/10` §4「TTS failure」：TTS 挂了文本回复仍要送达。"""
    bus = EventBus()
    subscription = bus.subscribe()
    tts = FakeTTSProvider(error=TTSUnavailableError("backend down"))
    orchestrator = _orchestrator(conversations, persona, tts=tts, bus=bus)

    result = asyncio.run(orchestrator.run_turn(TurnRequest(text="你好", synthesize_audio=True)))
    frames = asyncio.run(_drain(subscription))

    assert result.succeeded is True
    assert result.response is not None
    assert result.response.reply == "你好呀，今天过得怎么样？"

    types = _event_types(frames)
    assert "tts.started" not in types
    completed = next(
        frame
        for frame in frames
        if isinstance(frame, EventEnvelope) and frame.type == "tts.completed"
    )
    assert completed.payload["status"] == "failed"
    assert completed.payload["code"] == "tts_unavailable"
    assert "turn.completed" in types


# ── 打断 ────────────────────────────────────────────────


def test_cancelled_turn_publishes_turn_interrupted(
    conversations: ConversationService, persona
) -> None:
    """barge-in 取消 turn 时，客户端必须收到 ``turn.interrupted`` 而不是干等。"""
    bus = EventBus()
    subscription = bus.subscribe()
    provider = FakeLLMProvider([llm_response()], delay_s=5.0)
    orchestrator = _orchestrator(conversations, persona, bus=bus, provider=provider)

    async def scenario() -> list[object]:
        task = asyncio.create_task(orchestrator.run_turn(TurnRequest(text="你好")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await _drain(subscription)

    frames = asyncio.run(scenario())
    types = _event_types(frames)

    assert "turn.interrupted" in types
    # 被取消的 turn **不该**发出完成事件——客户端要靠这个区分"结束了"与"被打断了"
    assert "turn.completed" not in types

    interrupted = next(
        frame
        for frame in frames
        if isinstance(frame, EventEnvelope) and frame.type == EventType.TURN_INTERRUPTED.value
    )
    assert interrupted.payload["conversation_id"]
    assert interrupted.payload["turn_id"]


def test_cancelled_turn_releases_the_conversation_lock(
    conversations: ConversationService, persona
) -> None:
    """取消不是瞬时的，但锁必须被释放——否则下一轮会永远等下去。"""
    provider = FakeLLMProvider([llm_response(), llm_response()], delay_s=0.2)
    orchestrator = _orchestrator(conversations, persona, provider=provider)

    async def scenario() -> str:
        first = asyncio.create_task(orchestrator.run_turn(TurnRequest(text="第一轮")))
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = await asyncio.wait_for(orchestrator.run_turn(TurnRequest(text="第二轮")), 5.0)
        return second.reply_text

    assert asyncio.run(scenario())
