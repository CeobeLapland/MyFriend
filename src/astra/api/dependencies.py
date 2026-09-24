"""路由共享的依赖注入函数。

统一从 ``app.state`` 取装配好的组件——``create_app`` 是唯一的装配点。
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from astra.application.conversation_service import ConversationService
from astra.application.health_service import HealthService
from astra.application.orchestrator import Orchestrator
from astra.domain.persona import Persona
from astra.settings import Settings


def get_health_service(request: Request) -> HealthService:
    return cast("HealthService", request.app.state.health_service)


def get_orchestrator(request: Request) -> Orchestrator:
    return cast("Orchestrator", request.app.state.orchestrator)


def get_conversations(request: Request) -> ConversationService:
    return cast("ConversationService", request.app.state.conversations)


def get_persona(request: Request) -> Persona:
    return cast("Persona", request.app.state.persona)


def get_app_settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)
