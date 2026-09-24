"""FastAPI 应用装配。

``create_app()`` 是**唯一的依赖装配点**：Settings 在此转成具体的探针与服务实现，
并挂到 ``app.state`` 上供路由通过依赖注入取用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from astra import __version__
from astra.api.routes.health import router as health_router
from astra.application.health_service import HealthService
from astra.domain.health import DATABASE_COMPONENT
from astra.domain.ports import HealthProbe
from astra.infrastructure.db.health import DatabaseHealthProbe
from astra.infrastructure.db.migrations import upgrade
from astra.settings import Settings

_logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    """按给定配置构造 FastAPI 应用。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 启动时确保 schema 就绪——这是 M0 验收项
        # "empty DB can be initialized from scratch" 的运行时保证。
        applied = await asyncio.to_thread(upgrade, settings.db_path)
        if applied:
            _logger.info(
                "applied migrations: %s",
                ", ".join(applied),
                extra={"event": "db.migrated", "detail": applied},
            )
        yield

    app = FastAPI(title="Astra", version=__version__, lifespan=lifespan)

    probes: list[HealthProbe] = [DatabaseHealthProbe(settings.db_path)]
    required = frozenset({DATABASE_COMPONENT}) if settings.health_db_required else frozenset()
    app.state.health_service = HealthService(
        probes,
        version=__version__,
        required=required,
    )

    app.include_router(health_router)

    return app
