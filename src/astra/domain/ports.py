"""领域端口（Protocol）。

本模块**只**定义接口，实现一律放在 ``astra.infrastructure`` / ``astra.providers``。
禁止在本层 import fastapi / starlette / sqlite3 / httpx 或任何 infrastructure 模块，
由 ``tests/unit/test_architecture_rules.py`` 强制执行（ADR 0008）。

端口方法声明为 ``async def``，这是对**调用方**的契约；实现内部可以是同步的
（例如 SQLite 适配器内部用同步 sqlite3，只在方法入口 ``await asyncio.to_thread``）。
"""

from __future__ import annotations

from typing import Protocol

from astra.domain.health import ComponentHealth


class HealthProbe(Protocol):
    """单个外部依赖的健康探针。

    实现方应自行捕获 I/O 异常并返回 ``DOWN`` 状态。若实现意外抛出异常，
    ``HealthService`` 会把该组件记为 ``DOWN``，而不会让 ``/health`` 整体失败。
    """

    @property
    def name(self) -> str:
        """组件名，必须是 :data:`astra.domain.health.CANONICAL_COMPONENTS` 之一。"""
        ...

    async def check(self) -> ComponentHealth:
        """探测并返回当前状态。不得阻塞事件循环。"""
        ...
