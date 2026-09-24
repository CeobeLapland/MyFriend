"""情绪 / 动作 / 音色风格的白名单与归一化。

**为什么白名单在代码里而不在人格 TOML 里**（ADR 0015）：这是**协议契约**——
Unity 端要按同一份词表实现表情与动作映射。放进 TOML 会产生两份真相
（Python 一份、Unity 一份），必然漂移。改为通过 ``GET /v1/config``
把这些词表分发给客户端，保持单一分发点。

依据：`docs/07_AUDIO_AND_AVATAR.md` §6（Action Model）、§7（Expression Model），
`docs/04_API_AND_EVENTS.md` §5（"未知 emotion/action 直接降级为 neutral / idle"）。

实测背景：`qwen/qwen3.5-9b` 会在自由字符串字段里写中文（观察到
``emotion.primary="欢迎"``、``action.name="闲聊模式"``），所以归一化是必需的，
不是防御性代码。
"""

from __future__ import annotations

from astra.domain.memory import DEFAULT_MEMORY_TYPE, MemoryType

#: 表情白名单（docs/07 §7）
EMOTIONS: frozenset[str] = frozenset(
    {
        "angry",
        "embarrassed",
        "excited",
        "happy",
        "neutral",
        "sad",
        "sleepy",
        "surprised",
    }
)

#: 动作白名单（docs/07 §6）
ACTIONS: frozenset[str] = frozenset(
    {
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
    }
)

#: 音色风格白名单（docs/04 §5 的 voice.style）
VOICE_STYLES: frozenset[str] = frozenset(
    {
        "calm",
        "cheerful",
        "excited",
        "gentle",
        "neutral",
        "playful",
        "serious",
        "warm",
    }
)

DEFAULT_EMOTION = "neutral"
DEFAULT_ACTION = "idle"
DEFAULT_VOICE_STYLE = "warm"

#: 排序后的稳定序列，供 prompt 渲染与 /v1/config 分发使用。
#: 用 tuple 而非 set，保证 prompt 逐字节确定（docs/10 §2 要求）。
EMOTION_CHOICES: tuple[str, ...] = tuple(sorted(EMOTIONS))
ACTION_CHOICES: tuple[str, ...] = tuple(sorted(ACTIONS))
VOICE_STYLE_CHOICES: tuple[str, ...] = tuple(sorted(VOICE_STYLES))

#: 记忆类型词表（M2）。**从 ``MemoryType`` 枚举派生**，不另抄一份——枚举是唯一真相。
MEMORY_TYPE_CHOICES: tuple[str, ...] = tuple(sorted(member.value for member in MemoryType))


def normalize_emotion(value: str) -> str:
    """归一化情绪名。未知值降级为 :data:`DEFAULT_EMOTION`。"""
    return _normalize(value, EMOTIONS, DEFAULT_EMOTION)


def normalize_action(value: str) -> str:
    """归一化动作名。未知值降级为 :data:`DEFAULT_ACTION`。"""
    return _normalize(value, ACTIONS, DEFAULT_ACTION)


def normalize_voice_style(value: str) -> str:
    """归一化音色风格。未知值降级为 :data:`DEFAULT_VOICE_STYLE`。"""
    return _normalize(value, VOICE_STYLES, DEFAULT_VOICE_STYLE)


def normalize_memory_type(value: str) -> MemoryType:
    """归一化记忆类型。未知值降级为 :data:`DEFAULT_MEMORY_TYPE`。

    与上面三个不同，这里返回枚举成员而非 ``str``——调用方需要它构造
    ``MemoryCandidate``。降级而非丢弃候选：类型不认识不代表内容没有价值
    （`docs/05` §8 的取舍原则是"内容有没有用户依据"，不是"类型名写没写对"）。
    """
    candidate = value.strip().lower()
    try:
        return MemoryType(candidate)
    except ValueError:
        return DEFAULT_MEMORY_TYPE


def _normalize(value: str, allowed: frozenset[str], default: str) -> str:
    candidate = value.strip().lower()
    return candidate if candidate in allowed else default
