"""``GET /health`` 路由。

响应体的 key 集合在 M0 一次定死（ADR 0006）：``status`` / ``version`` /
``schema_version`` / ``uptime_s`` / ``components``。后续里程碑接入 STT/TTS 时
只改变 ``components`` 里对应组件的 *值*，不改变结构。

HTTP 状态码**恒为 200**（只要进程还能响应）。严重程度只在 body 的 ``status``
字段表达——依据 `docs/01_REQUIREMENTS.md` §3，任何外部依赖不可用时系统都应降级
继续运行，不存在"该返 503"的语义。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from astra.api.dependencies import get_health_service
from astra.application.health_service import HealthService
from astra.domain.health import HealthStatus

router = APIRouter(tags=["system"])


class ComponentHealthResponse(BaseModel):
    """单个组件的健康状态。"""

    status: HealthStatus
    latency_ms: float | None = None
    detail: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """``/health`` 的响应契约。"""

    status: HealthStatus
    version: str
    schema_version: int | None = None
    uptime_s: float
    components: dict[str, ComponentHealthResponse] = Field(
        description="组件名 -> 健康状态。key 集合固定，见 CANONICAL_COMPONENTS。"
    )


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(service: HealthServiceDep) -> HealthResponse:
    report = await service.check()

    return HealthResponse(
        status=report.status,
        version=report.version,
        schema_version=report.schema_version,
        uptime_s=report.uptime_s,
        components={
            name: ComponentHealthResponse(
                status=component.status,
                latency_ms=component.latency_ms,
                detail=component.detail,
            )
            for name, component in report.components.items()
        },
    )
