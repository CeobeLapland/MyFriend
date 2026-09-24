"""``HealthService`` 与 ``aggregate_status`` 的单元测试。

全部使用假探针，不接触真实数据库或网络。
"""

from __future__ import annotations

import asyncio

import pytest

from astra.application.health_service import HealthService
from astra.domain.health import (
    CANONICAL_COMPONENTS,
    ComponentHealth,
    HealthReport,
    HealthStatus,
    aggregate_status,
)

_DB = "database"


class FakeProbe:
    """可编程的假探针。"""

    def __init__(
        self,
        name: str,
        status: HealthStatus = HealthStatus.OK,
        *,
        latency_ms: float | None = 1.0,
        detail: dict[str, object] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._status = status
        self._latency_ms = latency_ms
        self._detail = detail
        self._raises = raises

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> ComponentHealth:
        if self._raises is not None:
            raise self._raises
        return ComponentHealth(
            status=self._status,
            latency_ms=self._latency_ms,
            detail=self._detail,
        )


def _check(service: HealthService) -> HealthReport:
    return asyncio.run(service.check())


def _service(*probes: FakeProbe, required: frozenset[str] = frozenset()) -> HealthService:
    return HealthService(probes, version="0.1.0", required=required)


# ── 响应体结构 ──────────────────────────────────────────


def test_canonical_keys_are_always_present() -> None:
    """即使一个探针都没注册，5 个 key 也必须齐全（ADR 0006）。"""
    report = _check(_service())

    assert set(report.components) == set(CANONICAL_COMPONENTS)


def test_unregistered_components_are_not_configured() -> None:
    report = _check(_service(FakeProbe(_DB)))

    assert report.components["llm"].status is HealthStatus.NOT_CONFIGURED
    assert report.components["stt"].status is HealthStatus.NOT_CONFIGURED
    assert report.components["tts"].status is HealthStatus.NOT_CONFIGURED


def test_api_component_is_always_ok() -> None:
    report = _check(_service())

    assert report.components["api"].status is HealthStatus.OK
    assert report.components["api"].latency_ms == 0.0


def test_probe_result_is_used() -> None:
    report = _check(_service(FakeProbe(_DB, HealthStatus.OK, latency_ms=2.5)))

    assert report.components[_DB].status is HealthStatus.OK
    assert report.components[_DB].latency_ms == 2.5


def test_uptime_is_reported() -> None:
    report = _check(_service())

    assert report.uptime_s >= 0.0
    assert report.version == "0.1.0"


# ── 聚合规则 ────────────────────────────────────────────


def test_m0_baseline_is_ok() -> None:
    """M0 真实场景：api ok + database ok + llm/stt/tts 未配置 → 整体 ok。"""
    report = _check(_service(FakeProbe(_DB), required=frozenset({_DB})))

    assert report.status is HealthStatus.OK


def test_required_component_down_makes_overall_down() -> None:
    report = _check(_service(FakeProbe(_DB, HealthStatus.DOWN), required=frozenset({_DB})))

    assert report.status is HealthStatus.DOWN


def test_non_required_component_down_makes_overall_degraded() -> None:
    report = _check(_service(FakeProbe("llm", HealthStatus.DOWN)))

    assert report.status is HealthStatus.DEGRADED


def test_degraded_component_makes_overall_degraded() -> None:
    report = _check(_service(FakeProbe("llm", HealthStatus.DEGRADED)))

    assert report.status is HealthStatus.DEGRADED


@pytest.mark.parametrize("status", [HealthStatus.NOT_CONFIGURED, HealthStatus.DISABLED])
def test_neutral_statuses_do_not_affect_overall(status: HealthStatus) -> None:
    report = _check(_service(FakeProbe("llm", status), FakeProbe("stt", status)))

    assert report.status is HealthStatus.OK


def test_aggregate_status_on_empty_mapping() -> None:
    assert aggregate_status({}, required=frozenset()) is HealthStatus.OK


# ── 失败隔离 ────────────────────────────────────────────


def test_probe_exception_is_isolated_as_down() -> None:
    """探针抛异常时 /health 不能整体失败（01_REQUIREMENTS §3 稳定性）。"""
    report = _check(_service(FakeProbe("llm", raises=RuntimeError("connection refused"))))

    assert report.components["llm"].status is HealthStatus.DOWN
    assert report.components["llm"].detail == {"error": "connection refused"}
    assert report.status is HealthStatus.DEGRADED


def test_exception_in_required_probe_yields_down() -> None:
    report = _check(
        _service(
            FakeProbe(_DB, raises=TimeoutError("db locked")),
            required=frozenset({_DB}),
        )
    )

    assert report.status is HealthStatus.DOWN


# ── schema_version 提升 ─────────────────────────────────


def test_schema_version_extracted_from_database_detail() -> None:
    report = _check(_service(FakeProbe(_DB, detail={"schema_version": 1})))

    assert report.schema_version == 1


def test_schema_version_is_none_without_database_probe() -> None:
    assert _check(_service()).schema_version is None


def test_schema_version_is_none_when_detail_lacks_it() -> None:
    report = _check(_service(FakeProbe(_DB, detail={"other": "x"})))

    assert report.schema_version is None
