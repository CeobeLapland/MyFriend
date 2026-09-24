"""状态子系统的集成测试（`docs/08_IMPLEMENTATION_PLAN.md` 阶段 3 的验收）。

验收标准（文档原文）：

```
user praises companion
 -> happiness increases
 -> assistant response style changes
 -> avatar event can reflect the new state
```

前两条在这里验；"response style changes" 用"状态确实进了 prompt 的 Layer 4"来验
（措辞风格无法断言，但**输入**可以）；"avatar event" 在 ``test_chat_ws`` 的事件序列里。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.domain.state import EMOTION_BASELINE_VALUES
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(db_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": db_path,
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


def _post_turn(client: TestClient, content: str) -> dict:
    response = client.post(
        "/v1/chat/turns",
        content=json.dumps({"content": content}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── 只读快照 ────────────────────────────────────────────


def test_state_endpoint_returns_the_baseline_on_a_fresh_install(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path / "astra.db"))) as client:
        body = client.get("/v1/state").json()

    assert body["companion_id"] == "astra"
    assert body["emotion"]["happiness"] == EMOTION_BASELINE_VALUES["happiness"]
    assert set(body["emotion"]) == {
        "happiness",
        "energy",
        "curiosity",
        "social_need",
        "stress",
        "embarrassment",
        "excitement",
    }
    assert set(body["relationship"]) == {
        "familiarity",
        "trust",
        "affection",
        "reliance",
        "conflict",
    }


def test_state_endpoint_is_read_only(tmp_path: Path) -> None:
    """`docs/03` §5：状态变化只来自事件，没有写入端点。"""
    with TestClient(create_app(_settings(tmp_path / "astra.db"))) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/state" in paths
    assert set(paths["/v1/state"]) == {"get"}


def test_state_survives_restart(tmp_path: Path) -> None:
    """状态落库——重启后不该回到基线。"""
    db_path = tmp_path / "astra.db"

    with TestClient(create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))) as first:
        _post_turn(first, "谢谢你，你真的很厉害")
        raised = first.get("/v1/state").json()["emotion"]["happiness"]

    with TestClient(create_app(_settings(db_path))) as second:
        reloaded = second.get("/v1/state").json()["emotion"]["happiness"]

    # 两次读取之间隔了几毫秒，惰性衰减会让值低一点点——所以用 approx
    assert reloaded == pytest.approx(raised, abs=1e-3)
    assert reloaded > EMOTION_BASELINE_VALUES["happiness"]


# ── 验收：称赞 → happiness 上升 ─────────────────────────


def test_praise_raises_happiness(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=FakeLLMProvider([llm_response()]))
    ) as client:
        before = client.get("/v1/state").json()

        _post_turn(client, "谢谢你，你真的很厉害，我很喜欢你")

        after = client.get("/v1/state").json()

    assert after["emotion"]["happiness"] > before["emotion"]["happiness"]
    # 关系也累积了（docs/03 §5：USER_PRAISED_COMPANION -> affection +0.02）
    assert after["relationship"]["affection"] > before["relationship"]["affection"]


def test_critique_lowers_happiness_and_raises_conflict(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=FakeLLMProvider([llm_response()]))
    ) as client:
        before = client.get("/v1/state").json()

        _post_turn(client, "你错了，答非所问")

        after = client.get("/v1/state").json()

    assert after["emotion"]["happiness"] < before["emotion"]["happiness"]
    assert after["relationship"]["conflict"] > before["relationship"]["conflict"]


def test_ordinary_chat_barely_moves_the_relationship(tmp_path: Path) -> None:
    """普通闲聊不该被当成"被称赞"——关系是累积的，不是每次说话都涨。"""
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=FakeLLMProvider([llm_response()]))
    ) as client:
        _post_turn(client, "今天天气不错")

        after = client.get("/v1/state").json()

    assert after["relationship"]["affection"] == 0.0
    assert after["relationship"]["familiarity"] == 0.0


# ── 验收：状态进入 prompt（"response style changes" 的输入侧）──


def test_state_reaches_the_prompt(tmp_path: Path) -> None:
    provider = FakeLLMProvider([llm_response(), llm_response()])

    with TestClient(create_app(_settings(tmp_path / "astra.db"), llm=provider)) as client:
        _post_turn(client, "谢谢你，我很喜欢你")
        _post_turn(client, "你好")

    first_prompt = provider.requests[0].system_prompt
    second_prompt = provider.requests[1].system_prompt

    assert "CURRENT STATE" in first_prompt
    # 第一轮时关系还是 0；第二轮应该已经带着上一轮涨起来的 affection
    assert "- affection: 0.00" in first_prompt
    assert "- affection: 0.00" not in second_prompt


def test_emotional_expression_shows_up_in_the_next_prompt(tmp_path: Path) -> None:
    """模型这轮说"开心"，下轮的 prompt 里 happiness 应该已经高了一点。"""
    provider = FakeLLMProvider([llm_response(), llm_response()])

    with TestClient(create_app(_settings(tmp_path / "astra.db"), llm=provider)) as client:
        _post_turn(client, "嗯")
        _post_turn(client, "嗯")

    baseline = EMOTION_BASELINE_VALUES["happiness"]
    assert f"- happiness: {baseline:.2f}" in provider.requests[0].system_prompt
    assert f"- happiness: {baseline:.2f}" not in provider.requests[1].system_prompt


# ── 事件 ────────────────────────────────────────────────


def test_state_events_are_published_on_the_bus(tmp_path: Path) -> None:
    """``state.emotion.updated`` / ``state.relationship.updated`` 必须真的发出来。"""
    app = create_app(_settings(tmp_path / "astra.db"), llm=FakeLLMProvider([llm_response()]))
    with TestClient(app) as client:
        subscription = app.state.event_bus.subscribe()

        _post_turn(client, "谢谢你，你真的很厉害")

        types = [event.type for event in _drain(subscription)]

    assert "state.emotion.updated" in types
    assert "state.relationship.updated" in types


def _drain(subscription: object) -> list:
    """同步把订阅队列里已有的事件取出来。

    队列非空时 ``await queue.get()`` 不会创建 future，所以这里用 ``asyncio.run``
    不会碰到"队列绑定到另一个事件循环"的问题。
    """

    async def collect() -> list:
        events = []
        while subscription.pending:  # type: ignore[attr-defined]
            event = await subscription.get()  # type: ignore[attr-defined]
            if event is None:
                break
            events.append(event)
        return events

    return asyncio.run(collect())


# ── 控制台 ──────────────────────────────────────────────


def test_dev_console_has_a_state_panel(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path / "astra.db"))) as client:
        body = client.get("/dev").text
        paths = client.get("/openapi.json").json()["paths"]

    assert "state-list" in body
    assert "/v1/state" in body
    assert "/v1/state" in paths


# ── 降级 ────────────────────────────────────────────────


def test_turn_still_works_without_state(tmp_path: Path) -> None:
    """所有流水线阶段都实现后，一轮对话仍然完整走通（``stubs_used`` 为空）。"""
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=FakeLLMProvider([llm_response()]))
    ) as client:
        body = _post_turn(client, "你好")

    assert body["status"] == "ok"
    assert body["reply"]
    assert body["stubs_used"] == []
