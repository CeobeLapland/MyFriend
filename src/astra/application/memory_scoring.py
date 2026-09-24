"""记忆的 importance / confidence 启发式（`docs/06_MEMORY.md` §3）。

**为什么由代码打分而不是让模型自评**（ADR 0018）：
- 实测让模型输出 ``importance`` 会把每轮延迟从 +2.4s 推到 +4.5s；
- 实测它会给越界值（``importance: 10``，schema 限 ``0..1``），仍然要 clamp；
- `docs/06` §3 的公式本来就是**加权和**，输入是几个可判定的事实（用户有没有
  明说"记住"、类型是不是偏好、重复了几次），不是需要语言理解的东西。

**权重为什么在这里而不在 ``Settings``**：`docs/06` §3 自己说"这只是初始启发式，
后续可以用评测数据调参"，而 §4 才明文要求检索权重"放在配置中"。放模块常量既满足
文档，又不让已经偏大的 Settings 再涨 5 个字段。要调参时改这一个地方。

**诚实边界**：这些信号是**词面启发式**，不是语义理解。措辞变体（"帮我留个印象"）
会漏判。`docs/06` §3 的原话就是"后续可以用评测数据调参"，这里给的是可工作的初值。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from astra.domain.memory import MemoryCandidate, MemoryType

#: 用户**明确要求记住**的说法。命中即给最高权重的那个信号。
_EXPLICIT_REQUEST_RE = re.compile(r"记住|记下|记一下|别忘了|别忘记|帮我记|记着|要记得")

#: 长期偏好倾向的说法（类型不是 ``preference`` 时的补充信号）。
_PREFERENCE_RE = re.compile(r"我喜欢|我习惯|我通常|我一般|我总是|我偏爱|我讨厌")

#: 犹豫 / 推测的说法——这类内容不该被当成确定事实（`docs/05` §8）。
_HEDGE_RE = re.compile(r"可能|也许|似乎|大概|好像|应该是|不确定")


@dataclass(frozen=True, slots=True)
class ImportanceWeights:
    """`docs/06` §3 的五个权重。和应为 1.0。"""

    explicit_request: float = 0.35
    long_term_preference: float = 0.25
    future_relevance: float = 0.15
    relationship_significance: float = 0.15
    repetition: float = 0.10


DEFAULT_IMPORTANCE_WEIGHTS = ImportanceWeights()

#: 重复多少次算"足够重要"。第 3 次观察到同一事实即拿满分。
_REPETITION_SATURATION = 3

#: 各类型的"未来相关性"。越可能长期有用的类型越高。
_FUTURE_RELEVANCE: dict[MemoryType, float] = {
    MemoryType.PREFERENCE: 1.0,
    MemoryType.SEMANTIC: 1.0,
    MemoryType.RELATIONSHIP: 1.0,
    MemoryType.EVENT: 0.5,
    MemoryType.EPISODIC: 0.2,
}


def importance_signals(
    candidate: MemoryCandidate,
    *,
    user_text: str,
    repetition: int = 0,
) -> Mapping[str, float]:
    """把 `docs/06` §3 的五个信号各自算成 ``0..1``。

    单独暴露出来是为了**可观测**：只看总分的调参是盲调。
    """
    return {
        "explicit_request": 1.0 if _EXPLICIT_REQUEST_RE.search(user_text) else 0.0,
        "long_term_preference": _preference_signal(candidate, user_text),
        "future_relevance": _FUTURE_RELEVANCE.get(candidate.type, 0.5),
        "relationship_significance": (1.0 if candidate.type is MemoryType.RELATIONSHIP else 0.0),
        "repetition": min(max(repetition, 0) / _REPETITION_SATURATION, 1.0),
    }


def score_importance(
    candidate: MemoryCandidate,
    *,
    user_text: str,
    repetition: int = 0,
    weights: ImportanceWeights = DEFAULT_IMPORTANCE_WEIGHTS,
) -> float:
    """按 `docs/06` §3 的加权和给出 importance，结果落在 ``[0, 1]``。"""
    signals = importance_signals(candidate, user_text=user_text, repetition=repetition)

    total = (
        signals["explicit_request"] * weights.explicit_request
        + signals["long_term_preference"] * weights.long_term_preference
        + signals["future_relevance"] * weights.future_relevance
        + signals["relationship_significance"] * weights.relationship_significance
        + signals["repetition"] * weights.repetition
    )
    return _clamp_unit(total)


def score_confidence(candidate: MemoryCandidate, *, user_text: str) -> float:
    """这条候选有多可信（`docs/05` §5："confidence 低时不允许写成确定事实"）。

    三个信号：

    - **基础分 0.55**：能进入候选说明模型认为它有价值，但还没被证实。
    - **+0.25 用户明说"记住"**：用户主动要求保存，可信度最高。
    - **± 证据重合度**：候选内容与用户原话的字符二元组重合比例，作为"有用户依据"
      的代理。模型凭空推测出来的内容与用户原话重合度低，会拿到低分——这正是
      `docs/05` §8 要过滤的"LLM 自己推测、没有用户依据的事实"。
    - **-0.15 含糊措辞**：内容或用户原话里出现"可能 / 也许"这类词时下调。
    """
    base = 0.55
    if _EXPLICIT_REQUEST_RE.search(user_text):
        base += 0.25

    base += 0.20 * _evidence_overlap(candidate.content, user_text)

    if _HEDGE_RE.search(candidate.content) or _HEDGE_RE.search(user_text):
        base -= 0.15

    return _clamp_unit(base)


def _preference_signal(candidate: MemoryCandidate, user_text: str) -> float:
    if candidate.type is MemoryType.PREFERENCE:
        return 1.0
    return 0.6 if _PREFERENCE_RE.search(user_text) else 0.0


def _evidence_overlap(content: str, user_text: str) -> float:
    """字符二元组重合度。中文没有词边界，二元组是零依赖的可用近似。"""
    content_grams = _bigrams(content)
    if not content_grams:
        return 0.0
    return len(content_grams & _bigrams(user_text)) / len(content_grams)


def _bigrams(text: str) -> frozenset[str]:
    stripped = "".join(text.split())
    if len(stripped) < 2:
        return frozenset({stripped}) if stripped else frozenset()
    return frozenset(stripped[index : index + 2] for index in range(len(stripped) - 1))


def _clamp_unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)
