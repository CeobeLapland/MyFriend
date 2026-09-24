"""数据库健康探针。

依据 `docs/04_API_AND_EVENTS.md` §2，``/health`` 需要检查 Database。
本探针做两件事：验证库可读，以及比对 schema 版本。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from astra.domain.health import DATABASE_COMPONENT, ComponentHealth, HealthStatus
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.migrations import current_version, expected_version


class DatabaseHealthProbe:
    """探测 SQLite 是否可用，并报告 schema 版本。

    刻意**不**做 ``PRAGMA integrity_check``：大库上会明显变慢，不适合放在
    请求路径上的健康检查里。
    """

    name = DATABASE_COMPONENT

    def __init__(self, db_path: Path, *, migrations_dir: Path | None = None) -> None:
        self._db_path = db_path
        self._expected_version = (
            expected_version() if migrations_dir is None else expected_version(migrations_dir)
        )

    async def check(self) -> ComponentHealth:
        # sqlite3 是阻塞的，必须离开事件循环
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> ComponentHealth:
        started = time.perf_counter()

        # 探针不应有副作用：库文件不存在时直接报告，不要顺手创建它
        if not self._db_path.exists():
            return ComponentHealth(
                status=HealthStatus.DOWN,
                detail={"error": f"database file not found: {self._db_path}"},
            )

        try:
            with open_connection(self._db_path) as conn:
                conn.execute("SELECT 1").fetchone()
                version = current_version(conn)
        except (sqlite3.Error, OSError) as exc:
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=_elapsed_ms(started),
                detail={"error": str(exc)},
            )

        latency_ms = _elapsed_ms(started)

        if version is None:
            return ComponentHealth(
                status=HealthStatus.DOWN,
                latency_ms=latency_ms,
                detail={"error": "database has no applied migrations"},
            )

        detail: dict[str, object] = {"migration_version": version}
        schema_version = _as_int(version)
        if schema_version is not None:
            detail["schema_version"] = schema_version

        if self._expected_version is not None and version != self._expected_version:
            detail["expected_migration_version"] = self._expected_version
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                latency_ms=latency_ms,
                detail=detail,
            )

        return ComponentHealth(status=HealthStatus.OK, latency_ms=latency_ms, detail=detail)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _as_int(version: str) -> int | None:
    """迁移版本号是零填充数字串（如 ``0001``），转成 int 供客户端比较。"""
    try:
        return int(version)
    except ValueError:
        return None
