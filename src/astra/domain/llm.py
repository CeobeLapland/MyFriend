"""LLM 端口的中立数据模型。

这些类型必须**与 provider 无关**：不出现 lmstudio / httpx / OpenAI 专有的字段名。
provider 专有参数（以及「怎么把 ``reasoning_effort`` 发给某个后端」）只允许出现在
``astra/providers/**``（ADR 0010）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from astra.domain.conversation import Role


class ReasoningEffort(StrEnum):
    """思考深度。

    ``NONE`` 是本项目的默认值：实测 ``qwen/qwen3.5-9b`` 默认开启思考，
    一次简单问候要 15.4s 且思考会吃光 token 预算；置为 ``none`` 后降到 3.4s。
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class JsonSchemaFormat:
    """要求 provider 按给定 JSON Schema 产出结构化输出。"""

    name: str
    schema: Mapping[str, Any]
    strict: bool = True


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """一次生成请求。

    ``system_prompt`` 与 ``messages`` 分开：前者是 8 层组装好的成品
    （``docs/05`` §1 的 Layer 0–5、7、8），后者只承载 Layer 6（当前对话）。
    分开便于契约测试直接断言 prompt 分层。
    """

    system_prompt: str
    messages: tuple[LLMMessage, ...]
    response_format: JsonSchemaFormat | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: 实测用途：判断「思考是否真的关掉了」，以及诊断 content 为空的原因
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次生成的结果。

    ``reasoning_present`` 是布尔而非 ``reasoning: str``——刻意为之：
    ``logging_setup.SENSITIVE_KEYS`` 里没有 ``reasoning``，一旦思考正文被写进
    日志的 ``extra`` 就会原样落盘。用布尔让这件事在类型层面不可能发生。
    """

    text: str
    model: str
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    latency_ms: float = 0.0
    reasoning_present: bool = False
    response_id: str | None = None
