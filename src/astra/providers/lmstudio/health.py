"""LLM 健康探针。

探测 ``GET /models``：免费、瞬时，同时证明**可达性**与**鉴权**。

状态映射：

- 不可达 → ``DOWN``（可达性是二值的；``DEGRADED`` 留给"可达但不能用"）
- 可达但配置的模型不在列表里 → ``DEGRADED``（这正是"显存不够、模型没加载"的有用信号）
- 可达且模型在列表里 → ``OK``

``ASTRA_LLM_PROVIDER=none`` 时**不注册本探针**，``/health`` 保持 M0 的
``not_configured``（ADR 0006）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from astra.domain.health import LLM_COMPONENT, ComponentHealth, HealthStatus


class LMStudioHealthProbe:
    """探测 LM Studio 的可达性与模型加载状态。"""

    name = LLM_COMPONENT

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
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

        available = _model_ids(data)
        latency_ms = _elapsed_ms(started)

        if self._model not in available:
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                latency_ms=latency_ms,
                detail={
                    "error": "model_not_loaded",
                    "model": self._model,
                    "available": available,
                },
            )

        return ComponentHealth(
            status=HealthStatus.OK,
            latency_ms=latency_ms,
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
