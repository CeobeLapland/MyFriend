"""共享测试 fixture。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

#: 仓库默认数据库位置（见 .env.example 的 ASTRA_DB_PATH）
_DEFAULT_DB_PATH = Path("data/astra.db")


@pytest.fixture(autouse=True)
def _clean_astra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空所有 ``ASTRA_*`` 环境变量。

    防止开发者本机导出的环境变量或 shell 里的残留污染测试结果。
    ``.env`` 文件本身由各测试显式传入 ``Settings(_env_file=None)`` 关闭。

    无需 teardown：``monkeypatch`` 会在用例结束时自动还原。
    """
    for key in list(os.environ):
        if key.startswith("ASTRA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _guard_default_database() -> Iterator[None]:
    """确保测试不会创建或改写仓库里的默认数据库。

    所有涉及 DB 的测试都必须使用 ``tmp_path``。
    """
    existed_before = _DEFAULT_DB_PATH.exists()
    mtime_before = _DEFAULT_DB_PATH.stat().st_mtime_ns if existed_before else None

    yield

    if not existed_before and _DEFAULT_DB_PATH.exists():
        msg = f"测试创建了默认数据库 {_DEFAULT_DB_PATH}；涉及 DB 的测试必须使用 tmp_path 隔离"
        raise AssertionError(msg)

    if mtime_before is not None and _DEFAULT_DB_PATH.stat().st_mtime_ns != mtime_before:
        msg = f"测试改写了默认数据库 {_DEFAULT_DB_PATH}"
        raise AssertionError(msg)
