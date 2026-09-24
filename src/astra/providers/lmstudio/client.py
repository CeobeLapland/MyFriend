"""LM Studio 的 ``LLMProvider`` 实现（OpenAI 兼容接口）。

这是**唯一**允许 import httpx 的地方（ADR 0010）。

关于 ``reasoning_effort``：实测 ``qwen/qwen3.5-9b`` 默认开启思考，一次简单问候
要 15.4s 且思考会吃光 token 预算；置为 ``none`` 后降到 3.4s。
``chat_template_kwargs.enable_thinking=false``、prompt 里的 ``/no_think``、
``chat_template_kwargs.thinking=false`` **全部无效**，只有这个参数管用。

关于超时：httpx 的 ``timeout=`` 是**各阶段共用的默认值**，不是总预算。
这里显式构造 ``httpx.Timeout`` 并再套一层 ``asyncio.wait_for`` 兜总时长，
否则很容易写出"设了 120s 却 10 分钟不返回"的 bug。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from astra.domain.conversation import Role
from astra.domain.errors import LLMResponseError
from astra.domain.llm import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ReasoningEffort,
)
from astra.providers.lmstudio.errors import translate


class LMStudioProvider:
    """通过 ``/v1/chat/completions`` 调用本地 LM Studio。"""

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
        default_reasoning_effort: ReasoningEffort | None = ReasoningEffort.NONE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._default_reasoning_effort = default_reasoning_effort
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
            # asyncio.wait_for 的超时（httpx 自己的超时会走下面的 HTTPError 分支）
            from astra.domain.errors import LLMTimeoutError

            raise LLMTimeoutError("llm request exceeded total budget", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise translate(exc) from exc

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

        effort = request.reasoning_effort or self._default_reasoning_effort
        if effort is not None:
            payload["reasoning_effort"] = effort.value

        if request.response_format is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_format.name,
                    "strict": request.response_format.strict,
                    "schema": dict(request.response_format.schema),
                },
            }

        return payload

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
            text=str(message.get("content") or ""),
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
            reasoning_present=bool(message.get("reasoning_content")),
            response_id=str(data["id"]) if data.get("id") is not None else None,
        )


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
