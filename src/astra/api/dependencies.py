"""路由共享的依赖注入函数。

统一从 ``app.state`` 取装配好的组件——``create_app`` 是唯一的装配点。

**用 ``HTTPConnection`` 而不是 ``Request``**：前者是 ``Request`` 与 ``WebSocket``
的公共基类，因此同一组依赖函数既能给 HTTP 路由用，也能给 WebSocket 路由用。
用 ``Request`` 的话 WS 路由会在解析依赖时报
``missing 1 required positional argument``——WS 作用域里没有 ``Request``。
"""

from __future__ import annotations

from typing import cast

from starlette.requests import HTTPConnection

from astra.api.connection_manager import ConnectionManager
from astra.application.conversation_service import ConversationService
from astra.application.health_service import HealthService
from astra.application.memory_service import MemoryService
from astra.application.orchestrator import Orchestrator
from astra.application.state_service import StateService
from astra.domain.persona import Persona
from astra.settings import Settings


def get_health_service(connection: HTTPConnection) -> HealthService:
    return cast("HealthService", connection.app.state.health_service)


def get_orchestrator(connection: HTTPConnection) -> Orchestrator:
    return cast("Orchestrator", connection.app.state.orchestrator)


def get_conversations(connection: HTTPConnection) -> ConversationService:
    return cast("ConversationService", connection.app.state.conversations)


def get_memory_service(connection: HTTPConnection) -> MemoryService:
    return cast("MemoryService", connection.app.state.memories)


def get_state_service(connection: HTTPConnection) -> StateService:
    return cast("StateService", connection.app.state.state_service)


def get_connection_manager(connection: HTTPConnection) -> ConnectionManager:
    return cast("ConnectionManager", connection.app.state.connection_manager)


def get_persona(connection: HTTPConnection) -> Persona:
    return cast("Persona", connection.app.state.persona)


def get_app_settings(connection: HTTPConnection) -> Settings:
    return cast("Settings", connection.app.state.settings)
