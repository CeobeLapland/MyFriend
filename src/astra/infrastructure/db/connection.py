"""SQLite 连接管理（ADR 0004）。

设计要点：

- 使用 stdlib ``sqlite3``，**同步**实现。async 调用方应通过 ``asyncio.to_thread``
  调用，避免阻塞事件循环。
- **每个工作单元开一个新连接**，用完即关。SQLite 打开成本是微秒级，本地单用户
  场景 QPS 极低，这样可以天然规避跨线程共享问题。
- PRAGMA 的作用域不同，容易搞混：

  ==========================  ==================
  PRAGMA                      作用域
  ==========================  ==================
  ``journal_mode=WAL``        持久（写在库文件里）
  ``foreign_keys``            **每连接**，必须每次设置
  ``busy_timeout``            每连接
  ``synchronous``             每连接
  ==========================  ==================
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000


def connect(db_path: Path | str, *, load_extensions: bool = False) -> sqlite3.Connection:
    """打开一个配置好的连接。调用方负责 ``close()``。

    父目录不存在时会自动创建（``data/`` 通常不在版本控制里）。

    Args:
        db_path: 库文件路径。
        load_extensions: 为 ``True`` 时加载 sqlite-vec 扩展（M2 的向量索引需要）。
            **默认关闭**——只有需要 vec0 表的连接才开，且加载后立刻
            ``enable_load_extension(False)``，把「允许加载任意扩展」的窗口压到最小。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        # 关闭 sqlite3 的隐式事务管理，事务由我们显式控制（见 migrations.py）
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)

    if load_extensions:
        _load_vec_extension(conn)

    return conn


@contextmanager
def open_connection(
    db_path: Path | str, *, load_extensions: bool = False
) -> Iterator[sqlite3.Connection]:
    """``connect()`` 的上下文管理器形式，保证连接被关闭。"""
    conn = connect(db_path, load_extensions=load_extensions)
    try:
        yield conn
    finally:
        conn.close()


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """加载 sqlite-vec。

    只加载随项目依赖分发的那个扩展文件（``sqlite_vec.loadable_path()``），
    绝不加载外部路径；加载完立即关闭扩展加载开关（ADR 0018）。

    ``import`` 放在函数内：让 ``astra.infrastructure.db`` 的其他用途（会话仓储、
    健康探针）不必依赖 sqlite-vec 的导入副作用。
    """
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    _logger.debug("sqlite-vec extension loaded", extra={"event": "db.extension.loaded"})


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
