"""OpenAI 兼容后端的健康探针（LLM / STT / TTS 共用一套逻辑）。

探测 ``GET /models``：免费、瞬时，而且同时证明**可达性**与**鉴权**（第三方 API
没有 key 会回 401，那正是"配置错了"最有用的信号）。**不触发任何推理**——
真跑一次生成或合成要几秒到几十秒，而 ``/health`` 是要被频繁调用的。

两种用法由 ``model`` 区分：

=================  ============  ==================================================
用途               ``model``     判定
=================  ============  ==================================================
LLM               模型名        OK / 模型不在清单里 → ``DEGRADED``（"没加载/名字写错"）
STT / TTS         ``None``      只判可达与鉴权；``/models`` 返回 404 → ``DEGRADED``
=================  ============  ==================================================

**为什么语音不校验模型名**：OpenAI 会在 ``/models`` 里列出 ``whisper-1`` /
``tts-1``，但大量兼容厂商只列 chat 模型——拿它当依据会把"其实能用"报成
``DEGRADED``。宁可少说一句，也不要说错（ADR 0006）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from astra.domain.health import ComponentHealth, HealthStatus


class OpenAICompatibleHealthProbe:
    """探测任意 OpenAI 兼容后端。``name`` 由 ``component`` 决定（``llm``/``stt``/``tts``）。"""

    def __init__(
        self,
        *,
        component: str,
        base_url: str,
        api_key: str,
        timeout_s: float,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: 组件名，必须是 CANONICAL_COMPONENTS 之一
        self.name = component
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        #: 给了就校验它在 ``/models`` 清单里；``None`` 表示只探可达性
        self._model = model
        self._transport = transport

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(self._timeout_s),
                transport=self._transport,
            ) as client:
                response = await client.get("/models")
                response.raise_for_status()
                data: Any = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and self._model is None:
                # 语音后端可能压根没有 /models——探不到不等于挂了
                return ComponentHealth(
                    status=HealthStatus.DEGRADED,
                    latency_ms=_elapsed_ms(started),
                    detail={"error": "no_models_endpoint", "base_url": self._base_url},
                )
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=_elapsed_ms(started),
                detail={"error": f"HTTP {exc.response.status_code}"},
            )
        except httpx.HTTPError as exc:
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=_elapsed_ms(started),
                detail={"error": _describe(exc)},
            )
        except json.JSONDecodeError:
            # 有响应但不是 JSON（例如被反代/登录页拦截）
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=_elapsed_ms(started),
                detail={"error": "invalid_json_response"},
            )

        if self._model is None:
            return ComponentHealth(
                status=HealthStatus.OK,
                latency_ms=_elapsed_ms(started),
                detail={"backend": "openai-compatible", "base_url": self._base_url},
            )

        available = _model_ids(data)
        if self._model not in available:
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                latency_ms=_elapsed_ms(started),
                detail={
                    "error": "model_not_listed",
                    "model": self._model,
                    "available": available,
                },
            )

        return ComponentHealth(
            status=HealthStatus.OK,
            latency_ms=_elapsed_ms(started),
            detail={"model": self._model},
        )


def _model_ids(data: object) -> list[str]:
    if not isinstance(data, dict):
        return []
    entries = data.get("data")
    if not isinstance(entries, list):
        return []
    return [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") is not None
    ]


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
