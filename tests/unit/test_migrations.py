"""``astra.infrastructure.db`` 的单元测试：连接 PRAGMA 与迁移 runner。

DB 一律使用 ``tmp_path`` 下的**真实临时文件**，不用 ``:memory:``：
每个 ``:memory:`` 连接是独立数据库，与"每工作单元一连接"的设计相冲突，
且无法验证 WAL 与重启持久性。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.migrations import (
    MigrationError,
    current_version,
    discover,
    expected_version,
    upgrade,
)

_EXPECTED_TABLES = {"conversations", "memories", "messages", "schema_migrations"}
_EXPECTED_INDEXES = {
    "idx_memories_companion_type",
    "idx_memories_importance",
    "idx_messages_conversation_created",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "astra.db"


def _names(db_path: Path, kind: str) -> set[str]:
    with open_connection(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {str(row["name"]) for row in rows}


def _write_migration(directory: Path, filename: str, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(sql, encoding="utf-8")
    return path


# ── 空库初始化 ──────────────────────────────────────────


def test_empty_db_initializes_from_scratch(db_path: Path) -> None:
    assert upgrade(db_path) == ["0001"]

    assert _names(db_path, "table") == _EXPECTED_TABLES
    assert _names(db_path, "index") >= _EXPECTED_INDEXES


def test_upgrade_is_idempotent(db_path: Path) -> None:
    upgrade(db_path)

    assert upgrade(db_path) == []
    assert _names(db_path, "table") == _EXPECTED_TABLES


def test_upgrade_creates_missing_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "astra.db"

    upgrade(nested)

    assert nested.is_file()


def test_upgrade_records_version(db_path: Path) -> None:
    upgrade(db_path)

    with open_connection(db_path) as conn:
        assert current_version(conn) == "0001"
        row = conn.execute("SELECT version, checksum, applied_at FROM schema_migrations").fetchone()

    assert row is not None
    assert row["version"] == "0001"
    assert len(row["checksum"]) == 64  # sha256 hexdigest
    assert row["applied_at"].endswith("+00:00")


def test_current_version_is_none_on_fresh_db(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        assert current_version(conn) is None


def test_state_survives_reconnect(db_path: Path) -> None:
    """模拟进程重启：新连接必须看到已建好的表，且不会重复应用迁移。"""
    upgrade(db_path)

    assert _names(db_path, "table") == _EXPECTED_TABLES
    assert upgrade(db_path) == []


def test_expected_version_matches_shipped_migrations() -> None:
    assert expected_version() == "0001"


def test_shipped_migrations_are_valid() -> None:
    migrations = discover()

    assert migrations, "至少要有一个迁移文件"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions), "版本号必须升序"
    assert len(set(versions)) == len(versions), "版本号不能重复"


# ── 校验与失败路径 ──────────────────────────────────────


def test_modified_migration_is_detected(tmp_path: Path, db_path: Path) -> None:
    """已应用的迁移被改动后必须报错，而不是静默跳过。"""
    migrations_dir = tmp_path / "migrations"
    file = _write_migration(migrations_dir, "0001_init.sql", "CREATE TABLE t1 (id TEXT);\n")
    upgrade(db_path, migrations_dir)

    file.write_text("CREATE TABLE t1 (id TEXT, extra TEXT);\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="checksum mismatch"):
        upgrade(db_path, migrations_dir)


def test_failed_migration_rolls_back_atomically(tmp_path: Path, db_path: Path) -> None:
    """中途失败时，DDL 与版本记录都必须回滚。"""
    migrations_dir = tmp_path / "migrations"
    _write_migration(
        migrations_dir,
        "0001_bad.sql",
        "CREATE TABLE t1 (id TEXT PRIMARY KEY);\n"
        "CREATE TABLE t1 (id TEXT PRIMARY KEY);\n",  # 重复建表 -> 报错
    )

    with pytest.raises(MigrationError, match="failed"):
        upgrade(db_path, migrations_dir)

    assert _names(db_path, "table") == {"schema_migrations"}
    with open_connection(db_path) as conn:
        assert current_version(conn) is None


def test_rollback_leaves_connection_usable(tmp_path: Path, db_path: Path) -> None:
    """回滚之后连接不能卡在事务里（否则后续操作会一直失败）。"""
    migrations_dir = tmp_path / "migrations"
    _write_migration(
        migrations_dir,
        "0001_bad.sql",
        "CREATE TABLE t1 (id TEXT);\nDROP TABLE nope;\n",
    )

    with pytest.raises(MigrationError):
        upgrade(db_path, migrations_dir)

    with open_connection(db_path) as conn:
        assert conn.in_transaction is False
        assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("statement", ["BEGIN;", "COMMIT;", "ROLLBACK;", "SAVEPOINT s;"])
def test_transaction_control_in_migration_is_rejected(tmp_path: Path, statement: str) -> None:
    """迁移文件里出现事务控制会造成嵌套事务，必须在 discover 阶段拦下。"""
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "0001_x.sql", f"{statement}\nCREATE TABLE t (id TEXT);\n")

    with pytest.raises(MigrationError, match="must not contain"):
        discover(migrations_dir)


def test_migration_filename_must_start_with_number(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_migration(migrations_dir, "init.sql", "CREATE TABLE t (id TEXT);\n")

    with pytest.raises(MigrationError, match="must start with a number"):
        discover(migrations_dir)


def test_missing_migrations_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="not found"):
        discover(tmp_path / "does-not-exist")


def test_empty_migrations_directory_yields_nothing(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    assert discover(migrations_dir) == []
    assert expected_version(migrations_dir) is None


# ── 连接 PRAGMA ─────────────────────────────────────────


def test_wal_journal_mode_is_enabled(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_foreign_keys_enabled_on_every_connection(db_path: Path) -> None:
    """`foreign_keys` 是每连接生效的，漏设会让约束静默失效。"""
    for _ in range(3):
        with open_connection(db_path) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_are_actually_enforced(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))"
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child VALUES ('c1', 'missing-parent')")


def test_busy_timeout_is_set(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_rows_are_accessible_by_column_name(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        row = conn.execute("SELECT 1 AS answer").fetchone()

    assert row is not None
    assert row["answer"] == 1


def test_connection_is_closed_after_context_exit(db_path: Path) -> None:
    with open_connection(db_path) as conn:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
