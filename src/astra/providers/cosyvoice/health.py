"""CosyVoice 雨落版的健康探针。

探测 ``GET /gradio_api/info``：它只回一份端点清单，**不触发任何模型推理**。
比"试合成一次"便宜几个数量级——真跑一次 TTS 要 2–30 s，而 `/health` 是要被
频繁调用的。

STT 与 TTS 是**同一个 Gradio 进程**，所以两个组件共用这套探测逻辑，
只是 ``name`` 不同（``stt`` / ``tts``）。这样 `/health` 的 5 个 key 集合不变
（ADR 0006），只是它们的**值**从 ``not_configured`` 变成真值。
"""

from __future__ import annotations

import time

import httpx

from astra.domain.health import ComponentHealth, HealthStatus


class CosyVoiceHealthProbe:
    """探测一个 Gradio 应用是否在服务。"""

    def __init__(
        self,
        *,
        component: str,
        base_url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: 组件名（``stt`` / ``tts``），必须是 CANONICAL_COMPONENTS 之一
        self.name = component
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._transport = transport

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_s),
                transport=self._transport,
            ) as client:
                response = await client.get("/gradio_api/info")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=_elapsed_ms(started),
                detail={"error": _describe(exc)},
            )

        return ComponentHealth(
            status=HealthStatus.OK,
            latency_ms=_elapsed_ms(started),
            detail={"backend": "cosyvoice-rainfall", "base_url": self._base_url},
        )


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
