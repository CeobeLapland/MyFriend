"""助手回复的结构化输出契约（Pydantic 侧）。

**M1 profile 只有 4 个字段**（ADR 0011）。`docs/schemas/assistant_response.schema.json`
保留 7 字段的完整定义作为对外契约描述；M2/M3/M6 逐阶段把
``memory_candidates`` / ``state_delta`` / ``tool_calls`` 加进来。

三个刻意的设计选择：

1. **字符串字段不加 ``enum`` 约束**。加了之后 ``domain/vocabulary`` 的归一化会变成
   死代码却仍需维护，而词表演进（M5 加表情）还要重测 prompt 契约。
   实际策略是「软约束（Layer 8 里写清单）+ 硬保证（代码归一化）」。
2. **数值字段不加 ``minimum``/``maximum``**。实测 ``strict: true`` 的 JSON Schema
   **不约束**数值边界（观察到 ``voice.pitch = 200``），加了也拦不住；范围由
   ``domain/response.py`` 的常量 + ``response_parser`` 的 clamp 强制。
3. **``extra="forbid"``**：这样生成的 schema 会带 ``additionalProperties: false``，
   这是 OpenAI 兼容接口 strict 模式的硬要求。解析侧的多余键由
   ``response_parser.prune_unknown_keys`` 在**校验之前**剪掉，所以「多余键丢弃」
   与「strict schema」不冲突。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from astra.domain.llm import JsonSchemaFormat

RESPONSE_FORMAT_NAME = "AssistantResponse"

#: 解析前需要递归剪枝的嵌套对象。必须与下面的模型定义保持一致——
#: `tests/contract/test_assistant_response_contract.py` 有同步性断言。
NESTED_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {}


class EmotionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str
    intensity: float


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    intensity: float


class VoicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str
    speed: float
    pitch: float


class AssistantResponsePayload(BaseModel):
    """M1 发送给 LLM 的 4 字段 profile。

    所有字段都是必填（无默认值）——strict 模式要求每个属性都出现在 ``required``
    里。缺失的非 ``reply`` 字段由 ``response_parser.apply_defaults`` 在校验前补齐。
    """

    model_config = ConfigDict(extra="forbid")

    reply: str
    emotion: EmotionPayload
    action: ActionPayload
    voice: VoicePayload


NESTED_PAYLOAD_MODELS.update(
    {
        "emotion": EmotionPayload,
        "action": ActionPayload,
        "voice": VoicePayload,
    }
)


def build_response_format() -> JsonSchemaFormat:
    """构造发给 provider 的 ``response_format``。"""
    return JsonSchemaFormat(
        name=RESPONSE_FORMAT_NAME,
        schema=AssistantResponsePayload.model_json_schema(),
        strict=True,
    )
