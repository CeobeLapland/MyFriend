"""``OpenAICompatibleHealthProbe`` 的单元测试。

判定语义是"宁可不报降级也不要误报"（ADR 0006）：

- 不可达 / 鉴权失败 / 非 JSON → ``DOWN``（真的不能用）
- LLM 的模型不在 ``/models`` 清单里 → ``DEGRADED``（能连上，但当前这个模型用不了）
- 语音后端没有 ``/models`` 端点 → ``DEGRADED``（探不到 ≠ 挂了）
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from astra.domain.health import LLM_COMPONENT, STT_COMPONENT, HealthStatus
from astra.providers.openai_compatible.health import OpenAICompatibleHealthProbe


def _probe(handler: Any, **overrides: Any) -> OpenAICompatibleHealthProbe:
    params: dict[str, Any] = {
        "component": LLM_COMPONENT,
        "base_url": "https://api.example.com/v1",
        "api_key": "test-key",
        "timeout_s": 2.0,
    }
    params.update(overrides)
    return OpenAICompatibleHealthProbe(transport=httpx.MockTransport(handler), **params)


def _models(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": model} for model in ids]})


def _check(probe: OpenAICompatibleHealthProbe) -> Any:
    return asyncio.run(probe.check())


def test_ok_when_the_configured_model_is_listed() -> None:
    probe = _probe(lambda request: _models("gpt-4o-mini"), model="gpt-4o-mini")

    health = _check(probe)

    assert health.status is HealthStatus.OK
    assert health.detail == {"model": "gpt-4o-mini"}


def test_degraded_when_the_model_is_not_listed() -> None:
    """这正是"名字写错 / 模型没加载"最有用的信号，而不是笼统的 down。"""
    probe = _probe(lambda request: _models("gpt-4o"), model="gpt-4o-mini")

    health = _check(probe)

    assert health.status is HealthStatus.DEGRADED
    assert health.detail is not None
    assert health.detail["error"] == "model_not_listed"
    assert health.detail["available"] == ["gpt-4o"]


def test_audio_probe_checks_reachability_only() -> None:
    """语音不做模型名校验：很多兼容厂商不把 whisper/tts 列进 /models。"""
    probe = _probe(lambda request: _models("chat-only"), component=STT_COMPONENT, model=None)

    health = _check(probe)

    assert health.status is HealthStatus.OK
    assert health.detail is not None
    assert health.detail["backend"] == "openai-compatible"


def test_audio_probe_is_degraded_when_there_is_no_models_endpoint() -> None:
    """没有 /models 的语音后端：**探不到不等于挂了**，所以是降级而不是 down。"""
    probe = _probe(
        lambda request: httpx.Response(404, text="not found"), component=STT_COMPONENT, model=None
    )

    health = _check(probe)

    assert health.status is HealthStatus.DEGRADED
    assert health.detail is not None
    assert health.detail["error"] == "no_models_endpoint"


def test_missing_models_endpoint_is_down_for_the_llm() -> None:
    """同样是 404，LLM 那边意味着 ``base_url`` 写错了——那是真的用不了。"""
    probe = _probe(lambda request: httpx.Response(404, text="not found"), model="gpt-4o-mini")

    assert _check(probe).status is HealthStatus.DOWN


def test_unauthorized_is_down() -> None:
    probe = _probe(lambda request: httpx.Response(401, text="bad key"), model="gpt-4o-mini")

    health = _check(probe)

    assert health.status is HealthStatus.DOWN
    assert health.detail == {"error": "HTTP 401"}


def test_unreachable_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _check(_probe(handler, model="gpt-4o-mini")).status is HealthStatus.DOWN


def test_non_json_response_is_down() -> None:
    """被反代 / 登录页拦截时最常见：200 但不是 JSON。"""
    probe = _probe(lambda request: httpx.Response(200, text="<html>login</html>"))

    health = _check(probe)

    assert health.status is HealthStatus.DOWN
    assert health.detail == {"error": "invalid_json_response"}


@pytest.mark.parametrize("payload", [{"data": "nope"}, {"data": [{"no_id": 1}]}, []])
def test_unexpected_models_payload_degrades_instead_of_crashing(payload: Any) -> None:
    probe = _probe(lambda request: httpx.Response(200, json=payload), model="gpt-4o-mini")

    assert _check(probe).status is HealthStatus.DEGRADED
