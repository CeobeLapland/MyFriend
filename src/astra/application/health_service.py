"""健康检查用例。

这是**降级策略的唯一实现处**：把各探针的结果汇总成一份 :class:`HealthReport`。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence

from astra.domain.health import (
    API_COMPONENT,
    CANONICAL_COMPONENTS,
    DATABASE_COMPONENT,
    ComponentHealth,
    HealthReport,
    HealthStatus,
    aggregate_status,
)
from astra.domain.ports import HealthProbe

_logger = logging.getLogger(__name__)


class HealthService:
    """汇总健康探针并计算整体状态。"""

    def __init__(
        self,
        probes: Sequence[HealthProbe] = (),
        *,
        version: str,
        required: frozenset[str] = frozenset(),
        started_at: float | None = None,
    ) -> None:
        self._probes = tuple(probes)
        self._version = version
        self._required = required
        self._started_at = time.monotonic() if started_at is None else started_at

    async def check(self) -> HealthReport:
        """执行一次健康检查。

        未注册探针的 :data:`CANONICAL_COMPONENTS` 会被填为 ``not_configured``，
        保证响应体的 key 集合在任何里程碑都一致（ADR 0006）。
        """
        components: dict[str, ComponentHealth] = {
            name: ComponentHealth(status=HealthStatus.NOT_CONFIGURED)
            for name in CANONICAL_COMPONENTS
        }

        # API 层能产生这份报告，本身就说明它在服务
        components[API_COMPONENT] = ComponentHealth(status=HealthStatus.OK, latency_ms=0.0)

        for probe in self._probes:
            name = probe.name
            components[name] = await self._run_probe(probe, name)

        return HealthReport(
            status=aggregate_status(components, required=self._required),
            version=self._version,
            schema_version=_extract_schema_version(components),
            uptime_s=round(time.monotonic() - self._started_at, 3),
            components=components,
        )

    async def _run_probe(self, probe: HealthProbe, name: str) -> ComponentHealth:
        """执行单个探针，把意外异常降级为 ``DOWN`` 而不是冒泡出去。

        ``/health`` 是排障入口，它自身绝不能因为某个依赖故障而失败。
        """
        try:
            return await probe.check()
        except Exception as exc:
            _logger.warning(
                "health probe failed",
                extra={"component": name, "event": "health.probe.failed"},
                exc_info=True,
            )
            return ComponentHealth(status=HealthStatus.DOWN, detail={"error": str(exc)})


def _extract_schema_version(components: Mapping[str, ComponentHealth]) -> int | None:
    """把 database 组件的 ``detail["schema_version"]`` 提升为报告的顶层字段。"""
    database = components.get(DATABASE_COMPONENT)
    if database is None or database.detail is None:
        return None

    value = database.detail.get("schema_version")
    return value if isinstance(value, int) else None
