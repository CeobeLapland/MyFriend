"""情绪状态与关系状态的领域值对象（`docs/03_DATA_MODEL.md` §2、§4、§5）。

**为什么是具名字段而不是 ``Mapping[str, float]``**：`docs/03` §2 逐字段列了它们，
具名字段能在类型层面防止拼错维度名，也让 `dataclasses.replace` 的用法一目了然。

**值域由 ``__post_init__`` 强制**（与 ``domain/response.py`` / ``domain/memory.py``
同一模式）：越界即 ``ValueError``。`docs/03` §2 明文要求"值域固定为 0..1。
不要允许任意文本覆盖状态"。

**三张"调参表"都在这里**（模块常量，不进 ``Settings``）：

- :data:`EMOTION_BASELINE` / :data:`RELATIONSHIP_BASELINE` —— 角色的性格底色。
  与 `docs/06` §3 的 importance 权重同样处理：这是**语义规则**，不是运维旋钮。
- :data:`TRANSITION_RULES` —— 用户侧事件（`docs/03` §5）触发的增量。
- :data:`EMOTION_TARGETS` —— 已表达情绪对应的**目标水平**（不是增量）。

放一处是为了"调参时只改一个文件"，也是 ADR 0020 里"只有 2 个字段进 Settings"的前提。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

#: 情绪维度（`docs/03` §2 的 EmotionState，也是 `docs/schemas` 里 state_delta 的 7 个键）
EMOTION_DIMS: tuple[str, ...] = (
    "happiness",
    "energy",
    "curiosity",
    "social_need",
    "stress",
    "embarrassment",
    "excitement",
)

#: 关系维度（`docs/03` §2 的 RelationshipState）
RELATIONSHIP_DIMS: tuple[str, ...] = (
    "familiarity",
    "trust",
    "affection",
    "reliance",
    "conflict",
)

#: 情绪基线：平静、略微好奇。情绪会按半衰期回到这里。
EMOTION_BASELINE_VALUES: Mapping[str, float] = {
    "happiness": 0.55,
    "energy": 0.60,
    "curiosity": 0.65,
    "social_need": 0.45,
    "stress": 0.15,
    "embarrassment": 0.10,
    "excitement": 0.35,
}

#: 关系基线：**全 0**（刚认识，什么都还没发生）。关系只由事件累积，不衰减，
#: 所以这个基线只在"从没有过记录"时用一次。
RELATIONSHIP_BASELINE_VALUES: Mapping[str, float] = dict.fromkeys(RELATIONSHIP_DIMS, 0.0)

#: 久别重逢的判定阈值（小时）。以情绪状态的 ``updated_at`` 为"上次互动时间"的代理
#: ——状态每轮都会更新，所以它约等于最后一次 turn 的时间。
REUNION_AFTER_HOURS = 24.0


@dataclass(frozen=True, slots=True)
class EmotionState:
    """角色的情绪状态。7 个维度全部在 ``[0, 1]``。"""

    happiness: float
    energy: float
    curiosity: float
    social_need: float
    stress: float
    embarrassment: float
    excitement: float

    def __post_init__(self) -> None:
        for name in EMOTION_DIMS:
            _require_unit_range(f"emotion.{name}", getattr(self, name))

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in EMOTION_DIMS}

    def pushed_toward(self, extremes: Mapping[str, float], *, strength: float) -> EmotionState:
        """朝某个**极值**（0.0 或 1.0）走 ``strength`` 比例。

        **为什么不是加法**（实机验收发现的问题）：规则用加法 + 情绪用比例靠拢时，
        每轮净变化是 ``d - p*(h - T)``，均衡点在 ``T + d/p``。用 `docs/03` §5 的
        推荐值（``d=0.05``、``p=0.15``、``T=0.80``）算出来是 **1.13**——也就是
        **连续被夸会把 happiness 钉死在 1.0**，状态从此不再携带信息。
        改成比例推进后，规则本身也收敛，均衡点变成
        ``(p*T + s) / (p + s)``（见 ADR 0020），恒小于 1。

        关系仍用加法：那是**累积**语义，涨到 1.0 是应该的。
        """
        if strength <= 0:
            return self

        ratio = clamp_unit(strength)
        return EmotionState(
            **{
                name: clamp_unit(value + ratio * (extremes[name] - value))
                if name in extremes
                else value
                for name, value in self.as_dict().items()
            }
        )

    def decayed(self, *, elapsed_hours: float, half_life_hours: float) -> EmotionState:
        """按半衰期拉向 :data:`EMOTION_BASELINE_VALUES`。

        ``elapsed_hours <= 0``（时钟回拨 / 同一时刻）时原样返回，不做任何事——
        否则一次负的 elapsed 会算出 >1 的系数，把状态推离基线。
        """
        if elapsed_hours <= 0 or half_life_hours <= 0:
            return self

        factor = 1.0 - math.pow(0.5, elapsed_hours / half_life_hours)
        return EmotionState(
            **{
                name: clamp_unit(value + (EMOTION_BASELINE_VALUES[name] - value) * factor)
                for name, value in self.as_dict().items()
            }
        )

    def pulled_toward(self, targets: Mapping[str, float], *, pull: float) -> EmotionState:
        """按比例向 ``targets`` 靠拢。

        **这是本设计里最要紧的一处**（ADR 0020）：必须是**比例靠拢**而不是加法。
        加法会形成"模型说开心 → 更开心 → prompt 更开心 → 模型更说开心"的正反馈，
        在衰减较慢时把值顶到 1.0 并钉住；比例靠拢天然收敛到目标值。
        """
        if pull <= 0:
            return self

        ratio = clamp_unit(pull)
        return EmotionState(
            **{
                name: clamp_unit(value + (targets[name] - value) * ratio)
                if name in targets
                else value
                for name, value in self.as_dict().items()
            }
        )


@dataclass(frozen=True, slots=True)
class RelationshipState:
    """角色与用户的关系状态。5 个维度全部在 ``[0, 1]``。

    **不衰减**（ADR 0020）：`docs/06` §7 把 relationship 归入 L3 长期状态，
    "好感度随时间淡忘"不是这个产品的语义——它只由事件推动、长期累积。
    """

    familiarity: float
    trust: float
    affection: float
    reliance: float
    conflict: float

    def __post_init__(self) -> None:
        for name in RELATIONSHIP_DIMS:
            _require_unit_range(f"relationship.{name}", getattr(self, name))

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in RELATIONSHIP_DIMS}

    def with_deltas(self, deltas: Mapping[str, float]) -> RelationshipState:
        return RelationshipState(
            **{
                name: clamp_unit(getattr(self, name) + deltas.get(name, 0.0))
                for name in RELATIONSHIP_DIMS
            }
        )

    def relevance(self) -> float:
        """给 `docs/06` §4 的 ``relationship_relevance`` 用的单一标量。

        取三个正向维度的均值——"我们有多熟 / 多信任 / 多亲近"共同决定
        "这条记忆对我们关系的相关度"。``conflict`` 是负向维度，不参与。
        """
        return clamp_unit((self.familiarity + self.trust + self.affection) / 3.0)


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """一次读取到的完整状态。

    两个 ``updated_at`` 分开记：情绪的衰减以 ``emotion_updated_at`` 为锚点，
    而关系不衰减，它的时间戳只用于展示与排查。
    """

    emotion: EmotionState
    relationship: RelationshipState
    emotion_updated_at: datetime
    relationship_updated_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "emotion": self.emotion.as_dict(),
            "relationship": self.relationship.as_dict(),
            "emotion_updated_at": self.emotion_updated_at.isoformat(),
            "relationship_updated_at": self.relationship_updated_at.isoformat(),
        }


# ── 事件规则表（docs/03 §5）─────────────────────────────


@dataclass(frozen=True, slots=True)
class TransitionRule:
    """一条用户侧事件规则。

    ``emotion`` 是**极值**（0.0 或 1.0）加一个统一的 ``emotion_strength``：
    "朝这个方向走这么多比例"，**不是加法**——理由见
    :meth:`EmotionState.pushed_toward`。``relationship`` 才是增量（累积语义）。

    规则表刻意只覆盖 `docs/03` §5 明确列出的三种情形——宁可少而准，
    也不要堆一堆猜出来的关键词。
    """

    name: str
    pattern: re.Pattern[str]
    emotion: Mapping[str, float]
    relationship: Mapping[str, float]
    emotion_strength: float = 0.20


#: `docs/03` §5 的三条规则。顺序即优先级（一条消息可能同时命中多条）。
TRANSITION_RULES: tuple[TransitionRule, ...] = (
    TransitionRule(
        name="user_praised_companion",
        pattern=re.compile(r"谢谢|感谢|你真棒|真厉害|太厉害|好厉害|喜欢你|真好|辛苦|做得好|真聪明"),
        emotion={"happiness": 1.0, "stress": 0.0},
        relationship={"affection": 0.02},
    ),
    TransitionRule(
        name="user_criticised_companion",
        pattern=re.compile(r"你错了|真没用|太笨|真笨|没用|好蠢|答非所问|不对吧|你不行"),
        emotion={"happiness": 0.0, "stress": 1.0},
        relationship={"conflict": 0.03},
    ),
)

#: 久别重逢（时间规则，不是文本规则）。见 :data:`REUNION_AFTER_HOURS`。
REUNION_RULE_NAME = "user_returned_after_long_absence"
REUNION_EMOTION_EXTREMES: Mapping[str, float] = {"happiness": 1.0}
REUNION_EMOTION_STRENGTH = 0.15
REUNION_RELATIONSHIP_DELTAS: Mapping[str, float] = {"familiarity": 0.01}

#: 已表达情绪对应的**目标水平**（不是增量）。未列出的维度保持不动。
#: ``intensity`` 决定"朝这个目标走多远"：``baseline + intensity * (target - baseline)``。
EMOTION_TARGETS: Mapping[str, Mapping[str, float]] = {
    "happy": {"happiness": 0.80, "excitement": 0.55, "stress": 0.10},
    "excited": {"happiness": 0.85, "excitement": 0.90, "energy": 0.85},
    "sad": {"happiness": 0.20, "energy": 0.35, "social_need": 0.70},
    "angry": {"happiness": 0.25, "stress": 0.70, "excitement": 0.55},
    "surprised": {"excitement": 0.75, "curiosity": 0.85},
    "sleepy": {"energy": 0.20, "excitement": 0.15},
    "embarrassed": {"embarrassment": 0.75, "stress": 0.45},
    "neutral": {"stress": 0.20},
}


def emotion_targets_for(primary: str, intensity: float) -> dict[str, float]:
    """把"表达出来的情绪 + 强度"翻译成各维度的目标水平。

    目标 = ``基线 + intensity * (目标水平 - 基线)``。这样强度低时只轻微偏移，
    强度高时才靠近目标，且**永远不会越过目标水平**（不会饱和到 1.0）。
    """
    levels = EMOTION_TARGETS.get(primary)
    if not levels:
        return {}

    weight = clamp_unit(intensity)
    return {
        name: clamp_unit(
            EMOTION_BASELINE_VALUES[name] + weight * (level - EMOTION_BASELINE_VALUES[name])
        )
        for name, level in levels.items()
        if name in EMOTION_BASELINE_VALUES
    }


def baseline_emotion() -> EmotionState:
    return EmotionState(**EMOTION_BASELINE_VALUES)


def baseline_relationship() -> RelationshipState:
    return RelationshipState(**RELATIONSHIP_BASELINE_VALUES)


def clamp_unit(value: float) -> float:
    """夹到 ``[0, 1]``。非有限值（NaN / inf）按 0 处理——它们会污染整条状态链。"""
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def with_emotion(snapshot: StateSnapshot, emotion: EmotionState, *, at: datetime) -> StateSnapshot:
    return replace(snapshot, emotion=emotion, emotion_updated_at=at)


def with_relationship(
    snapshot: StateSnapshot, relationship: RelationshipState, *, at: datetime
) -> StateSnapshot:
    return replace(snapshot, relationship=relationship, relationship_updated_at=at)


def _require_unit_range(name: str, value: float) -> None:
    # NaN / Infinity 也要拦掉：它们能通过 `0 <= v <= 1` 的直觉检查，却会污染下游
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        msg = f"{name} must be a finite number within [0.0, 1.0], got {value!r}"
        raise ValueError(msg)
