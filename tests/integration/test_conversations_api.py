"""``GET /v1/conversations`` 的集成测试（Studio 的历史栏靠它）。

在**真实 uvicorn** 上跑：开两段会话各聊一轮，再看列表的顺序、计数与摘要。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

import websockets

_SESSION = "01CONVLIST"
_RECV_TIMEOUT_S = 30.0


def _send_text(server: Any, text: str) -> None:
    async def scenario() -> None:
        async with websockets.connect(f"{server.ws_url}/ws/v1/session/{_SESSION}") as socket:
            await socket.send(
                json.dumps(
                    {"type": "input.user_text", "version": 1, "payload": {"text": text}},
                    ensure_ascii=False,
                )
            )
            while True:
                message = await asyncio.wait_for(socket.recv(), timeout=_RECV_TIMEOUT_S)
                if not isinstance(message, str):
                    continue
                if json.loads(message)["type"] in {"turn.completed", "turn.failed"}:
                    return

    asyncio.run(scenario())


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - 本机地址
        return json.loads(response.read().decode("utf-8"))


def _list(server: Any, query: str = "") -> list[dict[str, Any]]:
    return _get_json(f"{server.http_url}/v1/conversations{query}")["conversations"]


def _new_conversation(server: Any) -> str:
    request = urllib.request.Request(  # noqa: S310 - 本机地址
        f"{server.http_url}/v1/conversations", method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 本机地址
        return str(json.loads(response.read().decode("utf-8"))["conversation_id"])


def test_list_is_empty_on_a_fresh_database(run_server) -> None:
    server = run_server()

    assert _list(server) == []


def test_list_orders_by_recent_activity_and_carries_a_preview(run_server) -> None:
    server = run_server()
    _send_text(server, "第一段的一句话")

    first = _list(server)
    assert len(first) == 1
    assert first[0]["message_count"] == 2
    assert first[0]["status"] == "active"
    assert first[0]["last_message_role"] == "assistant"
    assert first[0]["last_message_preview"]
    assert first[0]["last_message_at"] is not None

    # 开新会话再聊一句：新的排最前，旧的不再是 active 但**仍在列表里**
    old_id = first[0]["conversation_id"]
    new_id = _new_conversation(server)
    _send_text(server, "第二段的一句话")

    listing = _list(server)
    assert [item["conversation_id"] for item in listing] == [new_id, old_id]
    assert listing[0]["status"] == "active"
    assert listing[1]["status"] == "ended"
    assert listing[1]["message_count"] == 2


def test_list_respects_the_limit(run_server) -> None:
    server = run_server()
    _send_text(server, "第一段")
    _new_conversation(server)
    _send_text(server, "第二段")

    assert len(_list(server)) == 2
    assert len(_list(server, "?limit=1")) == 1


def test_preview_flattens_and_truncates() -> None:
    """摘要只为"认出这是哪一段"——换行压平、超长截断（直接测那个纯函数）。"""
    from astra.api.routes.conversations import _preview

    assert _preview("短消息") == "短消息"
    assert _preview("第一行\n第二行") == "第一行 第二行"

    long_preview = _preview("很长的一句话" * 60)
    assert len(long_preview) == 81  # 80 个字 + 省略号
    assert long_preview.endswith("…")
