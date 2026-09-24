"""测试用的假实现与构造器。

放在包内（``tests/`` 是包）以便各测试文件共享。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from astra.domain.errors import LLMUnavailableError
from astra.domain.llm import LLMRequest, LLMResponse, LLMUsage

VALID_ASSISTANT_JSON = json.dumps(
    {
        "reply": "你好呀，今天过得怎么样？",
        "emotion": {"primary": "happy", "intensity": 0.6},
        "action": {"name": "nod", "intensity": 0.4},
        "voice": {"style": "warm", "speed": 1.0, "pitch": 0.0},
    },
    ensure_ascii=False,
)


def llm_response(
    text: str = VALID_ASSISTANT_JSON,
    *,
    model: str = "fake-model",
    finish_reason: str = "stop",
    reasoning_present: bool = False,
) -> LLMResponse:
    return LLMResponse(
        text=text,
        model=model,
        finish_reason=finish_reason,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=20, reasoning_tokens=0),
        latency_ms=1.0,
        reasoning_present=reasoning_present,
        response_id="fake-1",
    )


def assistant_json(**overrides: Any) -> str:
    """构造一段合法但可定制的助手 JSON 文本。"""
    payload = json.loads(VALID_ASSISTANT_JSON)
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class FakeLLMProvider:
    """脚本化的 :class:`astra.domain.ports.LLMProvider`。

    ``script`` 里每一项可以是 :class:`LLMResponse`（正常返回）或
    :class:`Exception`（抛出）。脚本用完后继续调用会抛
    :class:`LLMUnavailableError`，避免测试静默通过。
    """

    def __init__(
        self,
        script: list[LLMResponse | Exception] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        self.requests: list[LLMRequest] = []
        self._script: list[LLMResponse | Exception] = list(script or [])
        self._delay_s = delay_s

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)

        if self._delay_s:
            await asyncio.sleep(self._delay_s)

        if not self._script:
            raise LLMUnavailableError("FakeLLMProvider script is exhausted")

        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)
