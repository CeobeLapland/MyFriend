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
from astra.domain.memory import Memory, MemoryFilter


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


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本向量化后端（M2）。

    ``dim`` 是启动期校验的关键：``memory_vectors`` 的 vec0 表把维度写死在 DDL 里
    （迁移不可改），所以装配期必须比对，不一致就快速失败而不是运行时出错（ADR 0018）。
    """

    @property
    def model(self) -> str:
        """模型标识，落进 ``memory_embeddings.embedding_model``。"""
        ...

    @property
    def dim(self) -> int:
        """向量维度。"""
        ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """批量向量化，返回顺序与 ``texts`` 一一对应。

        失败时抛 :mod:`astra.domain.errors` 里的异常——与 ``LLMProvider`` 同一约定，
        由调用方（``MemoryService``）统一降级，而不是返回错误对象。
        """
        ...


class MemoryStore(Protocol):
    """记忆的持久化端口：**事实表 + 向量索引 + 运行时开关**。

    三者放在同一个端口下是刻意的：它们在同一份 SQLite 文件里，而
    ``docs/04`` §2 / ``docs/11`` §4 要求"删除记忆时必须同时处理向量索引"。
    拆成多个端口会让这条不变量失去单一入口。
    """

    async def add(self, memory: Memory, embedding: Sequence[float]) -> None:
        """写入一条记忆及其向量（事实 + embedding + 索引，同一事务）。"""
        ...

    async def get(self, memory_id: str) -> Memory | None:
        """按 id 读取；不存在返回 ``None``。"""
        ...

    async def get_many(self, memory_ids: Sequence[str]) -> tuple[Memory, ...]:
        """批量读取。**返回顺序跟随入参顺序**，缺失的 id 被跳过。"""
        ...

    async def list(self, query: MemoryFilter) -> tuple[Memory, ...]:
        """按过滤条件列出（``ORDER BY id``）。"""
        ...

    async def update(self, memory: Memory, embedding: Sequence[float] | None = None) -> None:
        """更新一条记忆。``embedding`` 非空时同时更新向量（内容变了才需要）。"""
        ...

    async def delete(self, memory_id: str) -> bool:
        """**真正删除**事实、embedding 与向量索引（`docs/04` §2）。返回是否存在。"""
        ...

    async def delete_all(self, *, user_id: str, companion_id: str) -> int:
        """清空该用户的全部记忆，返回删除条数。"""
        ...

    async def touch_accessed(self, memory_ids: Sequence[str], *, at: datetime) -> None:
        """更新 ``last_accessed_at``（`docs/06` §4 的 ``access_frequency`` 需要它）。"""
        ...

    async def search_similar(
        self,
        vector: Sequence[float],
        *,
        user_id: str,
        companion_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """向量近邻检索，返回 ``(memory_id, distance)``，按距离升序。

        只返回**可检索**的记忆（未归档、未过期）；重排序（importance / recency /
        access / relationship）不在这里做——那需要 ``docs/06`` §4 的完整公式，
        由 ``MemoryService`` 负责。
        """
        ...

    async def count(self, *, user_id: str, companion_id: str) -> int:
        """该用户当前未归档的记忆条数。"""
        ...

    async def get_flag(self, key: str) -> str | None:
        """读 ``app_flags``（M2 只用到 ``memory.enabled`` 一个键）。"""
        ...

    async def set_flag(self, key: str, value: str, *, at: datetime) -> None:
        """写 ``app_flags``。"""
        ...
