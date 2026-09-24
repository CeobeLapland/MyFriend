"""健康检查的领域值对象与聚合规则。

纯数据 + 纯函数：不依赖 FastAPI、sqlite 或任何框架。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: `/health` 契约中**固定输出**的组件名（ADR 0006）。
#: 后续里程碑接入 STT/TTS 时只改变这些组件的 *值*，不改变 key 集合。
CANONICAL_COMPONENTS: tuple[str, ...] = ("api", "database", "llm", "stt", "tts")

#: API 组件。由 HealthService 直接合成——能产生健康报告即说明 API 正在服务。
API_COMPONENT = "api"

#: LLM 组件。M1 起由 ``providers.lmstudio.LMStudioHealthProbe`` 注册。
LLM_COMPONENT = "llm"

#: 数据库组件。其 ``detail["schema_version"]`` 会被提升为报告的顶层字段。
DATABASE_COMPONENT = "database"


class HealthStatus(StrEnum):
    """组件或整体的健康状态。

    ``NOT_CONFIGURED`` 与 ``DISABLED`` 是**中性状态**：表示该能力本阶段就
    不该工作（例如 M0 尚未接入 LLM），因此不影响整体判定。
    ``DOWN`` 表示配置了但连不上，``DEGRADED`` 表示可用但有降级。
    """

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """单个组件的健康快照。组件名由所在字典的 key 承载，不在此重复。"""

    status: HealthStatus
    latency_ms: float | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次完整的健康检查结果。"""

    status: HealthStatus
    version: str
    schema_version: int | None
    uptime_s: float
    components: dict[str, ComponentHealth]


def aggregate_status(
    components: Mapping[str, ComponentHealth],
    *,
    required: frozenset[str],
) -> HealthStatus:
    """把各组件状态聚合为整体状态。

    规则（顺序即优先级）：

    1. 任一 **required** 组件 ``DOWN`` → 整体 ``DOWN``
    2. 任一组件 ``DOWN``（非 required）→ 整体 ``DEGRADED``
    3. 任一组件 ``DEGRADED`` → 整体 ``DEGRADED``
    4. 其余 → ``OK``

    ``NOT_CONFIGURED`` / ``DISABLED`` 不参与判定——M0 的 ``llm``/``stt``/``tts``
    属于这一类，因此整体仍是 ``OK``。
    """
    if any(
        health.status is HealthStatus.DOWN and name in required
        for name, health in components.items()
    ):
        return HealthStatus.DOWN

    if any(health.status is HealthStatus.DOWN for health in components.values()):
        return HealthStatus.DEGRADED

    if any(health.status is HealthStatus.DEGRADED for health in components.values()):
        return HealthStatus.DEGRADED

    return HealthStatus.OK
