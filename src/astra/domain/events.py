"""事件信封与类型登记（`docs/04_API_AND_EVENTS.md` §1、§4）。

``EventEnvelope`` 是**纯数据**，序列化后必须含且仅含 6 个必备字段：
``event_id`` / ``type`` / ``version`` / ``occurred_at`` / ``correlation_id`` /
``payload``。这条由 `tests/contract/test_event_envelope_contract.py` 锁住。

事件类型登记在这里而不是散落在各调用点，这样客户端拿到的事件名有单一真相。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from astra.domain.ids import new_id

#: 事件协议版本。新增事件类型不升版本；**改动已有事件的 payload 结构**才升。
EVENT_VERSION = 1

#: 信封的必备字段集合（顺序无关，但集合必须完全一致）
ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {"event_id", "type", "version", "occurred_at", "correlation_id", "payload"}
)


class EventType(StrEnum):
    """全量事件类型登记。"""

    # ── Conversation ────────────────────────────────────
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_MESSAGE_CREATED = "conversation.message.created"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_INTERRUPTED = "turn.interrupted"

    # ── Assistant ───────────────────────────────────────
    ASSISTANT_TEXT_COMPLETED = "assistant.text.completed"

    # ── Memory（M2 起）──────────────────────────────────
    MEMORY_CANDIDATE_CREATED = "memory.candidate.created"
    MEMORY_CREATED = "memory.created"
    MEMORY_RETRIEVED = "memory.retrieved"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_ARCHIVED = "memory.archived"
    MEMORY_DELETED = "memory.deleted"

    # ── State（M3 起）───────────────────────────────────
    #: 用 `docs/04` §4 的具体名，而不是 §3 里那个泛化的 ``assistant.state.updated``
    #: ——后者已移除（从未发布过，留着是死登记）。见 ADR 0020。
    STATE_EMOTION_UPDATED = "state.emotion.updated"
    STATE_RELATIONSHIP_UPDATED = "state.relationship.updated"

    # ── Audio（M4 起）───────────────────────────────────
    #: 客户端开始/停止送音频。**不是音频本身**——音频走二进制帧（见 BinaryFrame）。
    AUDIO_INPUT_STARTED = "audio.input.started"
    AUDIO_INPUT_STOPPED = "audio.input.stopped"
    STT_FINAL = "stt.final"
    TTS_STARTED = "tts.started"
    TTS_COMPLETED = "tts.completed"

    # ── Avatar ──────────────────────────────────────────
    AVATAR_EXPRESSION_SET = "avatar.expression.set"
    AVATAR_ACTION_PLAY = "avatar.action.play"

    # ── Protocol ────────────────────────────────────────
    ERROR_PROTOCOL = "error.protocol"


class ClientFrameType(StrEnum):
    """客户端可以发来的帧类型。"""

    INPUT_USER_TEXT = "input.user_text"
    #: 开始送音频。payload 里声明格式（encoding / sample_rate / channels），
    #: 之后的**二进制帧**按这个格式解释，直到 ``INPUT_AUDIO_STOPPED``。
    INPUT_AUDIO_STARTED = "input.audio.started"
    INPUT_AUDIO_STOPPED = "input.audio.stopped"
    #: 显式开关语音模式。``docs/11`` §2 要求"只有用户显式进 voice mode 才采集"，
    #: 而"这一轮该不该合成语音"是同一个开关的另一面。
    VOICE_MODE_SET = "session.voice_mode.set"


@dataclass(frozen=True, slots=True)
class BinaryFrame:
    """一段二进制负载（音频字节）。

    **它不是事件**：没有 ``event_id`` / ``type`` / ``version`` 信封，
    也不进幂等去重（去重靠 ``event_id``）。

    协议规定它**必须紧跟**一条说明格式的 JSON 事件：
    客户端发来的音频跟在 ``audio.input.started`` 之后，
    服务端推的音频跟在 ``tts.started`` 之后。没有上下文时收到二进制帧，
    边界层回 ``error.protocol`` 且**不关连接**（ADR 0021）。
    """

    payload: bytes


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """一条事件。"""

    event_id: str
    type: str
    version: int
    occurred_at: datetime
    correlation_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """序列化为对外 JSON。

        ``occurred_at`` 必须是带时区的 ISO-8601——客户端要靠它做时间线对齐。
        """
        return {
            "event_id": self.event_id,
            "type": self.type,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }


def make_event(
    event_type: EventType | str,
    *,
    correlation_id: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> EventEnvelope:
    """构造一条事件。"""
    return EventEnvelope(
        event_id=new_id() if event_id is None else event_id,
        type=str(event_type),
        version=EVENT_VERSION,
        occurred_at=occurred_at if occurred_at is not None else datetime.now(tz=UTC),
        correlation_id=correlation_id,
        payload=payload if payload is not None else {},
    )


#: 一条连接上可能流动的东西：JSON 事件，或一段二进制负载。
#: ``EventBus`` / ``ConnectionManager`` 的队列元素就是它。
ServerFrame = EventEnvelope | BinaryFrame
