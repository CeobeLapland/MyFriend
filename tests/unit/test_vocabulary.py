"""``astra.domain.vocabulary`` 与 ``astra.domain.errors`` 的单元测试。"""

from __future__ import annotations

import pytest

from astra.domain import vocabulary as vocab
from astra.domain.errors import (
    AstraError,
    AudioFormatError,
    ConversationNotFoundError,
    ErrorCode,
    LLMDisabledError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    STTUnavailableError,
    TTSUnavailableError,
)
from astra.domain.memory import MemoryType

# ── 白名单自身的一致性 ──────────────────────────────────


def test_defaults_are_in_their_whitelists() -> None:
    assert vocab.DEFAULT_EMOTION in vocab.EMOTIONS
    assert vocab.DEFAULT_ACTION in vocab.ACTIONS
    assert vocab.DEFAULT_VOICE_STYLE in vocab.VOICE_STYLES


def test_choices_are_sorted_and_match_whitelists() -> None:
    assert tuple(sorted(vocab.EMOTIONS)) == vocab.EMOTION_CHOICES
    assert tuple(sorted(vocab.ACTIONS)) == vocab.ACTION_CHOICES
    assert tuple(sorted(vocab.VOICE_STYLES)) == vocab.VOICE_STYLE_CHOICES


def test_whitelists_match_docs() -> None:
    """白名单是协议契约，改动必须同步 docs/07 §6/§7。"""
    assert {
        "angry",
        "embarrassed",
        "excited",
        "happy",
        "neutral",
        "sad",
        "sleepy",
        "surprised",
    } == vocab.EMOTIONS
    assert {
        "idle",
        "laugh",
        "look_at_user",
        "look_away",
        "nod",
        "shake_head",
        "shy",
        "stretch",
        "think",
        "wave",
        "yawn",
    } == vocab.ACTIONS


# ── 归一化 ──────────────────────────────────────────────


@pytest.mark.parametrize("value", sorted(vocab.EMOTIONS))
def test_known_emotion_passes_through(value: str) -> None:
    assert vocab.normalize_emotion(value) == value


@pytest.mark.parametrize("value", sorted(vocab.ACTIONS))
def test_known_action_passes_through(value: str) -> None:
    assert vocab.normalize_action(value) == value


@pytest.mark.parametrize("value", sorted(vocab.VOICE_STYLES))
def test_known_voice_style_passes_through(value: str) -> None:
    assert vocab.normalize_voice_style(value) == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Happy ", "happy"), ("HAPPY", "happy"), ("Neutral", "neutral")],
)
def test_emotion_normalization_is_case_and_whitespace_insensitive(raw: str, expected: str) -> None:
    assert vocab.normalize_emotion(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Nod ", "nod"), ("WAVE", "wave"), ("Look_At_User", "look_at_user")],
)
def test_action_normalization_is_case_and_whitespace_insensitive(raw: str, expected: str) -> None:
    assert vocab.normalize_action(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  WARM  ", "warm"), ("Playful", "playful"), ("CALM", "calm")],
)
def test_voice_style_normalization_is_case_and_whitespace_insensitive(
    raw: str, expected: str
) -> None:
    assert vocab.normalize_voice_style(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["欢迎", "闲聊模式", "joy", "cheerful_but_calm", "", "neutral-ish", "😀"],
)
def test_unknown_values_fall_back_to_defaults(raw: str) -> None:
    """实测模型会写中文与自造词，必须降级而不是让下游拿到非法值。"""
    assert vocab.normalize_emotion(raw) == vocab.DEFAULT_EMOTION
    assert vocab.normalize_action(raw) == vocab.DEFAULT_ACTION
    assert vocab.normalize_voice_style(raw) == vocab.DEFAULT_VOICE_STYLE


def test_normalization_does_not_leak_cross_category_values() -> None:
    """情绪名不应被当作动作名接受（反之亦然）。"""
    assert vocab.normalize_action("happy") == vocab.DEFAULT_ACTION
    assert vocab.normalize_emotion("nod") == vocab.DEFAULT_EMOTION


# ── 记忆类型（M2）───────────────────────────────────────


def test_memory_type_choices_are_derived_from_the_enum() -> None:
    """词表必须从 ``MemoryType`` 派生——两份手抄的清单必然漂移。"""
    derived = tuple(sorted(member.value for member in MemoryType))
    assert derived == vocab.MEMORY_TYPE_CHOICES
    assert vocab.MEMORY_TYPE_CHOICES == (
        "episodic",
        "event",
        "preference",
        "relationship",
        "semantic",
    )


@pytest.mark.parametrize("value", sorted(member.value for member in MemoryType))
def test_known_memory_type_passes_through(value: str) -> None:
    assert vocab.normalize_memory_type(value) is MemoryType(value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Preference ", MemoryType.PREFERENCE), ("EPISODIC", MemoryType.EPISODIC)],
)
def test_memory_type_normalization_is_case_and_whitespace_insensitive(
    raw: str, expected: MemoryType
) -> None:
    assert vocab.normalize_memory_type(raw) is expected


@pytest.mark.parametrize("raw", ["goal", "fact", "长期目标", "", "semantic-ish"])
def test_unknown_memory_type_falls_back_to_default(raw: str) -> None:
    """``goal`` / ``fact`` 来自 docs/schemas 的示例，**不在** docs/03 的规格里。

    类型不认识不代表内容没价值，所以降级而不是丢弃候选（docs/05 §8）。
    """
    assert vocab.normalize_memory_type(raw) is MemoryType.SEMANTIC


# ── 错误码 ──────────────────────────────────────────────


def test_error_codes_are_a_closed_set() -> None:
    assert {code.value for code in ErrorCode} == {
        "llm_unavailable",
        "llm_timeout",
        "llm_disabled",
        "llm_invalid_response",
        # M4 音频：三个新码。STT/TTS 的"不可达"与"格式不支持"是客户端要分支的
        # 三种不同处境，合成一个 generic 码会让客户端只能显示"出错了"。
        "stt_unavailable",
        "tts_unavailable",
        "audio_format_error",
        "conversation_not_found",
        "invalid_input",
        "internal_error",
    }


@pytest.mark.parametrize(
    ("error_type", "code", "retryable"),
    [
        (LLMUnavailableError, ErrorCode.LLM_UNAVAILABLE, False),
        (LLMTimeoutError, ErrorCode.LLM_TIMEOUT, True),
        (LLMDisabledError, ErrorCode.LLM_DISABLED, False),
        (LLMResponseError, ErrorCode.LLM_INVALID_RESPONSE, True),
        (ConversationNotFoundError, ErrorCode.CONVERSATION_NOT_FOUND, False),
        # M4：音频侧的三个错误码都**不可重试**——重试要用户重新说一遍才有意义
        (STTUnavailableError, ErrorCode.STT_UNAVAILABLE, False),
        (TTSUnavailableError, ErrorCode.TTS_UNAVAILABLE, False),
        (AudioFormatError, ErrorCode.AUDIO_FORMAT_ERROR, False),
    ],
)
def test_error_metadata(error_type: type[AstraError], code: ErrorCode, retryable: bool) -> None:
    error = error_type("boom", detail="extra")

    assert isinstance(error, AstraError)
    assert error.code is code
    assert error.retryable is retryable
    assert error.message == "boom"
    assert error.detail == "extra"
    assert str(error) == "boom"


def test_base_error_defaults_to_internal() -> None:
    assert AstraError("x").code is ErrorCode.INTERNAL_ERROR
    assert AstraError("x").detail is None
