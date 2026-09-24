"""FastAPI 应用装配。

``create_app()`` 是**唯一的依赖装配点**：Settings 在此转成具体的探针、provider
与服务实现，并挂到 ``app.state`` 上供路由通过依赖注入取用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from astra import __version__
from astra.api.connection_manager import ConnectionManager
from astra.api.routes.chat import router as chat_router
from astra.api.routes.chat_ws import router as chat_ws_router
from astra.api.routes.config import router as config_router
from astra.api.routes.conversations import router as conversations_router
from astra.api.routes.dev import router as dev_router
from astra.api.routes.health import router as health_router
from astra.application.conversation_service import ConversationService
from astra.application.event_bus import EventBus
from astra.application.health_service import HealthService
from astra.application.orchestrator import Orchestrator, TurnConfig
from astra.application.prompt_builder import PromptBudget
from astra.domain.errors import ConversationNotFoundError
from astra.domain.health import DATABASE_COMPONENT
from astra.domain.persona import Persona
from astra.domain.ports import HealthProbe, LLMProvider
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.health import DatabaseHealthProbe
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.persona.toml_loader import load_persona
from astra.providers.lmstudio.client import LMStudioProvider
from astra.providers.lmstudio.health import LMStudioHealthProbe
from astra.settings import Settings

_logger = logging.getLogger(__name__)

_LMSTUDIO = "lmstudio"


def create_app(settings: Settings, *, llm: LLMProvider | None = None) -> FastAPI:
    """按给定配置构造 FastAPI 应用。

    Args:
        settings: 运行时配置。
        llm: 可选的 provider 注入点。为 ``None`` 时按 ``settings.llm_provider``
            构造；显式注入主要用于测试，也方便将来接别的后端。

    Raises:
        PersonaLoadError: 人格文件缺失或非法——人格是应用的身份，缺失应当快速失败。
    """
    persona = load_persona(settings.persona_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 启动时确保 schema 就绪（M0 验收项在运行时的保证）
        applied = await asyncio.to_thread(upgrade, settings.db_path)
        if applied:
            _logger.info(
                "applied migrations: %s",
                ", ".join(applied),
                extra={"event": "db.migrated", "detail": applied},
            )
        yield

    app = FastAPI(title="Astra", version=__version__, lifespan=lifespan)

    _install_health(app, settings)
    _install_conversation_stack(app, settings, persona, llm)

    app.state.settings = settings
    app.state.persona = persona

    app.add_exception_handler(ConversationNotFoundError, _conversation_not_found_handler)

    app.include_router(health_router)
    app.include_router(config_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(chat_ws_router)

    # 开发者控制台只在 dev 环境挂载——绑到 0.0.0.0 时把 ASTRA_ENV 设成 prod 即可关掉
    if settings.env == "dev":
        app.include_router(dev_router)

    return app


async def _conversation_not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    """未知 / 已结束的会话统一返回 404，而不是静默返回空列表。"""
    assert isinstance(exc, ConversationNotFoundError)  # noqa: S101 - 由 add_exception_handler 保证
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": {"code": exc.code.value, "message": exc.message}},
    )


def _install_health(app: FastAPI, settings: Settings) -> None:
    probes: list[HealthProbe] = [DatabaseHealthProbe(settings.db_path)]

    # provider=none 时不注册 LLM 探针，/health 保持 M0 的 not_configured（ADR 0006）
    if settings.llm_provider == _LMSTUDIO:
        probes.append(
            LMStudioHealthProbe(
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key.get_secret_value(),
                timeout_s=settings.health_probe_timeout_s,
            )
        )

    required = frozenset({DATABASE_COMPONENT}) if settings.health_db_required else frozenset()

    app.state.health_service = HealthService(probes, version=__version__, required=required)


def _install_conversation_stack(
    app: FastAPI,
    settings: Settings,
    persona: Persona,
    llm: LLMProvider | None,
) -> None:
    repository = SQLiteConversationRepository(settings.db_path)
    conversations = ConversationService(
        repository,
        user_id=settings.user_id,
        companion_id=settings.companion_id,
        idle_timeout=timedelta(minutes=settings.conversation_idle_minutes),
    )

    provider = llm if llm is not None else _build_llm_provider(settings)

    # 事件总线是单进程内的广播点：Orchestrator 往里发，ConnectionManager 往外推
    bus = EventBus()

    orchestrator = Orchestrator(
        llm=provider,
        conversations=conversations,
        persona=persona,
        bus=bus,
        config=TurnConfig(
            user_id=settings.user_id,
            companion_id=settings.companion_id,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            request_timeout_s=settings.llm_request_timeout_s,
            reasoning_effort=settings.llm_reasoning_effort,
            history_budget=PromptBudget(
                max_history_messages=settings.llm_history_max_messages,
                max_history_chars=settings.llm_history_max_chars,
            ),
        ),
    )

    app.state.conversations = conversations
    app.state.orchestrator = orchestrator
    app.state.event_bus = bus
    app.state.connection_manager = ConnectionManager(bus)


def _build_llm_provider(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider != _LMSTUDIO:
        return None

    return LMStudioProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        connect_timeout_s=settings.llm_connect_timeout_s,
        request_timeout_s=settings.llm_request_timeout_s,
        default_max_tokens=settings.llm_max_tokens,
        default_temperature=settings.llm_temperature,
        default_reasoning_effort=settings.llm_reasoning_effort,
    )
