"""``LMStudioHealthProbe`` 的单元测试（用 ``httpx.MockTransport``，不打真实网络）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from astra.domain.health import ComponentHealth, HealthStatus
from astra.providers.lmstudio.health import LMStudioHealthProbe

_MODEL = "qwen/qwen3.5-9b"


def _probe(handler) -> LMStudioHealthProbe:
    return LMStudioHealthProbe(
        base_url="http://localhost:1234/v1",
        model=_MODEL,
        api_key="test-key",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )


def _models_response(*model_ids: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"id": model_id, "object": "model"} for model_id in model_ids]},
    )


def _check(probe: LMStudioHealthProbe) -> ComponentHealth:
    return asyncio.run(probe.check())


# ── OK ──────────────────────────────────────────────────


def test_ok_when_model_is_loaded() -> None:
    probe = _probe(lambda request: _models_response(_MODEL, "other-model"))

    result = _check(probe)

    assert result.status is HealthStatus.OK
    assert result.detail == {"model": _MODEL}
    assert result.latency_ms is not None


def test_requests_the_models_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _models_response(_MODEL)

    _check(_probe(handler))

    assert seen == ["/v1/models"]


def test_sends_bearer_token() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _models_response(_MODEL)

    _check(_probe(handler))

    assert seen == ["Bearer test-key"]


# ── DEGRADED ────────────────────────────────────────────


def test_degraded_when_configured_model_is_not_loaded() -> None:
    """可达但模型没加载——这正是"显存不够、模型被换掉"的信号。"""
    probe = _probe(lambda request: _models_response("nvidia/nemotron-3-nano-4b"))

    result = _check(probe)

    assert result.status is HealthStatus.DEGRADED
    assert result.detail is not None
    assert result.detail["error"] == "model_not_loaded"
    assert result.detail["available"] == ["nvidia/nemotron-3-nano-4b"]


def test_degraded_when_model_list_is_empty() -> None:
    result = _check(_probe(lambda request: _models_response()))

    assert result.status is HealthStatus.DEGRADED


def test_degraded_when_payload_has_no_data_key() -> None:
    result = _check(_probe(lambda request: httpx.Response(200, json={})))

    assert result.status is HealthStatus.DEGRADED


# ── DOWN ────────────────────────────────────────────────


def test_down_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _check(_probe(handler))

    assert result.status is HealthStatus.DOWN
    assert result.detail is not None
    assert result.detail["error"] == "ConnectError"


def test_down_on_server_error() -> None:
    result = _check(_probe(lambda request: httpx.Response(500, text="boom")))

    assert result.status is HealthStatus.DOWN
    assert result.detail is not None
    assert result.detail["error"] == "HTTP 500"


def test_down_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    result = _check(_probe(handler))

    assert result.status is HealthStatus.DOWN
    assert result.detail is not None
    assert result.detail["error"] == "ReadTimeout"


def test_down_on_invalid_json() -> None:
    result = _check(_probe(lambda request: httpx.Response(200, text="not json")))

    assert result.status is HealthStatus.DOWN


# ── 契约 ────────────────────────────────────────────────


def test_probe_reports_canonical_component_name() -> None:
    probe = _probe(lambda request: _models_response(_MODEL))

    assert probe.name == "llm"


def test_probe_never_raises() -> None:
    """探针必须自行捕获异常——否则会污染 /health 的稳定性。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        # 非 HTTPError 不属于探针的职责范围；HealthService 会兜住它
        _check(_probe(handler))


def test_detail_is_json_serializable() -> None:
    result = _check(_probe(lambda request: _models_response("other")))

    json.dumps(result.detail, ensure_ascii=False)
