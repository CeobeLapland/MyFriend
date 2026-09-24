"""领域端口（Protocol）。

本模块**只**定义接口，实现一律放在 ``astra.infrastructure`` / ``astra.providers``。
禁止在本层 import fastapi / starlette / sqlite3 / httpx 或任何 infrastructure 模块，
由 ``tests/unit/test_architecture_rules.py`` 强制执行（ADR 0008）。

端口方法声明为 ``async def``，这是对**调用方**的契约；实现内部可以是同步的
（例如 SQLite 适配器内部用同步 sqlite3，只在方法入口 ``await asyncio.to_thread``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from astra.domain.conversation import Conversation, Message
from astra.domain.health import ComponentHealth
from astra.domain.llm import LLMRequest, LLMResponse


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


@runtime_checkable
class LLMProvider(Protocol):
    """文本生成后端。

    失败时抛 :mod:`astra.domain.errors` 里的异常，而不是返回错误对象——
    Orchestrator 的 ``try/except`` 是唯一的降级点，与 ``HealthService``
    隔离探针异常是同一个模式。

    ``runtime_checkable`` 只让测试能写 ``isinstance(FakeLLMProvider(), LLMProvider)``
    （仅检查方法存在性），不用于生产分支。
    """

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """执行一次生成。实现必须自行应用超时，且不得阻塞事件循环。"""
        ...


class ConversationRepository(Protocol):
    """会话与消息的持久化端口。

    实现位于 ``astra.infrastructure.db``。方法声明为 ``async def``（对调用方的
    契约），SQLite 实现内部是同步的，只在方法入口 ``await asyncio.to_thread``。
    """

    async def create(self, conversation: Conversation) -> None:
        """新建一个会话。"""
        ...

    async def get(self, conversation_id: str) -> Conversation | None:
        """按 id 读取会话；不存在返回 ``None``。"""
        ...

    async def find_active(self, *, user_id: str, companion_id: str) -> Conversation | None:
        """取该角色最近一个 ``active`` 会话；没有则返回 ``None``。"""
        ...

    async def end(self, conversation_id: str, *, ended_at: datetime) -> None:
        """把会话置为 ``ended``。"""
        ...

    async def last_message_at(self, conversation_id: str) -> datetime | None:
        """该会话最后一条消息的时间；空会话返回 ``None``。"""
        ...

    async def append_messages(self, messages: Sequence[Message]) -> tuple[str, ...]:
        """**幂等**写入一批消息，返回本次真正插入的消息 id。

        已存在同 ``(turn_id, role)`` 的消息会被跳过（`docs/04` §6 事件幂等性）。
        """
        ...

    async def load_messages(self, conversation_id: str) -> tuple[Message, ...]:
        """按 **id 升序**返回该会话的全部消息（ADR 0012：不能用 created_at 排序）。"""
        ...
