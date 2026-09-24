"""手写 SQL 迁移 runner（ADR 0005）。

设计要点：

- 迁移文件为 ``migrations/NNNN_name.sql``，按版本号升序执行。
  版本号即文件名前缀并**保留零填充**（如 ``0001``），因此字典序就是版本序。
- ``schema_migrations(version, checksum, applied_at)`` 记录已应用版本。
- **每个迁移在单个显式事务内原子应用**：DDL 与版本记录要么都成功，要么都回滚。
- ``checksum`` 用 SHA-256 校验；已应用的迁移被改动时报错，而不是静默跳过。

关于 ``sqlite3.executescript()`` 的事务语义（已实测确认）：

    若当前存在未提交事务，先隐式执行一次 COMMIT；除此之外**不做任何事务控制**。

因此**不能**在外层 ``BEGIN`` 之后再调用它——必须把 ``BEGIN`` / ``COMMIT``
拼进传给它的脚本文本里。失败时脚本会抛异常并把事务留在打开状态，runner 需要
显式 ``ROLLBACK``；注意无事务时执行 ``ROLLBACK`` 会抛 ``OperationalError``，
所以必须先判断 ``conn.in_transaction``。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from astra.infrastructure.db.connection import open_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_VERSION_RE = re.compile(r"^(\d+)_")

#: 迁移文件内禁止出现事务控制语句——事务由 runner 独占。
_FORBIDDEN_RE = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE | re.MULTILINE,
)

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """迁移发现或执行失败。"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """按版本号升序读取并校验迁移文件。"""
    if not migrations_dir.is_dir():
        msg = f"migrations directory not found: {migrations_dir}"
        raise MigrationError(msg)

    migrations: list[Migration] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _VERSION_RE.match(path.name)
        if match is None:
            msg = f"migration filename must start with a number: {path.name}"
            raise MigrationError(msg)

        sql = path.read_text(encoding="utf-8")
        if _FORBIDDEN_RE.search(sql):
            msg = (
                f"{path.name} must not contain BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE; "
                "the runner owns the transaction"
            )
            raise MigrationError(msg)

        migrations.append(
            Migration(
                version=match.group(1),
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    return migrations


def expected_version(migrations_dir: Path = MIGRATIONS_DIR) -> str | None:
    """磁盘上可用的最高迁移版本号。"""
    migrations = discover(migrations_dir)
    return migrations[-1].version if migrations else None


def upgrade(
    db_path: Path | str,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """应用所有未执行的迁移，返回本次实际应用的版本号列表。

    幂等：重复调用不会重复应用，也不会报错。

    **加载 sqlite-vec 扩展**：``0003`` 会建 ``vec0`` 虚拟表，而虚拟表的 DDL 需要
    扩展已加载。扩展加载在这里是幂等且廉价的，所以统一开启，避免「哪些迁移需要
    扩展」这种隐式耦合（ADR 0018）。
    """
    migrations = discover(migrations_dir)
    applied: list[str] = []

    with open_connection(db_path, load_extensions=True) as conn:
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        already = {
            row["version"]: row["checksum"]
            for row in conn.execute("SELECT version, checksum FROM schema_migrations")
        }

        for migration in migrations:
            recorded = already.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    msg = (
                        f"migration {migration.version} was modified after being applied "
                        f"({migration.path.name}): checksum mismatch"
                    )
                    raise MigrationError(msg)
                continue

            _apply(conn, migration)
            applied.append(migration.version)

    return applied


def current_version(conn: sqlite3.Connection) -> str | None:
    """已应用的最高版本号。``schema_migrations`` 不存在时返回 ``None``。"""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return None

    if row is None or row["v"] is None:
        return None
    return str(row["v"])


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """在单个显式事务内应用一个迁移。

    值全部来自受控来源（纯数字版本号、十六进制 checksum、ISO-8601 时间戳），
    且 ``executescript`` 不支持参数绑定，因此只能内联。
    """
    script = (
        "BEGIN;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES "
        f"('{migration.version}', '{migration.checksum}', '{_now()}');\n"
        "COMMIT;"
    )

    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        msg = f"migration {migration.version} ({migration.path.name}) failed: {exc}"
        raise MigrationError(msg) from exc


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
