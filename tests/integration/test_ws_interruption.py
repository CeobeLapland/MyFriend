"""barge-in（打断）的端到端测试（真实 uvicorn）。

对应 `docs/10` §7 Scenario D："TTS speaking while user starts talking →
TTS stops quickly / new turn starts / no duplicated final response"。

这里的"打断"是**同一个连接上来了新的输入**，不是 TCP 断线——断线**不**打断
正在跑的 turn（那条契约由 ``test_chat_ws.py`` 的
``test_turn_completes_even_if_client_disconnects`` 锁住）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from astra.domain.events import EventType
from tests.fakes import FakeLLMProvider, FakeTTSProvider, llm_response

_RECV_TIMEOUT_S = 30.0
_SESSION = "01BARGE"


def _user_text(text: str) -> str:
    return json.dumps(
        {"type": "input.user_text", "version": 1, "payload": {"text": text}},
        ensure_ascii=False,
    )


def _voice_mode(enabled: bool) -> str:
    return json.dumps(
        {"type": "session.voice_mode.set", "version": 1, "payload": {"enabled": enabled}}
    )


async def _collect(socket: Any) -> list[dict[str, Any]]:
    """读到这一轮结束（``turn.completed`` / ``turn.failed``）。二进制音频帧被跳过。"""
    events: list[dict[str, Any]] = []
    while True:
        message = await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S)
        if isinstance(message, bytes):
            continue
        event = json.loads(message)
        events.append(event)
        if event["type"] in {
            EventType.TURN_COMPLETED.value,
            EventType.TURN_FAILED.value,
        }:
            return events


def test_new_input_interrupts_the_running_turn(run_server) -> None:
    """第一轮还在跑 LLM 时发第二句 → 第一轮收到 ``turn.interrupted``，不收到 completed。"""
    llm = FakeLLMProvider([llm_response() for _ in range(4)], delay_s=0.8)
    server = run_server(llm=llm)

    async def scenario() -> list[dict[str, Any]]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_user_text("第一句"))
            await asyncio.sleep(0.3)  # 让第一轮真的跑起来
            await socket.send(_user_text("第二句"))
            return await _collect(socket)

    events = asyncio.run(scenario())
    types = [event["type"] for event in events]

    assert types.count(EventType.TURN_INTERRUPTED.value) == 1
    assert types[-1] == EventType.TURN_COMPLETED.value
    # 只有第二轮发出 completed——第一轮**绝不能**也发一条
    assert types.count(EventType.TURN_COMPLETED.value) == 1

    interrupted = next(
        event for event in events if event["type"] == EventType.TURN_INTERRUPTED.value
    )
    completed = events[-1]
    assert interrupted["payload"]["turn_id"] != completed["payload"]["turn_id"]


def test_interrupted_turn_does_not_reach_the_tts_stage(run_server) -> None:
    """在 TTS 合成阶段被打断 → 那一轮的 ``tts.started`` 不该发出（声音没开始就不该响）。"""
    tts = FakeTTSProvider(samples=1600, delay_s=0.8)
    server = run_server(tts=tts)

    async def scenario() -> list[dict[str, Any]]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_voice_mode(True))
            await socket.send(_user_text("第一句"))
            await asyncio.sleep(0.3)  # 第一轮已经进到 TTS 合成里了
            await socket.send(_user_text("第二句"))
            return await _collect(socket)

    events = asyncio.run(scenario())
    types = [event["type"] for event in events]

    assert types.count(EventType.TURN_INTERRUPTED.value) == 1
    # 第一轮的 tts.started 没发；第二轮的发了
    assert types.count(EventType.TTS_STARTED.value) == 1
    assert tts.call_count == 2  # 第一次被取消，第二次跑完


def test_second_turn_completes_after_interrupting_the_first(run_server) -> None:
    """打断之后新 turn 必须真的跑完——取消不能把每会话锁一起带走。"""
    llm = FakeLLMProvider([llm_response() for _ in range(4)], delay_s=0.5)
    server = run_server(llm=llm)

    async def scenario() -> dict[str, Any]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_user_text("第一句"))
            await asyncio.sleep(0.2)
            await socket.send(_user_text("第二句"))
            events = await _collect(socket)
            return events[-1]

    completed = asyncio.run(scenario())

    assert completed["type"] == EventType.TURN_COMPLETED.value
    assert completed["payload"]["status"] == "ok"


def test_rapid_successive_messages_leave_exactly_one_completed_turn(run_server) -> None:
    """连发三句：前两轮各被打断一次，只有最后一句跑完。"""
    llm = FakeLLMProvider([llm_response() for _ in range(6)], delay_s=0.4)
    server = run_server(llm=llm)

    async def scenario() -> list[str]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            for text in ("第一句", "第二句", "第三句"):
                await socket.send(_user_text(text))
                await asyncio.sleep(0.15)
            events = await _collect(socket)
        return [event["type"] for event in events]

    types = asyncio.run(scenario())

    assert types.count(EventType.TURN_INTERRUPTED.value) == 2
    assert types.count(EventType.TURN_COMPLETED.value) == 1


def test_text_chat_still_works_after_an_interruption(run_server) -> None:
    """打断不是一次性开关——后续对话照常。"""
    llm = FakeLLMProvider([llm_response() for _ in range(6)], delay_s=0.3)
    server = run_server(llm=llm)

    async def scenario() -> list[str]:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(_user_text("第一句"))
            await asyncio.sleep(0.1)
            await socket.send(_user_text("第二句"))
            await _collect(socket)
            await socket.send(_user_text("第三句"))
            second = await _collect(socket)
        return [event["type"] for event in second]

    types = asyncio.run(scenario())

    assert types[-1] == EventType.TURN_COMPLETED.value
    assert EventType.TURN_INTERRUPTED.value not in types
