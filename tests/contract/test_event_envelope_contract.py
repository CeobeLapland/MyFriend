"""事件信封的契约测试（`docs/04_API_AND_EVENTS.md` §1）。

每个对外事件必须**含且仅含** 6 个必备字段，且
``occurred_at`` 带时区、``event_id != correlation_id``（turn 上下文里
``correlation_id`` 等于 ``turn_id``，而 ``event_id`` 是每个事件独立的）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astra.domain.events import (
    ENVELOPE_FIELDS,
    EVENT_VERSION,
    ClientFrameType,
    EventEnvelope,
    EventType,
    make_event,
)


def test_envelope_has_exactly_the_six_required_fields() -> None:
    payload = make_event(
        EventType.TURN_STARTED, correlation_id="01CORR", payload={"turn_id": "01T"}
    ).to_dict()

    assert set(payload) == ENVELOPE_FIELDS


def test_envelope_field_names_match_the_document() -> None:
    assert {
        "event_id",
        "type",
        "version",
        "occurred_at",
        "correlation_id",
        "payload",
    } == ENVELOPE_FIELDS


def test_occurred_at_is_timezone_aware_iso8601() -> None:
    payload = make_event(EventType.TURN_STARTED, correlation_id="01CORR").to_dict()

    parsed = datetime.fromisoformat(payload["occurred_at"])
    assert parsed.tzinfo is not None
    assert payload["occurred_at"].endswith("+00:00")


def test_version_defaults_to_one() -> None:
    assert make_event(EventType.TURN_STARTED, correlation_id="x").version == EVENT_VERSION


def test_event_id_is_generated_and_unique() -> None:
    first = make_event(EventType.TURN_STARTED, correlation_id="01CORR")
    second = make_event(EventType.TURN_STARTED, correlation_id="01CORR")

    assert first.event_id
    assert first.event_id != second.event_id


def test_event_id_differs_from_correlation_id() -> None:
    event = make_event(EventType.TURN_STARTED, correlation_id="01CORR")

    assert event.event_id != event.correlation_id


def test_correlation_id_is_shared_across_a_turn() -> None:
    """同一 turn 的所有事件共用一个 correlation_id。"""
    events = [
        make_event(event_type, correlation_id="01TURN")
        for event_type in (
            EventType.TURN_STARTED,
            EventType.CONVERSATION_MESSAGE_CREATED,
            EventType.AVATAR_EXPRESSION_SET,
            EventType.TURN_COMPLETED,
        )
    ]

    assert {event.correlation_id for event in events} == {"01TURN"}
    assert len({event.event_id for event in events}) == len(events)


def test_payload_defaults_to_empty_dict() -> None:
    assert make_event(EventType.TURN_STARTED, correlation_id="x").payload == {}


def test_payload_is_carried_through() -> None:
    event = make_event(
        EventType.AVATAR_EXPRESSION_SET,
        correlation_id="01T",
        payload={"emotion": "happy", "intensity": 0.72},
    )

    assert event.to_dict()["payload"] == {"emotion": "happy", "intensity": 0.72}


def test_explicit_occurred_at_is_preserved() -> None:
    moment = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    event = make_event(EventType.TURN_STARTED, correlation_id="x", occurred_at=moment)

    assert event.to_dict()["occurred_at"] == moment.isoformat()


def test_event_envelope_is_immutable() -> None:
    event = make_event(EventType.TURN_STARTED, correlation_id="x")

    with pytest.raises(AttributeError):
        event.type = "other"  # type: ignore[misc]


def test_type_is_a_plain_string_in_the_payload() -> None:
    payload = make_event(EventType.TURN_COMPLETED, correlation_id="x").to_dict()

    assert payload["type"] == "turn.completed"
    assert isinstance(payload["type"], str)


# ── 事件类型登记 ────────────────────────────────────────


@pytest.mark.parametrize(
    "event_type",
    [
        "conversation.created",
        "conversation.message.created",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.interrupted",
        "assistant.text.completed",
        "avatar.expression.set",
        "avatar.action.play",
        "error.protocol",
    ],
)
def test_documented_event_types_are_registered(event_type: str) -> None:
    assert event_type in {member.value for member in EventType}


def test_event_type_values_are_unique() -> None:
    values = [member.value for member in EventType]

    assert len(values) == len(set(values))


def test_client_frame_type_is_registered() -> None:
    assert ClientFrameType.INPUT_USER_TEXT.value == "input.user_text"


def test_envelope_can_be_constructed_directly() -> None:
    envelope = EventEnvelope(
        event_id="01A",
        type="turn.started",
        version=1,
        occurred_at=datetime(2026, 9, 24, tzinfo=UTC) + timedelta(seconds=1),
        correlation_id="01B",
        payload={},
    )

    assert envelope.to_dict()["event_id"] == "01A"
