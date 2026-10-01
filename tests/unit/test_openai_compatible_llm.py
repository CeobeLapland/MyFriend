"""``OpenAICompatibleLLMProvider`` 的单元测试（用 ``httpx.MockTransport``）。

与 ``test_lmstudio_client.py`` 是**两份不同的契约**，不是重复：本地 LM Studio
接受 ``reasoning_effort`` 与 ``json_schema``，第三方则默认不发（ADR 0032）。
这里重点钉住那两条"少发参数"的行为，以及响应里 ``content`` 不是字符串时的解析。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from astra.application.assistant_schema import build_response_format
from astra.domain.conversation import Role
from astra.domain.errors import LLMResponseError, LLMTimeoutError, LLMUnavailableError
from astra.domain.llm import LLMMessage, LLMRequest, LLMResponse, ReasoningEffort
from astra.providers.openai_compatible.llm import OpenAICompatibleLLMProvider

_MODEL = "gpt-4o-mini"


def _provider(handler: Any, **overrides: Any) -> OpenAICompatibleLLMProvider:
    params: dict[str, Any] = {
        "base_url": "https://api.example.com/v1",
        "model": _MODEL,
        "api_key": "test-key",
        "connect_timeout_s": 5.0,
        "request_timeout_s": 120.0,
        "default_max_tokens": 400,
        "default_temperature": 0.7,
    }
    params.update(overrides)
    return OpenAICompatibleLLMProvider(transport=httpx.MockTransport(handler), **params)


def _request(**overrides: Any) -> LLMRequest:
    params: dict[str, Any] = {
        "system_prompt": "你是 Astra。",
        "messages": (LLMMessage(role=Role.USER, content="你好"),),
    }
    params.update(overrides)
    return LLMRequest(**params)


def _completion(message: dict[str, Any] | None = None) -> httpx.Response:
    body = (
        message if message is not None else {"role": "assistant", "content": '{"reply":"你好呀"}'}
    )
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": _MODEL,
            "choices": [{"index": 0, "message": body, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 90,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


def _generate(provider: OpenAICompatibleLLMProvider, request: LLMRequest) -> LLMResponse:
    return asyncio.run(provider.generate(request))


def _capture(
    response: httpx.Response | None = None, **overrides: Any
) -> tuple[OpenAICompatibleLLMProvider, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        holder["body"] = request.content.decode("utf-8")
        holder["path"] = request.url.path
        holder["auth"] = request.headers.get("authorization")
        return response if response is not None else _completion()

    return _provider(handler, **overrides), holder


def _payload(holder: dict[str, Any]) -> dict[str, Any]:
    return json.loads(holder["body"])


# ── 请求体：保守参数 ────────────────────────────────────


def test_posts_to_chat_completions_with_bearer_auth() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    assert holder["path"] == "/v1/chat/completions"
    assert holder["auth"] == "Bearer test-key"


def test_reasoning_effort_is_not_sent_by_default() -> None:
    """多数第三方厂商不认识这个参数，会直接回 400。"""
    provider, holder = _capture()

    _generate(provider, _request(reasoning_effort=ReasoningEffort.HIGH))

    assert "reasoning_effort" not in _payload(holder)


def test_reasoning_effort_is_sent_when_enabled() -> None:
    provider, holder = _capture(send_reasoning_effort=True)

    _generate(provider, _request(reasoning_effort=ReasoningEffort.LOW))

    assert _payload(holder)["reasoning_effort"] == "low"


def test_response_format_degrades_to_json_object_by_default() -> None:
    provider, holder = _capture()

    _generate(provider, _request(response_format=build_response_format()))

    assert _payload(holder)["response_format"] == {"type": "json_object"}


def test_response_format_uses_json_schema_when_enabled() -> None:
    provider, holder = _capture(use_json_schema=True)

    _generate(provider, _request(response_format=build_response_format()))

    fmt = _payload(holder)["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "AssistantResponse"
    assert fmt["json_schema"]["strict"] is True


def test_response_format_is_omitted_when_not_requested() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    assert "response_format" not in _payload(holder)


def test_system_prompt_and_defaults_are_sent() -> None:
    provider, holder = _capture()

    _generate(provider, _request())

    payload = _payload(holder)
    assert payload["messages"][0] == {"role": "system", "content": "你是 Astra。"}
    assert payload["messages"][1] == {"role": "user", "content": "你好"}
    assert payload["model"] == _MODEL
    assert payload["max_tokens"] == 400
    assert payload["temperature"] == 0.7


# ── 响应解析：比本地版本更宽的形状 ──────────────────────


def test_string_content_is_parsed() -> None:
    provider, _ = _capture()

    result = _generate(provider, _request())

    assert result.text == '{"reply":"你好呀"}'
    assert result.model == _MODEL
    assert result.finish_reason == "stop"
    assert result.response_id == "chatcmpl-1"
    assert result.usage is not None
    assert result.usage.completion_tokens == 90


def test_content_as_text_parts_is_joined() -> None:
    """Responses / Anthropic 风格的 ``[{type: text, text: ...}]``。"""
    provider, _ = _capture(
        _completion(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": '{"reply"'},
                    {"type": "text", "text": ': "你好呀"}'},
                ],
            }
        )
    )

    assert _generate(provider, _request()).text == '{"reply": "你好呀"}'


def test_null_content_becomes_empty_string() -> None:
    """推理模型把正文放在 reasoning 侧时 ``content`` 是 null。

    只负责如实返回空串；是否重试由 Orchestrator 决定（ADR 0011）。
    """
    provider, _ = _capture(
        _completion({"role": "assistant", "content": None, "reasoning": "想很久"})
    )

    result = _generate(provider, _request())

    assert result.text == ""
    assert result.reasoning_present is True


def test_reasoning_present_never_carries_the_text() -> None:
    provider, _ = _capture(
        _completion({"role": "assistant", "content": "hi", "reasoning_content": "让我想想……"})
    )

    result = _generate(provider, _request())

    assert result.reasoning_present is True
    assert "让我想想" not in repr(result)


def test_missing_choices_raises_retryable_error() -> None:
    provider, _ = _capture(httpx.Response(200, json={"choices": []}))

    with pytest.raises(LLMResponseError) as excinfo:
        _generate(provider, _request())

    assert excinfo.value.retryable is True


# ── 异常翻译 ────────────────────────────────────────────


def test_connection_error_is_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailableError) as excinfo:
        _generate(_provider(handler), _request())

    assert excinfo.value.retryable is False


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(LLMTimeoutError) as excinfo:
        _generate(_provider(handler), _request())

    assert excinfo.value.retryable is True


def test_http_400_from_an_unsupported_parameter_is_reported() -> None:
    """未知参数被拒是这条路径最可能的失败，必须如实带出状态码。"""
    with pytest.raises(LLMUnavailableError, match="HTTP 400"):
        _generate(_provider(lambda request: httpx.Response(400, text="bad")), _request())


def test_total_budget_timeout_is_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return _completion()

    with pytest.raises(LLMTimeoutError):
        _generate(_provider(handler), _request(timeout_s=0.05))
