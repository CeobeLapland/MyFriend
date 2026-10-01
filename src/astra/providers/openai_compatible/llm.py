"""任意 OpenAI 兼容后端的 ``LLMProvider`` 实现。

请求形状与 ``providers/lmstudio/client.py`` 相同，**多出来的是两个保守开关**
（``llm_send_reasoning_effort`` / ``llm_use_json_schema``，默认都关）：

- 多数第三方厂商对不认识的参数直接回 **400**，所以 ``reasoning_effort`` 默认不发；
- ``response_format: {type: json_schema, strict: true}`` 只有一部分厂商支持，
  默认退到更通用的 ``{"type": "json_object"}``。

**为什么不用探测来自动决定**：参数容忍度是"服务端实现的静态属性"，猜错一次就是
一整轮对话失败，而且失败信息在 400 的 body 里、不好归因。让用户显式声明更诚实。

**响应解析比本地版本更宽**：``content`` 可能是字符串（常态）、对象数组
（``[{type: text, text: ...}]`` 这种 Responses/Anthropic 风格）或 ``null``
（推理模型把正文放在别处）。本地 LM Studio 不会出现后两种，所以那边没处理。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from astra.domain.conversation import Role
from astra.domain.errors import LLMResponseError, LLMTimeoutError, LLMUnavailableError
from astra.domain.llm import (
    JsonSchemaFormat,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ReasoningEffort,
)
from astra.providers.openai_compatible.errors import translate


class OpenAICompatibleLLMProvider:
    """通过 ``POST /chat/completions`` 调用任何 OpenAI 兼容后端。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        connect_timeout_s: float,
        request_timeout_s: float,
        default_max_tokens: int,
        default_temperature: float,
        send_reasoning_effort: bool = False,
        use_json_schema: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        #: False 时**完全不发** ``reasoning_effort``（多数厂商会 400）
        self._send_reasoning_effort = send_reasoning_effort
        #: False 时把 ``json_schema`` 降级成 ``json_object``
        self._use_json_schema = use_json_schema
        #: 测试用：注入 ``httpx.MockTransport`` 以避免真实网络
        self._transport = transport

    async def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request)
        timeout = request.timeout_s or self._request_timeout_s
        started = time.perf_counter()

        try:
            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.post("/chat/completions", json=payload),
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
        except TimeoutError as exc:
            # asyncio.wait_for 的总预算超时（httpx 自己的超时走下面的 HTTPError 分支）
            raise LLMTimeoutError("llm request exceeded total budget", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise translate(
                exc,
                unavailable=LLMUnavailableError,
                timeout=LLMTimeoutError,
                backend="llm provider",
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return self._parse_response(data, latency_ms)

    # ── 内部 ────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(
                connect=self._connect_timeout_s,
                read=self._request_timeout_s,
                write=self._request_timeout_s,
                pool=self._connect_timeout_s,
            ),
            transport=self._transport,
        )

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        messages = [{"role": Role.SYSTEM.value, "content": request.system_prompt}]
        messages.extend(
            {"role": message.role.value, "content": message.content} for message in request.messages
        )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self._default_temperature
            ),
            "max_tokens": request.max_tokens or self._default_max_tokens,
        }

        if self._send_reasoning_effort:
            effort = request.reasoning_effort or ReasoningEffort.NONE
            payload["reasoning_effort"] = effort.value

        if request.response_format is not None:
            payload["response_format"] = self._response_format(request.response_format)

        return payload

    def _response_format(self, fmt: JsonSchemaFormat) -> dict[str, Any]:
        if not self._use_json_schema:
            # 通用档：只需要"输出是 JSON"这一条保证，正文格式由 system prompt 的
            # Layer 8 说明 + response_parser 的降级兜底（ADR 0011）
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": fmt.name,
                "strict": fmt.strict,
                "schema": dict(fmt.schema),
            },
        }

    def _parse_response(self, data: object, latency_ms: float) -> LLMResponse:
        if not isinstance(data, dict):
            msg = "llm provider returned a non-object response"
            raise LLMResponseError(msg, detail=type(data).__name__)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = "llm provider returned no choices"
            raise LLMResponseError(msg, detail=str(data)[:300])

        choice: dict[str, Any] = choices[0] if isinstance(choices[0], dict) else {}

        raw_message = choice.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}

        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}

        raw_details = usage.get("completion_tokens_details")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}

        return LLMResponse(
            text=_extract_text(message),
            model=str(data.get("model") or self._model),
            finish_reason=(
                str(choice["finish_reason"]) if choice.get("finish_reason") is not None else None
            ),
            usage=LLMUsage(
                prompt_tokens=_as_int(usage.get("prompt_tokens")),
                completion_tokens=_as_int(usage.get("completion_tokens")),
                reasoning_tokens=_as_int(details.get("reasoning_tokens")),
            ),
            latency_ms=latency_ms,
            # 只记录"有没有思考"，**绝不携带思考正文**——SENSITIVE_KEYS 里没有
            # `reasoning`，正文一旦进入日志的 extra 就会原样落盘。
            reasoning_present=_has_reasoning(message),
            response_id=str(data["id"]) if data.get("id") is not None else None,
        )


def _extract_text(message: dict[str, Any]) -> str:
    """取出正文。

    三种形态：字符串（常态）、对象数组（``[{type: "text", text: ...}]``）、
    ``None``（推理模型把正文放在 ``reasoning`` 侧，或直接拒答）。
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _has_reasoning(message: dict[str, Any]) -> bool:
    """有没有思考正文。**只返回布尔**，与本地版本同一理由（不落盘）。"""
    return bool(message.get("reasoning_content")) or bool(message.get("reasoning"))


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
