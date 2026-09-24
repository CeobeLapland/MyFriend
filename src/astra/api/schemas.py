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


class ErrorBody(BaseModel):
    code: str
    message: str
