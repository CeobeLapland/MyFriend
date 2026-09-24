"""WebSocket 通道的端到端测试（真实 uvicorn）。

覆盖 M1b 的验收标准：事件序列、信封契约、多连接、协议错误不关连接、
客户端断开不影响 turn。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import websockets

from astra.domain.events import ENVELOPE_FIELDS, EventType
from tests.integration.conftest import LiveServer, default_llm

_TERMINATORS = {EventType.TURN_COMPLETED.value, EventType.TURN_FAILED.value}

#: 每一轮 turn 的核心事件序列（M1b 验收标准）
_CORE_SEQUENCE = [
    EventType.TURN_STARTED.value,
    EventType.CONVERSATION_MESSAGE_CREATED.value,
    EventType.CONVERSATION_MESSAGE_CREATED.value,
    EventType.ASSISTANT_TEXT_COMPLETED.value,
    EventType.AVATAR_EXPRESSION_SET.value,
    EventType.AVATAR_ACTION_PLAY.value,
    EventType.TURN_COMPLETED.value,
]

#: **新会话的第一轮**会多一条 conversation.created 打头
_FIRST_TURN_SEQUENCE = [EventType.CONVERSATION_CREATED.value, *_CORE_SEQUENCE]

_RECV_TIMEOUT_S = 30.0


def _user_text(text: str, **extra: Any) -> str:
    return json.dumps(
        {"type": "input.user_text", "version": 1, "payload": {"text": text, **extra}},
        ensure_ascii=False,
    )


async def _collect_turn(
    socket: Any, *, recv_timeout_s: float = _RECV_TIMEOUT_S
) -> list[dict[str, Any]]:
    """读到本轮结束。"""
    events: list[dict[str, Any]] = []
    while True:
        event = json.loads(await asyncio.wait_for(socket.recv(), timeout=recv_timeout_s))
        events.append(event)
        if event["type"] in _TERMINATORS:
            return events


async def _run_turn(server: LiveServer, text: str = "你好呀") -> list[dict[str, Any]]:
    async with websockets.connect(f"{server.ws_url}/ws/v1/session/01SESSION") as socket:
        await socket.send(_user_text(text))
        return await _collect_turn(socket)


# ── 事件序列 ────────────────────────────────────────────


def test_single_turn_event_sequence(run_server) -> None:
    """验收：turn.started → …message.created → …text.completed → avatar.* → turn.completed。"""
    server = run_server()

    events = asyncio.run(_run_turn(server))

    assert [event["type"] for event in events] == _FIRST_TURN_SEQUENCE


def test_second_turn_does_not_repeat_conversation_created(run_server) -> None:
    server = run_server()

    async def scenario() -> list[str]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send(_user_text("第一句"))
            await _collect_turn(socket)
            await socket.send(_user_text("第二句"))
            second = await _collect_turn(socket)
        return [event["type"] for event in second]

    assert asyncio.run(scenario()) == _CORE_SEQUENCE


def test_conversation_created_is_emitted_for_a_new_conversation(run_server) -> None:
    server = run_server()

    events = asyncio.run(_run_turn(server))

    assert events[0]["type"] == EventType.CONVERSATION_CREATED.value
    assert events[0]["payload"]["companion_id"] == "astra"


def test_assistant_text_completed_carries_the_reply(run_server) -> None:
    """客户端靠这条事件拿到回复正文——没有它 WS 客户端就没东西可显示。"""
    server = run_server()

    events = asyncio.run(_run_turn(server))
    completed = next(e for e in events if e["type"] == EventType.ASSISTANT_TEXT_COMPLETED.value)

    assert completed["payload"]["reply"] == "你好呀，今天过得怎么样？"
    assert completed["payload"]["emotion"]["primary"] == "happy"
    assert completed["payload"]["voice"]["style"] == "warm"


def test_avatar_events_use_normalized_values(run_server) -> None:
    """原始 LLM 字符串绝不能流到 Unity（docs/04 §5）。"""
    server = run_server()

    events = asyncio.run(_run_turn(server))
    expression = next(e for e in events if e["type"] == EventType.AVATAR_EXPRESSION_SET.value)
    action = next(e for e in events if e["type"] == EventType.AVATAR_ACTION_PLAY.value)

    assert expression["payload"]["emotion"] in {
        "neutral",
        "happy",
        "sad",
        "angry",
        "surprised",
        "embarrassed",
        "sleepy",
        "excited",
    }
    assert action["payload"]["name"] in {
        "idle",
        "laugh",
        "look_at_user",
        "look_away",
        "nod",
        "shake_head",
        "shy",
        "stretch",
        "think",
        "wave",
        "yawn",
    }
    assert action["payload"]["priority"] == "normal"


# ── 信封契约 ────────────────────────────────────────────


def test_every_envelope_has_exactly_six_fields(run_server) -> None:
    server = run_server()

    events = asyncio.run(_run_turn(server))

    assert events
    for event in events:
        assert set(event) == ENVELOPE_FIELDS


def test_correlation_id_is_stable_within_a_turn(run_server) -> None:
    server = run_server()

    events = asyncio.run(_run_turn(server))

    correlation_ids = {event["correlation_id"] for event in events}
    assert len(correlation_ids) == 1
    assert all(event["event_id"] != event["correlation_id"] for event in events)


def test_event_ids_are_unique(run_server) -> None:
    server = run_server()

    events = asyncio.run(_run_turn(server))

    event_ids = [event["event_id"] for event in events]
    assert len(set(event_ids)) == len(event_ids)


def test_occurred_at_is_timezone_aware(run_server) -> None:
    server = run_server()

    events = asyncio.run(_run_turn(server))

    assert all(event["occurred_at"].endswith("+00:00") for event in events)


def test_correlation_id_differs_across_turns(run_server) -> None:
    server = run_server()

    async def scenario() -> list[str]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send(_user_text("第一句"))
            first = await _collect_turn(socket)
            await socket.send(_user_text("第二句"))
            second = await _collect_turn(socket)
        return [first[-1]["correlation_id"], second[-1]["correlation_id"]]

    first_id, second_id = asyncio.run(scenario())
    assert first_id != second_id


# ── 多连接 ──────────────────────────────────────────────


def test_two_connections_on_same_session_both_receive(run_server) -> None:
    """M5 场景：Unity 客户端与文本调试客户端会同时在线。"""
    server = run_server()

    async def scenario() -> tuple[list[str], list[str]]:
        uri = f"{server.ws_url}/ws/v1/session/01SHARED"
        async with websockets.connect(uri) as first, websockets.connect(uri) as second:
            await first.send(_user_text("你好"))
            first_events = await _collect_turn(first)
            second_events = await _collect_turn(second)
        return (
            [event["type"] for event in first_events],
            [event["type"] for event in second_events],
        )

    first_types, second_types = asyncio.run(scenario())
    assert first_types == second_types == _FIRST_TURN_SEQUENCE


def test_connection_count_drops_after_disconnect(run_server) -> None:
    server = run_server()

    async def scenario() -> None:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S"):
            pass

    asyncio.run(scenario())
    # 断开后服务端不应留下异常；用一个正常 turn 证明服务仍然健康
    events = asyncio.run(_run_turn(server))
    assert events[-1]["type"] == EventType.TURN_COMPLETED.value


# ── 协议错误不关连接 ────────────────────────────────────


def test_invalid_json_returns_protocol_error_and_keeps_connection(run_server) -> None:
    server = run_server()

    async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send("这不是 JSON")
            error = json.loads(await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S))
            # 连接必须还活着：紧接着跑一轮正常对话
            await socket.send(_user_text("你好"))
            turn = await _collect_turn(socket)
        return error, turn

    error, turn = asyncio.run(scenario())
    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert error["payload"]["code"] == "invalid_frame"
    assert turn[-1]["type"] == EventType.TURN_COMPLETED.value


def test_unsupported_frame_type_returns_protocol_error(run_server) -> None:
    server = run_server()

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send(json.dumps({"type": "input.audio", "version": 1, "payload": {}}))
            return json.loads(await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S))

    error = asyncio.run(scenario())
    assert error["type"] == EventType.ERROR_PROTOCOL.value
    assert "unsupported frame type" in error["payload"]["message"]


def test_empty_text_returns_protocol_error(run_server) -> None:
    server = run_server()

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send(_user_text(""))
            return json.loads(await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S))

    error = asyncio.run(scenario())
    assert error["type"] == EventType.ERROR_PROTOCOL.value


def test_protocol_error_envelope_is_well_formed(run_server) -> None:
    server = run_server()

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send("bad")
            return json.loads(await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S))

    error = asyncio.run(scenario())
    assert set(error) == ENVELOPE_FIELDS
    # 无 turn 上下文的事件用 event_id 作为 correlation_id（ADR 0012）
    assert error["correlation_id"] == error["event_id"]


# ── 断开不影响 turn ─────────────────────────────────────


def test_turn_completes_even_if_client_disconnects(run_server) -> None:
    """客户端断线不该中断正在跑的 turn——它照常完成并落库。"""
    server = run_server()

    # 先拿一个已知的 conversation_id，这样断开后仍能查历史
    accepted = httpx.post(
        f"{server.http_url}/v1/chat/messages", json={"content": "预热"}, timeout=10.0
    ).json()
    conversation_id = accepted["conversation_id"]
    assert len(_wait_for_history(server, conversation_id, minimum=2)) == 2

    async def scenario() -> None:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/01S") as socket:
            await socket.send(_user_text("这句话必须落库", conversation_id=conversation_id))
            # 立刻断开，不等结果

    asyncio.run(scenario())

    messages = _wait_for_history(server, conversation_id, minimum=4)
    assert messages[-2]["content"] == "这句话必须落库"
    assert messages[-1]["role"] == "assistant"


# ── HTTP 202 ────────────────────────────────────────────


def test_accepted_endpoint_returns_immediately(run_server) -> None:
    """验收：<100ms 返回。这里放宽到 1s 以吸收 Windows 上的线程启动抖动。"""
    server = run_server()

    started = time.perf_counter()
    response = httpx.post(
        f"{server.http_url}/v1/chat/messages",
        json={"content": "你好"},
        timeout=10.0,
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["turn_id"]
    assert body["conversation_id"]
    assert elapsed < 1.0, f"202 端点花了 {elapsed:.3f}s，说明它在等 turn 完成"

    # 后台 turn 仍要真的跑完并落库
    assert len(_wait_for_history(server, body["conversation_id"], minimum=2)) == 2


def _wait_for_history(
    server: LiveServer, conversation_id: str, *, minimum: int = 2
) -> list[dict[str, Any]]:
    """轮询历史，直到至少 ``minimum`` 条消息或超时。"""
    deadline = time.monotonic() + 30.0
    messages: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        response = httpx.get(
            f"{server.http_url}/v1/conversations/{conversation_id}/messages", timeout=5.0
        )
        assert response.status_code == 200
        messages = response.json()["messages"]
        if len(messages) >= minimum:
            return messages
        time.sleep(0.2)

    return messages


def test_accepted_endpoint_rejects_unknown_conversation(run_server) -> None:
    server = run_server()

    response = httpx.post(
        f"{server.http_url}/v1/chat/messages",
        json={"content": "你好", "conversation_id": "01MISSING"},
        timeout=10.0,
    )

    assert response.status_code == 404


def test_accepted_endpoint_requires_non_empty_content(run_server) -> None:
    server = run_server()

    response = httpx.post(f"{server.http_url}/v1/chat/messages", json={"content": ""}, timeout=10.0)

    assert response.status_code == 422


def test_custom_llm_script_is_used(run_server) -> None:
    server = run_server(llm=default_llm(20))

    assert asyncio.run(_run_turn(server))[-1]["type"] == EventType.TURN_COMPLETED.value
