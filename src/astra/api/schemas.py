"""HTTP 请求 / 响应模型。

响应体的字段名直接对应用户可见的语义；``status`` 与 ``failure.code`` 是机读部分
（ADR 0014），``reply`` 是人读部分，**任何情况下都非空**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TurnStatus = Literal["ok", "failed"]


class TurnRequestBody(BaseModel):
    """``POST /v1/chat/turns`` 的请求体。"""

    content: str = Field(min_length=1, description="用户输入文本")
    conversation_id: str | None = Field(
        default=None,
        description="留空则复用最近的活跃会话（空闲超时后自动新开）",
    )
    input_type: Literal["text"] = "text"


class EmotionBody(BaseModel):
    primary: str
    intensity: float


class ActionBody(BaseModel):
    name: str
    intensity: float


class VoiceBody(BaseModel):
    style: str
    speed: float
    pitch: float


class AssistantResponseBody(BaseModel):
    reply: str
    emotion: EmotionBody
    action: ActionBody
    voice: VoiceBody


class FailureBody(BaseModel):
    code: str
    message: str
    recoverable: bool


class TimingsBody(BaseModel):
    total_ms: float
    llm_ms: float | None = None


class TurnResponse(BaseModel):
    """``POST /v1/chat/turns`` 的响应。

    **HTTP 状态码恒为 200**（沿用 ADR 0006 的哲学：进程活着就 200，严重程度
    只在 body 里表达）。LLM 挂掉时 ``status="failed"``、``failure.code`` 给出
    稳定错误码、``reply`` 仍然是一句诚实的中文。
    """

    turn_id: str
    conversation_id: str
    status: TurnStatus
    reply: str
    response: AssistantResponseBody | None = None
    failure: FailureBody | None = None
    model: str | None = None
    retries: int = 0
    degraded: bool = False
    stubs_used: list[str] = Field(default_factory=list)
    timings: TimingsBody
    #: M2：本轮注入 prompt 的记忆条数 / 新建的记忆条数。
    #: 客户端与开发者控制台靠它判断"记忆子系统到底有没有在工作"。
    memories_retrieved: int = 0
    memories_created: int = 0


class AcceptedBody(BaseModel):
    """``POST /v1/chat/messages`` 的响应。

    只表示"收到了"，实际结果通过 WebSocket 推送。
    """

    turn_id: str
    conversation_id: str
    status: Literal["accepted"] = "accepted"


class MessageBody(BaseModel):
    id: str
    conversation_id: str
    turn_id: str
    role: str
    content: str
    created_at: datetime
    metadata: dict[str, Any]


class ConversationHistoryBody(BaseModel):
    conversation_id: str
    messages: list[MessageBody]


class ConversationBody(BaseModel):
    """``POST /v1/conversations`` 的响应：刚开出来的那段会话。"""

    conversation_id: str
    started_at: datetime


class ConversationSummaryBody(BaseModel):
    """``GET /v1/conversations`` 列表里的一行（Studio 的历史栏用）。

    ``last_message_preview`` 是**截断过的开头**，只为在列表里认出来是哪一段；
    完整内容走 ``GET /v1/conversations/{id}/messages``。
    """

    conversation_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    message_count: int
    last_message_at: datetime | None = None
    last_message_role: str | None = None
    last_message_preview: str | None = None


class ConversationListBody(BaseModel):
    """最近的会话列表（按"最近有过消息"倒序）。"""

    conversations: list[ConversationSummaryBody] = Field(default_factory=list)


class ConfigBody(BaseModel):
    """``GET /v1/config`` 的非敏感运行配置。

    情绪 / 动作 / 音色风格的**词表在这里分发给客户端**——它们是协议契约，
    必须有单一真相（ADR 0015）。
    """

    companion_id: str
    user_id: str
    llm_provider: str
    llm_model: str
    # 以下三项在 M4/M5 接入时改为读 Settings；现在写成常量，避免提前引入
    # 未使用的配置字段（连带 .env.example 的同步负担）。
    stt_provider: str
    tts_provider: str
    avatar_enabled: bool
    emotions: list[str]
    actions: list[str]
    voice_styles: list[str]
    #: M2：记忆的**当前**开关状态（运行时可变，不等于 Settings 的初值）。
    memory_enabled: bool
    #: M2：记忆类型词表，与 emotions/actions 同理属于协议契约。
    memory_types: list[str]


# ── Memory（M2）─────────────────────────────────────────


class MemoryBody(BaseModel):
    """一条记忆的对外表示。"""

    id: str
    user_id: str
    companion_id: str
    type: str
    content: str
    summary: str | None = None
    importance: float
    confidence: float
    source_message_id: str | None = None
    source_event_id: str | None = None
    created_at: datetime
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None
    archived_at: datetime | None = None
    metadata: dict[str, Any]


class MemoryListBody(BaseModel):
    memories: list[MemoryBody]
    total: int


class MemoryUpdateBody(BaseModel):
    """``PATCH /v1/memories/{id}`` 的请求体。只传要改的字段。

    ``content`` 变了会**重新向量化并更新索引**——否则改完文字还是按旧向量被检索到。
    """

    content: str | None = Field(default=None, min_length=1)
    summary: str | None = None
    type: Literal["semantic", "episodic", "preference", "relationship", "event"] | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class MemoryClearBody(BaseModel):
    removed: int


class MemorySettingsBody(BaseModel):
    """``GET`` / ``PUT /v1/memories/settings``。"""

    enabled: bool


class MemoryExportBody(BaseModel):
    """``GET /v1/memories/export`` —— 备份用，**含已归档与已过期**。"""

    exported_at: datetime
    total: int
    memories: list[MemoryBody]


# ── State（M3）──────────────────────────────────────────


class StateBody(BaseModel):
    """``GET /v1/state`` 的只读快照。

    **没有写入端点**：`docs/03` §5 明文要求"状态变化应来自事件，而不是 LLM 自己
    直接写数据库"。客户端只能看，不能改。
    """

    companion_id: str
    emotion: dict[str, float]
    relationship: dict[str, float]
    emotion_updated_at: datetime
    relationship_updated_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str
