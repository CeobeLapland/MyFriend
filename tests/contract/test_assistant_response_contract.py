"""助手回复解析的契约测试。

逐行覆盖 ADR 0011 的重试矩阵：**哪些情况降级、哪些情况必须失败并可重试**。
输入是固定的 mock 原文（真实模型确实产出过这些形态）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from astra.application.assistant_schema import (
    NESTED_PAYLOAD_MODELS,
    AssistantResponsePayload,
    build_response_format,
)
from astra.application.response_parser import (
    apply_defaults,
    parse_assistant_response,
    prune_unknown_keys,
)
from astra.domain.errors import LLMResponseError
from astra.domain.response import INTENSITY_RANGE, PITCH_RANGE, SPEED_RANGE
from astra.domain.vocabulary import DEFAULT_ACTION, DEFAULT_EMOTION, DEFAULT_VOICE_STYLE

_VALID: dict[str, Any] = {
    "reply": "你好呀，今天过得怎么样？",
    "emotion": {"primary": "happy", "intensity": 0.6},
    "action": {"name": "nod", "intensity": 0.4},
    "voice": {"style": "warm", "speed": 1.0, "pitch": 0.0},
}


def _raw(**overrides: Any) -> str:
    payload = json.loads(json.dumps(_VALID))
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── 正常路径 ────────────────────────────────────────────


def test_valid_response_parses() -> None:
    outcome = parse_assistant_response(_raw())

    assert outcome.response.reply == "你好呀，今天过得怎么样？"
    assert outcome.response.emotion.primary == "happy"
    assert outcome.response.emotion.intensity == 0.6
    assert outcome.response.action.name == "nod"
    assert outcome.response.voice.style == "warm"
    assert outcome.degraded is False


def test_reply_is_stripped() -> None:
    outcome = parse_assistant_response(_raw(reply="  你好  "))

    assert outcome.response.reply == "你好"


def test_json_wrapped_in_code_fence_parses() -> None:
    outcome = parse_assistant_response(f"```json\n{_raw()}\n```")

    assert outcome.response.reply == _VALID["reply"]


def test_bare_code_fence_parses() -> None:
    outcome = parse_assistant_response(f"```\n{_raw()}\n```")

    assert outcome.response.emotion.primary == "happy"


def test_trailing_prose_after_json_is_ignored() -> None:
    """模型常在 JSON 后面接着解释，而解释里可能含 `}`——必须按括号平衡取。"""
    text = _raw() + "\n\n希望这个回复符合 {你的} 期待。"

    outcome = parse_assistant_response(text)

    assert outcome.response.reply == _VALID["reply"]


def test_leading_prose_before_json_is_ignored() -> None:
    outcome = parse_assistant_response(f"好的，这是结果：\n{_raw()}")

    assert outcome.response.emotion.primary == "happy"


# ── 段 4：未知枚举降级（实测模型会写中文）───────────────


@pytest.mark.parametrize("value", ["欢迎", "joy", "开心", ""])
def test_unknown_emotion_degrades_to_default(value: str) -> None:
    outcome = parse_assistant_response(_raw(emotion={"primary": value, "intensity": 0.5}))

    assert outcome.response.emotion.primary == DEFAULT_EMOTION
    assert outcome.degraded is True
    assert any("emotion.primary" in item for item in outcome.adjustments)


@pytest.mark.parametrize("value", ["闲聊模式", "greet", "点头"])
def test_unknown_action_degrades_to_default(value: str) -> None:
    outcome = parse_assistant_response(_raw(action={"name": value, "intensity": 0.5}))

    assert outcome.response.action.name == DEFAULT_ACTION
    assert outcome.degraded is True


@pytest.mark.parametrize("value", ["温柔", "kind", "sweet"])
def test_unknown_voice_style_degrades_to_default(value: str) -> None:
    outcome = parse_assistant_response(_raw(voice={"style": value, "speed": 1.0, "pitch": 0.0}))

    assert outcome.response.voice.style == DEFAULT_VOICE_STYLE
    assert outcome.degraded is True


def test_enum_normalization_is_case_insensitive() -> None:
    outcome = parse_assistant_response(_raw(emotion={"primary": "HAPPY", "intensity": 0.5}))

    assert outcome.response.emotion.primary == "happy"


# ── 段 5：数值 clamp（实测 strict 不约束边界）───────────


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("pitch", 200.0, PITCH_RANGE[1]),
        ("pitch", -2.0, PITCH_RANGE[0]),
        ("speed", 100.0, SPEED_RANGE[1]),
        ("speed", 0.1, SPEED_RANGE[0]),
    ],
)
def test_voice_values_are_clamped(field: str, value: float, expected: float) -> None:
    """实测模型返回过 `voice.pitch = 200`（schema 限 [-1,1]），这不是理论防护。"""
    voice = dict(_VALID["voice"])
    voice[field] = value

    outcome = parse_assistant_response(_raw(voice=voice))

    assert getattr(outcome.response.voice, field) == expected
    assert outcome.degraded is True


@pytest.mark.parametrize(("value", "expected"), [(1.5, 1.0), (-0.5, 0.0), (200.0, 1.0)])
def test_emotion_intensity_is_clamped(value: float, expected: float) -> None:
    outcome = parse_assistant_response(_raw(emotion={"primary": "happy", "intensity": value}))

    assert outcome.response.emotion.intensity == expected
    assert outcome.response.emotion.intensity in INTENSITY_RANGE


def test_numeric_strings_are_coerced() -> None:
    outcome = parse_assistant_response(_raw(emotion={"primary": "happy", "intensity": "0.7"}))

    assert outcome.response.emotion.intensity == 0.7


def test_overflowing_number_is_clamped() -> None:
    """`1e400` 会静默溢出成 inf，parse_constant 拦不住它。"""
    text = _raw().replace('"pitch": 0.0', '"pitch": 1e400')

    outcome = parse_assistant_response(text)

    assert outcome.response.voice.pitch == PITCH_RANGE[1]
    assert outcome.degraded is True


# ── 段 2：多余键丢弃 ────────────────────────────────────


def test_extra_top_level_key_is_dropped() -> None:
    outcome = parse_assistant_response(_raw(mood="extra", state_delta={"happiness": 0.1}))

    assert outcome.dropped_keys == ("mood", "state_delta")
    assert outcome.degraded is True


def test_extra_nested_key_is_dropped() -> None:
    outcome = parse_assistant_response(_raw(emotion={"primary": "happy", "intensity": 0.5, "x": 1}))

    assert outcome.dropped_keys == ("emotion.x",)
    assert outcome.response.emotion.primary == "happy"


def test_tool_calls_and_memory_candidates_are_dropped() -> None:
    """M1 的 profile 不含这些字段，出现即丢弃（M2/M6 才会消费）。"""
    outcome = parse_assistant_response(
        _raw(tool_calls=[{"name": "get_time", "arguments": {}}], memory_candidates=[{"type": "x"}])
    )

    assert set(outcome.dropped_keys) == {"tool_calls", "memory_candidates"}


# ── 段 3：缺失字段补默认 ────────────────────────────────


@pytest.mark.parametrize("missing", ["emotion", "action", "voice"])
def test_missing_section_is_filled_with_defaults(missing: str) -> None:
    payload = json.loads(json.dumps(_VALID))
    del payload[missing]

    outcome = parse_assistant_response(json.dumps(payload, ensure_ascii=False))

    assert outcome.degraded is False  # 补默认不算降级
    if missing == "emotion":
        assert outcome.response.emotion.primary == DEFAULT_EMOTION
    if missing == "action":
        assert outcome.response.action.name == DEFAULT_ACTION
    if missing == "voice":
        assert outcome.response.voice.style == DEFAULT_VOICE_STYLE


def test_partially_missing_section_is_filled() -> None:
    outcome = parse_assistant_response(_raw(voice={"style": "warm"}))

    assert outcome.response.voice.speed == 1.0
    assert outcome.response.voice.pitch == 0.0


# ── 失败路径：必须可重试 ────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "这不是 JSON",
        "{",
        '{"reply": "x"',  # 未闭合
        "[]",  # 不是对象
    ],
)
def test_unparseable_response_raises_retryable_error(text: str) -> None:
    with pytest.raises(LLMResponseError) as excinfo:
        parse_assistant_response(text)

    assert excinfo.value.retryable is True


@pytest.mark.parametrize("text", ['{"reply": NaN}', '{"reply": Infinity}', '{"reply": -Infinity}'])
def test_non_finite_constants_are_rejected(text: str) -> None:
    with pytest.raises(LLMResponseError):
        parse_assistant_response(text)


@pytest.mark.parametrize("reply", ["", "   ", "\n"])
def test_empty_reply_raises_retryable_error(reply: str) -> None:
    with pytest.raises(LLMResponseError, match="reply is empty") as excinfo:
        parse_assistant_response(_raw(reply=reply))

    assert excinfo.value.retryable is True


def test_missing_reply_raises_retryable_error() -> None:
    payload = json.loads(json.dumps(_VALID))
    del payload["reply"]

    with pytest.raises(LLMResponseError) as excinfo:
        parse_assistant_response(json.dumps(payload, ensure_ascii=False))

    assert excinfo.value.retryable is True


def test_malformed_section_degrades_instead_of_failing() -> None:
    """``"emotion": "happy"`` 这类结构错误应降级到默认并记录，而不是让 turn 失败。"""
    outcome = parse_assistant_response(_raw(emotion="happy"))

    assert outcome.response.emotion.primary == DEFAULT_EMOTION
    assert outcome.degraded is True
    assert "emotion: malformed section replaced" in outcome.adjustments


# ── 辅助函数的独立契约 ──────────────────────────────────


def test_pruning_table_stays_in_sync_with_models() -> None:
    """剪枝表必须与 Payload 模型结构一致，否则「多余键丢弃」会失效。"""
    nested = {
        name
        for name, field in AssistantResponsePayload.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
    }

    assert nested == set(NESTED_PAYLOAD_MODELS)


def test_prune_keeps_known_keys_untouched() -> None:
    cleaned, dropped = prune_unknown_keys(json.loads(json.dumps(_VALID)))

    assert cleaned == _VALID
    assert dropped == ()


def test_apply_defaults_is_idempotent() -> None:
    once, replaced = apply_defaults({"reply": "x"})
    twice, replaced_again = apply_defaults(once)

    assert once == twice
    # 字段"缺失"只是补齐，不算降级；只有"存在但结构不对"才计入 replaced
    assert replaced == ()
    assert replaced_again == ()
    assert once["emotion"] == {"primary": DEFAULT_EMOTION, "intensity": 0.0}


def test_apply_defaults_reports_malformed_sections() -> None:
    _, replaced = apply_defaults({"reply": "x", "emotion": "happy", "voice": 3})

    assert replaced == ("emotion", "voice")


def test_response_format_is_strict_with_additional_properties_false() -> None:
    fmt = build_response_format()

    assert fmt.strict is True
    assert fmt.schema["additionalProperties"] is False
    for name in ("emotion", "action", "voice"):
        assert fmt.schema["$defs"][f"{name.capitalize()}Payload"]["additionalProperties"] is False


def test_response_format_requires_all_properties() -> None:
    """strict 模式要求每个属性都出现在 required 里。"""
    fmt = build_response_format()
    properties = set(fmt.schema["properties"])

    assert set(fmt.schema["required"]) == properties
