"""``LMStudioProvider`` 的单元测试（用 ``httpx.MockTransport``）。

重点验证：请求体是否带上 ``reasoning_effort`` 与 ``response_format``、
响应是否被正确解析、以及异常是否被翻译成可重试/不可重试的领域异常。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from astra.application.assistant_schema import build_response_format
from astra.domain.conversation import Role
from astra.domain.errors import (
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from astra.domain.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ReasoningEffort,
)
from astra.providers.lmstudio.client import LMStudioProvider

_MODEL = "qwen/qwen3.5-9b"


def _provider(handler, **overrides: Any) -> LMStudioProvider:
    params: dict[str, Any] = {
        "base_url": "http://localhost:1234/v1",
        "model": _MODEL,
        "api_key": "test-key",
        "connect_timeout_s": 5.0,
        "request_timeout_s": 120.0,
        "default_max_tokens": 400,
        "default_temperature": 0.7,
    }
    params.update(overrides)
    return LMStudioProvider(transport=httpx.MockTransport(handler), **params)


def _request(**overrides: Any) -> LLMRequest:
    params: dict[str, Any] = {
        "system_prompt": "你是 Astra。",
        "messages": (LLMMessage(role=Role.USER, content="你好"),),
    }
    params.update(overrides)
    return LLMRequest(**params)


def _completion(
    content: str = '{"reply":"你好呀"}',
    *,
    reasoning: str | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": _MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 90,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


def _generate(provider: LMStudioProvider, request: LLMRequest) -> LLMResponse:
    return asyncio.run(provider.generate(request))


def _captured(handler_holder: dict[str, Any]) -> dict[str, Any]:
    return json.loads(handler_holder["body"])


def _capture(
    payload_response: httpx.Response | None = None, **provider_overrides: Any
) -> tuple[LMStudioProvider, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        holder["body"] = request.content.decode("utf-8")
        holder["path"] = request.url.path
        return payload_response or _completion()

    return _provider(handler, **provider_overrides), holder


# ── 请求体 ──────────────────────────────────────────────


def test_posts_to_chat_completions() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    assert holder["path"] == "/v1/chat/completions"


def test_system_prompt_becomes_first_message() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    payload = _captured(holder)
    assert payload["messages"][0] == {"role": "system", "content": "你是 Astra。"}
    assert payload["messages"][1] == {"role": "user", "content": "你好"}


def test_model_and_defaults_are_sent() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    payload = _captured(holder)
    assert payload["model"] == _MODEL
    assert payload["max_tokens"] == 400
    assert payload["temperature"] == 0.7


def test_request_overrides_defaults() -> None:
    provider, holder = _capture()

    _generate(provider, _request(max_tokens=99, temperature=0.1))

    payload = _captured(holder)
    assert payload["max_tokens"] == 99
    assert payload["temperature"] == 0.1


def test_reasoning_effort_defaults_to_none() -> None:
    """默认必须关思考：开着时一次问候要 15.4s，关掉后 3.4s。"""
    provider, holder = _capture()

    _generate(provider, _request())

    assert _captured(holder)["reasoning_effort"] == "none"


def test_reasoning_effort_can_be_overridden_per_request() -> None:
    provider, holder = _capture()

    _generate(provider, _request(reasoning_effort=ReasoningEffort.HIGH))

    assert _captured(holder)["reasoning_effort"] == "high"


def test_reasoning_effort_is_omitted_when_disabled() -> None:
    """适配器可以配成完全不发这个参数（给不支持它的后端用）。"""
    provider, holder = _capture(default_reasoning_effort=None)

    _generate(provider, _request())

    assert "reasoning_effort" not in _captured(holder)


def test_response_format_is_sent_as_json_schema() -> None:
    provider, holder = _capture()

    _generate(provider, _request(response_format=build_response_format()))

    payload = _captured(holder)
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "AssistantResponse"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


def test_response_format_is_omitted_when_not_requested() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    assert "response_format" not in _captured(holder)


# ── 响应解析 ────────────────────────────────────────────


def test_response_fields_are_parsed() -> None:
    provider, _ = _capture()

    result = _generate(provider, _request())

    assert result.text == '{"reply":"你好呀"}'
    assert result.model == _MODEL
    assert result.finish_reason == "stop"
    assert result.response_id == "chatcmpl-1"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 40
    assert result.usage.completion_tokens == 90
    assert result.usage.reasoning_tokens == 0
    assert result.latency_ms >= 0.0


def test_reasoning_present_is_a_flag_not_the_content() -> None:
    """思考正文绝不能出现在返回值里——它可能被顺手写进日志。"""
    provider, _ = _capture(_completion(reasoning="让我想想……"))

    result = _generate(provider, _request())

    assert result.reasoning_present is True
    assert not hasattr(result, "reasoning")
    assert "让我想想" not in repr(result)


def test_reasoning_present_is_false_when_absent() -> None:
    provider, _ = _capture()

    assert _generate(provider, _request()).reasoning_present is False


def test_missing_usage_is_tolerated() -> None:
    provider, _ = _capture(httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}))

    result = _generate(provider, _request())

    assert result.usage is not None
    assert result.usage.prompt_tokens is None


def test_missing_choices_raises_retryable_error() -> None:
    provider, _ = _capture(httpx.Response(200, json={"choices": []}))

    with pytest.raises(LLMResponseError) as excinfo:
        _generate(provider, _request())

    assert excinfo.value.retryable is True


def test_empty_content_is_returned_as_empty_string() -> None:
    """content 为空 + finish_reason=length 是"思考吃光 token"的签名。

    这里只负责如实返回；是否重试由 Orchestrator 决定。
    """
    provider, _ = _capture(_completion("", reasoning="想很久", finish_reason="length"))

    result = _generate(provider, _request())

    assert result.text == ""
    assert result.finish_reason == "length"
    assert result.reasoning_present is True


# ── 异常翻译 ────────────────────────────────────────────


def test_connection_error_becomes_unavailable_and_is_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailableError) as excinfo:
        _generate(_provider(handler), _request())

    assert excinfo.value.retryable is False


def test_read_timeout_becomes_retryable_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(LLMTimeoutError) as excinfo:
        _generate(_provider(handler), _request())

    assert excinfo.value.retryable is True


def test_server_error_becomes_unavailable() -> None:
    with pytest.raises(LLMUnavailableError, match="HTTP 500"):
        _generate(_provider(lambda request: httpx.Response(500, text="boom")), _request())


def test_client_error_becomes_unavailable() -> None:
    with pytest.raises(LLMUnavailableError, match="HTTP 400"):
        _generate(_provider(lambda request: httpx.Response(400, text="bad")), _request())


def test_total_budget_timeout_is_enforced() -> None:
    """httpx 的 timeout 不是总预算，必须再套一层 asyncio.wait_for。

    用**异步** handler：同步 handler 里的 ``time.sleep`` 会阻塞事件循环，
    ``wait_for`` 根本没机会触发。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return _completion()

    provider = _provider(handler)

    with pytest.raises(LLMTimeoutError):
        _generate(provider, _request(timeout_s=0.05))
