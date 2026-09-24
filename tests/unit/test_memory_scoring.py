"""``astra.application.memory_scoring`` 的单元测试。

这些是**启发式**，测试的目标不是"数值精确"，而是**单调性与边界**：
用户明说"记住"必须比没说更重、偏好必须比 episodic 更重、重复必须抬分、
凭空推测的内容必须拿低 confidence。数值本身会随评测调参，方向不该变。
"""

from __future__ import annotations

import pytest

from astra.application.memory_scoring import (
    DEFAULT_IMPORTANCE_WEIGHTS,
    ImportanceWeights,
    importance_signals,
    score_confidence,
    score_importance,
)
from astra.domain.memory import MemoryCandidate, MemoryType


def _candidate(
    memory_type: MemoryType = MemoryType.SEMANTIC, content: str = "用户在做本地 AI 项目"
):
    return MemoryCandidate(type=memory_type, content=content)


# ── 权重自身 ────────────────────────────────────────────


def test_default_weights_sum_to_one() -> None:
    """docs/06 §3 的公式是加权和，权重和不为 1 会让分数量纲漂移。"""
    weights = DEFAULT_IMPORTANCE_WEIGHTS
    total = (
        weights.explicit_request
        + weights.long_term_preference
        + weights.future_relevance
        + weights.relationship_significance
        + weights.repetition
    )
    assert total == pytest.approx(1.0)


# ── 信号 ────────────────────────────────────────────────


def test_explicit_request_signal_detects_common_phrasings() -> None:
    for phrase in ("请记住这个", "记一下", "别忘了这件事", "帮我记着"):
        signals = importance_signals(_candidate(), user_text=phrase)
        assert signals["explicit_request"] == 1.0


def test_explicit_request_signal_is_zero_without_a_request() -> None:
    signals = importance_signals(_candidate(), user_text="今天天气不错")

    assert signals["explicit_request"] == 0.0


def test_preference_signal_is_maximal_for_preference_type() -> None:
    signals = importance_signals(_candidate(MemoryType.PREFERENCE), user_text="我最近在做一个项目")

    assert signals["long_term_preference"] == 1.0


def test_preference_signal_picks_up_phrasing_for_other_types() -> None:
    signals = importance_signals(_candidate(MemoryType.SEMANTIC), user_text="我习惯深夜写代码")

    assert signals["long_term_preference"] == 0.6


def test_future_relevance_orders_types_sensibly() -> None:
    long_term = importance_signals(_candidate(MemoryType.PREFERENCE), user_text="x")
    event = importance_signals(_candidate(MemoryType.EVENT), user_text="x")
    episodic = importance_signals(_candidate(MemoryType.EPISODIC), user_text="x")

    assert long_term["future_relevance"] > event["future_relevance"] > episodic["future_relevance"]


def test_relationship_signal_only_for_relationship_type() -> None:
    assert (
        importance_signals(_candidate(MemoryType.RELATIONSHIP), user_text="x")[
            "relationship_significance"
        ]
        == 1.0
    )
    assert (
        importance_signals(_candidate(MemoryType.SEMANTIC), user_text="x")[
            "relationship_significance"
        ]
        == 0.0
    )


def test_repetition_signal_saturates() -> None:
    def signal(repetition: int) -> float:
        return importance_signals(_candidate(), user_text="x", repetition=repetition)["repetition"]

    assert signal(0) == 0.0
    assert 0.0 < signal(1) < signal(3)
    assert signal(3) == 1.0
    assert signal(99) == 1.0
    # 负数（脏 metadata）不能变成负贡献
    assert signal(-5) == 0.0


# ── importance 总分 ─────────────────────────────────────


def test_importance_stays_within_unit_range() -> None:
    candidate = _candidate(MemoryType.RELATIONSHIP, content="用户和我成了朋友")

    assert 0.0 <= score_importance(candidate, user_text="记住，我们是朋友", repetition=9) <= 1.0


def test_explicit_request_raises_importance() -> None:
    candidate = _candidate()

    without = score_importance(candidate, user_text="我在做一个项目")
    with_request = score_importance(candidate, user_text="我在做一个项目，请记住")

    assert with_request > without


def test_repetition_raises_importance() -> None:
    candidate = _candidate()

    once = score_importance(candidate, user_text="x", repetition=1)
    thrice = score_importance(candidate, user_text="x", repetition=3)

    assert thrice > once


def test_preference_outranks_episodic() -> None:
    preference = score_importance(_candidate(MemoryType.PREFERENCE), user_text="x")
    episodic = score_importance(_candidate(MemoryType.EPISODIC), user_text="x")

    assert preference > episodic


def test_custom_weights_are_honoured() -> None:
    """调参接口必须真的生效——否则"权重集中在一处"就只是摆设。"""
    candidate = _candidate(MemoryType.EPISODIC, content="今天吃了拉面")
    only_explicit = ImportanceWeights(
        explicit_request=1.0,
        long_term_preference=0.0,
        future_relevance=0.0,
        relationship_significance=0.0,
        repetition=0.0,
    )

    assert score_importance(candidate, user_text="今天吃了拉面", weights=only_explicit) == 0.0
    assert score_importance(candidate, user_text="记住，今天吃了拉面", weights=only_explicit) == 1.0


# ── confidence ──────────────────────────────────────────


def test_confidence_stays_within_unit_range() -> None:
    candidate = _candidate(content="用户在做本地 AI 项目")

    assert 0.0 <= score_confidence(candidate, user_text="我最近在做一个本地 AI 项目，请记住") <= 1.0


def test_explicit_request_raises_confidence() -> None:
    candidate = _candidate()

    without = score_confidence(candidate, user_text="我在做一个项目")
    with_request = score_confidence(candidate, user_text="我在做一个项目，请记住")

    assert with_request > without


def test_confidence_drops_for_content_without_user_evidence() -> None:
    """模型凭空推测的内容与用户原话几乎不重合——这正是 docs/05 §8 要过滤的。"""
    grounded = _candidate(content="用户喜欢深夜写代码")
    invented = _candidate(content="用户曾经在北海道养过一只叫小雪的猫")

    grounded_score = score_confidence(grounded, user_text="我特别喜欢深夜写代码")
    invented_score = score_confidence(invented, user_text="我特别喜欢深夜写代码")

    assert grounded_score > invented_score


def test_hedged_content_lowers_confidence() -> None:
    confident = _candidate(content="用户喜欢深夜写代码")
    hedged = _candidate(content="用户可能喜欢深夜写代码")

    assert score_confidence(hedged, user_text="我特别喜欢深夜写代码") < score_confidence(
        confident, user_text="我特别喜欢深夜写代码"
    )


def test_hedged_user_text_lowers_confidence() -> None:
    candidate = _candidate(content="用户喜欢深夜写代码")

    plain = score_confidence(candidate, user_text="我喜欢深夜写代码")
    hedged = score_confidence(candidate, user_text="我好像喜欢深夜写代码")

    assert hedged < plain


def test_empty_content_does_not_crash() -> None:
    assert 0.0 <= score_confidence(_candidate(content=""), user_text="你好") <= 1.0
