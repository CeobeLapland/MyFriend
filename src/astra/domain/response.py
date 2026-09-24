"""助手回复的领域值对象。

**M2 起是 5 个字段**：``reply`` / ``emotion`` / ``action`` / ``voice`` /
``memory_candidates``。``state_delta``（M3）、``tool_calls``（M6）仍按里程碑加回
（ADR 0011）。

M1 曾只发 4 字段：另外三个字段当时没有任何消费者，生成即丢弃，却实测多付 1.7x
延迟（完整 7 字段 15.2s/14.0s ↔ 裁剪 4 字段 8.5s/8.5s）。M2 的
``memory_candidates`` 有了消费者，但**只加回精简形态**（每条 ``type`` + ``content``）：
实测 +2.4s，而带 ``importance`` / ``confidence`` / ``expires_in_days`` 的完整形态
要 +4.5s，且模型给出的分数会越界。打分交给 ``application/memory_scoring.py``。

**职责划分**：
- **数值范围**由本模块的 ``__post_init__`` 强制（越界即 ``ValueError``）。
  实测 ``strict: true`` 的 JSON Schema **不约束** ``minimum``/``maximum``
  （观察到 ``voice.pitch = 200``、``memory_candidates[].importance = 10``），
  所以这些类型必须做到"构造即合法"。
- **词表成员资格**由 ``application/response_parser.py`` 负责，它有明确的降级
  策略与日志。值对象不该静默替调用方决定"未知情绪该变成什么"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from astra.domain.memory import MemoryCandidate

INTENSITY_RANGE = (0.0, 1.0)
SPEED_RANGE = (0.5, 2.0)
PITCH_RANGE = (-1.0, 1.0)


@dataclass(frozen=True, slots=True)
class Emotion:
    primary: str
    intensity: float

    def __post_init__(self) -> None:
        _require_range("emotion.intensity", self.intensity, *INTENSITY_RANGE)


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    intensity: float

    def __post_init__(self) -> None:
        _require_range("action.intensity", self.intensity, *INTENSITY_RANGE)


@dataclass(frozen=True, slots=True)
class Voice:
    style: str
    speed: float
    pitch: float

    def __post_init__(self) -> None:
        _require_range("voice.speed", self.speed, *SPEED_RANGE)
        _require_range("voice.pitch", self.pitch, *PITCH_RANGE)


@dataclass(frozen=True, slots=True)
class AssistantResponse:
    """一次助手回复的完整结构。"""

    reply: str
    emotion: Emotion
    action: Action
    voice: Voice
    #: M2 起有消费者（``MemoryService``）。默认空元组让 M1 的构造点无需改动。
    memory_candidates: tuple[MemoryCandidate, ...] = field(default=())


def _require_range(name: str, value: float, low: float, high: float) -> None:
    # NaN / Infinity 也要拦掉：json.loads 默认接受它们，会让下游数值静默污染
    if not math.isfinite(value) or not low <= value <= high:
        msg = f"{name} must be a finite number within [{low}, {high}], got {value!r}"
        raise ValueError(msg)
