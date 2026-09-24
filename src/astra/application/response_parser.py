"""把 LLM 的原始文本解析成合法的 :class:`AssistantResponse`（ADR 0011）。

**六段流水线**，每段可独立测试：

======  ==========================================  ====================
段      动作                                        失败归类
======  ==========================================  ====================
1       提取 JSON（去围栏、取首个平衡 ``{...}``、      **可重试**
        ``json.loads`` 拒绝 NaN / Infinity）
2       剪掉未知键（递归两层，含**对象数组**，        降级
        只记键名不记值）
3       补齐缺失的非 ``reply`` 字段                    降级
4       Pydantic 校验（``extra="forbid"``）            **可重试**
5       归一化枚举 + clamp 数值 + 有限性兜底            降级
6       构造领域对象（不变量由 ``domain.response`` 兜底） 降级
======  ==========================================  ====================

「可重试」由 ``LLMResponseError.retryable`` 表达，Orchestrator 是唯一的重试决策点。

**M2 新增的候选归一化**：归一化 ``type``、丢掉内容为空的、按内容去重。
**数量上限不在这里**——那是 ``Settings`` 驱动的策略，由 ``MemoryService`` 执行
（解析器只管"让数据合法"，不管"要几条"）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from astra.application.assistant_schema import (
    LIST_PAYLOAD_MODELS,
    NESTED_PAYLOAD_MODELS,
    AssistantResponsePayload,
)
from astra.domain.errors import LLMResponseError
from astra.domain.memory import MemoryCandidate
from astra.domain.response import (
    INTENSITY_RANGE,
    PITCH_RANGE,
    SPEED_RANGE,
    Action,
    AssistantResponse,
    Emotion,
    Voice,
)
from astra.domain.vocabulary import (
    DEFAULT_ACTION,
    DEFAULT_EMOTION,
    DEFAULT_VOICE_STYLE,
    normalize_action,
    normalize_emotion,
    normalize_memory_type,
    normalize_voice_style,
)

_logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: 缺失的非 reply 字段的默认值。校验前补齐，这样模型可以保持全字段必填
#: （strict 模式要求所有属性都出现在 required 里）。
_SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "emotion": {"primary": DEFAULT_EMOTION, "intensity": 0.0},
    "action": {"name": DEFAULT_ACTION, "intensity": 0.0},
    "voice": {"style": DEFAULT_VOICE_STYLE, "speed": 1.0, "pitch": 0.0},
}

#: **list 形状**的 section：缺失或类型不对都补成空列表。
_LIST_SECTIONS: tuple[str, ...] = ("memory_candidates",)


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """解析结果 + 为了得到它而做的降级动作（供可观测性使用）。"""

    response: AssistantResponse
    dropped_keys: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.dropped_keys or self.adjustments)


def parse_assistant_response(text: str) -> ParseOutcome:
    """解析 LLM 输出。

    Raises:
        LLMResponseError: 不可解析、``reply`` 缺失或为空。调用方可以重试一次。
    """
    payload = _decode_json_object(text)
    payload, dropped = prune_unknown_keys(payload)
    payload, replaced = apply_defaults(payload)

    try:
        validated = AssistantResponsePayload.model_validate(payload)
    except ValidationError as exc:
        msg = f"assistant response failed schema validation: {exc.error_count()} error(s)"
        raise LLMResponseError(msg, detail=str(exc)) from exc

    adjustments: list[str] = [f"{name}: malformed section replaced" for name in replaced]
    response = _to_domain(validated, adjustments)

    if dropped:
        _logger.warning(
            "dropped unknown keys from assistant response",
            extra={"event": "llm.response.pruned", "detail": list(dropped)},
        )
    if adjustments:
        _logger.warning(
            "normalized assistant response",
            extra={"event": "llm.response.normalized", "detail": adjustments},
        )

    return ParseOutcome(
        response=response,
        dropped_keys=dropped,
        adjustments=tuple(adjustments),
    )


# ── 段 1：提取 JSON ─────────────────────────────────────


def _decode_json_object(text: str) -> dict[str, Any]:
    candidate = _extract_first_object(_strip_code_fence(text))

    try:
        decoded = json.loads(candidate, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        msg = "assistant response is not valid JSON"
        raise LLMResponseError(msg, detail=str(exc)) from exc

    if not isinstance(decoded, dict):
        msg = "assistant response must be a JSON object"
        raise LLMResponseError(msg, detail=type(decoded).__name__)

    return decoded


def _reject_constant(name: str) -> None:
    # json.loads 默认接受 NaN / Infinity，会让下游数值静默污染
    msg = f"non-finite JSON constant is not allowed: {name}"
    raise ValueError(msg)


def _strip_code_fence(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _extract_first_object(text: str) -> str:
    """取出第一个**括号平衡**的 ``{...}``。

    不能用 rfind('}')：模型可能在 JSON 后面接着写解释文字，而解释里也可能有 `}`。
    """
    start = text.find("{")
    if start < 0:
        msg = "assistant response contains no JSON object"
        raise LLMResponseError(msg, detail=text[:200])

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    msg = "assistant response contains an unterminated JSON object"
    raise LLMResponseError(msg, detail=text[:200])


# ── 段 2：剪掉未知键 ────────────────────────────────────


def prune_unknown_keys(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """递归剪掉 payload 模型不认识的键，返回 ``(清洗后的数据, 被丢弃的键名)``。

    在校验**之前**做，这样模型可以保持 ``extra="forbid"``（strict schema 需要
    ``additionalProperties: false``），而多余键仍然只是被丢弃、不会让 turn 失败。

    支持两种嵌套形状（``assistant_schema`` 里分别登记）：

    - ``NESTED_PAYLOAD_MODELS``：值是 dict（``emotion`` / ``action`` / ``voice``）
    - ``LIST_PAYLOAD_MODELS``：值是 ``list[dict]``（``memory_candidates``）

    **数组必须单独处理**：只按 dict 走的话，候选里的多余键会原样留下，然后被
    ``extra="forbid"`` 判成校验失败——而那是**可重试**失败，等于每轮白跑一次 LLM。
    """
    dropped: list[str] = []
    cleaned: dict[str, Any] = {}

    for key, value in payload.items():
        if key not in AssistantResponsePayload.model_fields:
            dropped.append(key)
            continue

        nested_model = NESTED_PAYLOAD_MODELS.get(key)
        if nested_model is not None and isinstance(value, dict):
            cleaned[key] = _prune_object(value, nested_model, prefix=key, dropped=dropped)
            continue

        item_model = LIST_PAYLOAD_MODELS.get(key)
        if item_model is not None and isinstance(value, list):
            cleaned[key] = [
                _prune_object(item, item_model, prefix=key, dropped=dropped)
                if isinstance(item, dict)
                else item
                for item in value
            ]
            continue

        cleaned[key] = value

    return cleaned, tuple(dropped)


def _prune_object(
    value: dict[str, Any],
    model: type[BaseModel],
    *,
    prefix: str,
    dropped: list[str],
) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for nested_key, nested_value in value.items():
        if nested_key not in model.model_fields:
            dropped.append(f"{prefix}.{nested_key}")
            continue
        kept[nested_key] = nested_value
    return kept


# ── 段 3：补齐缺失字段 ──────────────────────────────────


def apply_defaults(payload: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """补齐缺失的非 ``reply`` 字段。

    返回 ``(补齐后的数据, 被整段替换掉的字段名)``。

    - **字段缺失** → 补默认，**不算降级**（`docs/04` §5 允许可选信息缺席）
    - **字段存在但类型不对**（例如 ``"emotion": "happy"``）→ 整段替换成默认，
      **算降级**并记入 adjustments——数据被丢掉了，必须可观测

    ``reply`` 没有默认值——它是回复的实质内容，缺失必须走可重试失败。
    """
    filled = dict(payload)
    replaced: list[str] = []

    for key, defaults in _SECTION_DEFAULTS.items():
        section = filled.get(key)
        if not isinstance(section, dict):
            if key in filled:
                replaced.append(key)
            filled[key] = dict(defaults)
            continue
        for field_name, default in defaults.items():
            section.setdefault(field_name, default)

    for key in _LIST_SECTIONS:
        if not isinstance(filled.get(key), list):
            if key in filled:
                replaced.append(key)
            filled[key] = []

    return filled, tuple(replaced)


# ── 段 5 / 6：归一化与构造 ──────────────────────────────


def _to_domain(validated: AssistantResponsePayload, adjustments: list[str]) -> AssistantResponse:
    reply = validated.reply.strip()
    if not reply:
        msg = "assistant response reply is empty"
        raise LLMResponseError(msg)

    emotion_primary = normalize_emotion(validated.emotion.primary)
    if emotion_primary != validated.emotion.primary:
        adjustments.append(f"emotion.primary={validated.emotion.primary!r}")

    action_name = normalize_action(validated.action.name)
    if action_name != validated.action.name:
        adjustments.append(f"action.name={validated.action.name!r}")

    voice_style = normalize_voice_style(validated.voice.style)
    if voice_style != validated.voice.style:
        adjustments.append(f"voice.style={validated.voice.style!r}")

    emotion_intensity = _clamp(
        validated.emotion.intensity, INTENSITY_RANGE, "emotion.intensity", adjustments
    )
    action_intensity = _clamp(
        validated.action.intensity, INTENSITY_RANGE, "action.intensity", adjustments
    )
    speed = _clamp(validated.voice.speed, SPEED_RANGE, "voice.speed", adjustments)
    pitch = _clamp(validated.voice.pitch, PITCH_RANGE, "voice.pitch", adjustments)

    return AssistantResponse(
        reply=reply,
        emotion=Emotion(primary=emotion_primary, intensity=emotion_intensity),
        action=Action(name=action_name, intensity=action_intensity),
        voice=Voice(style=voice_style, speed=speed, pitch=pitch),
        memory_candidates=_to_candidates(validated, adjustments),
    )


def _to_candidates(
    validated: AssistantResponsePayload, adjustments: list[str]
) -> tuple[MemoryCandidate, ...]:
    """归一化候选：类型归位、丢掉空内容、按内容去重。

    **不做数量截断**——上限是 ``Settings`` 驱动的策略，由 ``MemoryService`` 执行。
    解析器只保证"这些候选都是合法的"。

    **不在这里判断"值不值得记"**：那是 ``docs/05`` §8 的语义判断，交给
    importance / confidence 打分与去重阈值，而不是靠字符串规则硬筛。
    """
    candidates: list[MemoryCandidate] = []
    seen: set[str] = set()

    for raw in validated.memory_candidates:
        content = raw.content.strip()
        if not content:
            adjustments.append("memory_candidates: dropped empty content")
            continue
        if content in seen:
            adjustments.append("memory_candidates: dropped duplicate content")
            continue
        seen.add(content)

        memory_type = normalize_memory_type(raw.type)
        if memory_type.value != raw.type:
            adjustments.append(f"memory_candidates.type={raw.type!r}")

        candidates.append(MemoryCandidate(type=memory_type, content=content))

    return tuple(candidates)


def _clamp(
    value: float,
    bounds: tuple[float, float],
    field_name: str,
    adjustments: list[str],
) -> float:
    low, high = bounds

    if not math.isfinite(value):
        # `parse_constant` 只拦得住 NaN / Infinity 字面量；`1e400` 这种溢出会
        # 静默变成 inf，所以这里必须再兜一次。
        fallback = high if value > 0 else low
        adjustments.append(f"{field_name}=non-finite->{fallback!r}")
        return fallback

    clamped = min(max(value, low), high)
    if clamped != value:
        adjustments.append(f"{field_name}={value!r}->{clamped!r}")
    return clamped
