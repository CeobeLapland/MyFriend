"""``DatabaseHealthProbe`` 的单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from astra.domain.health import ComponentHealth, HealthStatus
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.health import DatabaseHealthProbe
from astra.infrastructure.db.migrations import upgrade


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "astra.db"


def _check(probe: DatabaseHealthProbe) -> ComponentHealth:
    return asyncio.run(probe.check())


def test_missing_database_file_is_down(db_path: Path) -> None:
    result = _check(DatabaseHealthProbe(db_path))

    assert result.status is HealthStatus.DOWN
    assert "not found" in str(result.detail)


def test_missing_database_file_is_not_created(db_path: Path) -> None:
    """探针不应有副作用。"""
    _check(DatabaseHealthProbe(db_path))

    assert not db_path.exists()


def test_unmigrated_database_is_down(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        conn.execute("SELECT 1")

    result = _check(DatabaseHealthProbe(db_path))

    assert result.status is HealthStatus.DOWN
    assert result.detail is not None
    assert "no applied migrations" in str(result.detail["error"])


def test_migrated_database_is_ok(db_path: Path) -> None:
    upgrade(db_path)

    result = _check(DatabaseHealthProbe(db_path))

    assert result.status is HealthStatus.OK
    assert result.detail is not None
    assert result.detail["schema_version"] == 1
    assert result.detail["migration_version"] == "0001"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0.0


def test_older_schema_is_degraded(tmp_path: Path, db_path: Path) -> None:
    """库里的版本落后于代码期望时降级，而不是判定为不可用。"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_a.sql").write_text("CREATE TABLE t1 (id TEXT);\n", encoding="utf-8")
    (migrations_dir / "0002_b.sql").write_text("CREATE TABLE t2 (id TEXT);\n", encoding="utf-8")

    # 只应用第一个迁移，模拟"库比代码旧"
    upgrade(db_path, migrations_dir)
    with open_connection(db_path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = '0002'")
        conn.execute("DROP TABLE t2")

    result = _check(DatabaseHealthProbe(db_path, migrations_dir=migrations_dir))

    assert result.status is HealthStatus.DEGRADED
    assert result.detail is not None
    assert result.detail["expected_migration_version"] == "0002"


def test_newer_schema_is_degraded(tmp_path: Path, db_path: Path) -> None:
    """库比代码新（例如代码被回滚）同样降级。"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_a.sql").write_text("CREATE TABLE t1 (id TEXT);\n", encoding="utf-8")
    upgrade(db_path, migrations_dir)

    with open_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO schema_migrations VALUES ('0002', 'deadbeef', '2026-01-01T00:00:00+00:00')"
        )

    result = _check(DatabaseHealthProbe(db_path, migrations_dir=migrations_dir))

    assert result.status is HealthStatus.DEGRADED
    assert result.detail is not None
    assert result.detail["migration_version"] == "0002"


def test_probe_reports_canonical_component_name(db_path: Path) -> None:
    assert DatabaseHealthProbe(db_path).name == "database"
