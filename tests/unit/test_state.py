"""``astra.domain.state`` 的单元测试。

重点在**性质**而不是单个数值：状态是个反馈系统，"某一次算出多少"远不如
"反复施加同一输入会不会发散 / 饱和"重要。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from astra.domain.state import (
    EMOTION_BASELINE_VALUES,
    EMOTION_DIMS,
    EMOTION_TARGETS,
    RELATIONSHIP_DIMS,
    TRANSITION_RULES,
    EmotionState,
    RelationshipState,
    StateSnapshot,
    baseline_emotion,
    baseline_relationship,
    clamp_unit,
    emotion_targets_for,
    with_emotion,
    with_relationship,
)

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _emotion(**overrides: float) -> EmotionState:
    values = dict.fromkeys(EMOTION_DIMS, 0.5)
    values.update(overrides)
    return EmotionState(**values)


def _relationship(**overrides: float) -> RelationshipState:
    values = dict.fromkeys(RELATIONSHIP_DIMS, 0.5)
    values.update(overrides)
    return RelationshipState(**values)


# ── 不变量 ──────────────────────────────────────────────


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_emotion_dims_must_be_unit_numbers(value: float) -> None:
    with pytest.raises(ValueError, match=r"emotion\.happiness"):
        _emotion(happiness=value)


@pytest.mark.parametrize("value", [-1.0, 2.0, float("-inf")])
def test_relationship_dims_must_be_unit_numbers(value: float) -> None:
    with pytest.raises(ValueError, match=r"relationship\.trust"):
        _relationship(trust=value)


def test_boundary_values_are_accepted() -> None:
    assert _emotion(happiness=0.0).happiness == 0.0
    assert _emotion(happiness=1.0).happiness == 1.0


def test_as_dict_covers_every_dimension() -> None:
    assert set(baseline_emotion().as_dict()) == set(EMOTION_DIMS)
    assert set(baseline_relationship().as_dict()) == set(RELATIONSHIP_DIMS)


def test_baselines_are_within_range() -> None:
    assert all(0.0 <= value <= 1.0 for value in EMOTION_BASELINE_VALUES.values())
    assert set(EMOTION_BASELINE_VALUES) == set(EMOTION_DIMS)


# ── clamp 与增量 ────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [(1.5, 1.0), (-0.5, 0.0), (0.3, 0.3)])
def test_clamp_unit(raw: float, expected: float) -> None:
    assert clamp_unit(raw) == expected


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_clamp_unit_treats_non_finite_as_zero(raw: float) -> None:
    assert clamp_unit(raw) == 0.0


def test_with_deltas_clamps_instead_of_raising() -> None:
    """关系增量是外部算出来的，越界必须被夹住而不是抛异常。"""
    assert _relationship(affection=0.95).with_deltas({"affection": 0.5}).affection == 1.0
    assert _relationship(affection=0.05).with_deltas({"affection": -0.5}).affection == 0.0


def test_with_deltas_ignores_unknown_dimensions() -> None:
    """规则表可以自由演进，拼错的维度名不该炸掉整条状态链。"""
    assert _relationship().with_deltas({"nonsense": 0.4}) == _relationship()


# ── 比例推进（事件规则）─────────────────────────────────


def test_push_moves_a_fraction_toward_the_extreme() -> None:
    state = _emotion(happiness=0.5)

    pushed = state.pushed_toward({"happiness": 1.0}, strength=0.2)

    assert pushed.happiness == pytest.approx(0.6)
    assert pushed.energy == state.energy  # 未列出的维度不动


def test_push_toward_zero_works_too() -> None:
    pushed = _emotion(stress=0.5).pushed_toward({"stress": 0.0}, strength=0.2)

    assert pushed.stress == pytest.approx(0.4)


def test_push_gets_weaker_near_the_extreme() -> None:
    """**这是"不饱和"的关键**：越接近极值，推进量越小。"""
    near = _emotion(happiness=0.95).pushed_toward({"happiness": 1.0}, strength=0.2)
    far = _emotion(happiness=0.2).pushed_toward({"happiness": 1.0}, strength=0.2)

    assert (1.0 - near.happiness) < (1.0 - far.happiness)
    assert near.happiness < 1.0


def test_push_never_reaches_the_extreme() -> None:
    state = _emotion(happiness=0.5)

    for _ in range(200):
        state = state.pushed_toward({"happiness": 1.0}, strength=0.2)

    assert state.happiness < 1.0
    assert state.happiness == pytest.approx(1.0, abs=1e-6)


def test_push_with_zero_strength_changes_nothing() -> None:
    state = _emotion()

    assert state.pushed_toward({"happiness": 1.0}, strength=0.0) == state


# ── 衰减 ────────────────────────────────────────────────


def test_decay_moves_halfway_toward_baseline_in_one_half_life() -> None:
    state = _emotion(happiness=1.0)
    baseline = EMOTION_BASELINE_VALUES["happiness"]

    decayed = state.decayed(elapsed_hours=2.0, half_life_hours=2.0)

    assert decayed.happiness == pytest.approx(baseline + (1.0 - baseline) * 0.5)


def test_decay_converges_to_baseline() -> None:
    decayed = _emotion(happiness=1.0, stress=1.0).decayed(elapsed_hours=200.0, half_life_hours=2.0)

    assert decayed.happiness == pytest.approx(EMOTION_BASELINE_VALUES["happiness"], abs=1e-6)
    assert decayed.stress == pytest.approx(EMOTION_BASELINE_VALUES["stress"], abs=1e-6)


def test_baseline_state_does_not_move_under_decay() -> None:
    """已经在基线上的状态，衰减不该把它推走。"""
    assert (
        baseline_emotion().decayed(elapsed_hours=100.0, half_life_hours=2.0) == baseline_emotion()
    )


@pytest.mark.parametrize("elapsed", [0.0, -1.0])
def test_decay_is_a_noop_for_non_positive_elapsed(elapsed: float) -> None:
    """时钟回拨时不能算出 >1 的系数，否则状态会被推离基线。"""
    state = _emotion(happiness=1.0)

    assert state.decayed(elapsed_hours=elapsed, half_life_hours=2.0) == state


def test_decay_stays_within_bounds_for_extreme_inputs() -> None:
    state = _emotion(**dict.fromkeys(EMOTION_DIMS, 1.0))

    for hours in (0.1, 1.0, 10.0, 1000.0):
        assert all(
            0.0 <= value <= 1.0
            for value in state.decayed(elapsed_hours=hours, half_life_hours=2.0).as_dict().values()
        )


# ── 比例靠拢（本设计最要紧的一处）───────────────────────


def test_pull_converges_to_the_target_without_overshooting() -> None:
    """**正反馈回归测试**：反复施加同一情绪必须收敛，不能顶到 1.0 并钉住。"""
    targets = emotion_targets_for("happy", 1.0)
    state = baseline_emotion()
    target_happiness = targets["happiness"]

    for _ in range(50):
        state = state.pulled_toward(targets, pull=0.15)

    # 50 轮后残差是 (1-pull)^50 ≈ 3e-4，所以容差取 1e-3 就足以证明"收敛"
    assert state.happiness == pytest.approx(target_happiness, abs=1e-3)
    assert state.happiness < 1.0


@pytest.mark.parametrize("primary", sorted(EMOTION_TARGETS))
def test_pull_never_saturates_for_any_emotion(primary: str) -> None:
    targets = emotion_targets_for(primary, 1.0)
    state = baseline_emotion()

    for _ in range(100):
        state = state.pulled_toward(targets, pull=0.5)

    assert all(0.0 <= value <= 1.0 for value in state.as_dict().values())
    # 任何一个维度都不该被推到满值——目标水平本身就留了余量
    assert all(value < 1.0 for value in state.as_dict().values())


def test_pull_with_zero_ratio_changes_nothing() -> None:
    state = baseline_emotion()

    assert state.pulled_toward(emotion_targets_for("happy", 1.0), pull=0.0) == state


def test_pull_leaves_unlisted_dimensions_untouched() -> None:
    state = _emotion()
    targets = emotion_targets_for("sleepy", 1.0)

    pulled = state.pulled_toward(targets, pull=0.5)

    for name in EMOTION_DIMS:
        if name not in targets:
            assert getattr(pulled, name) == getattr(state, name)


def test_targets_scale_with_intensity() -> None:
    """强度决定"朝目标走多远"：0 强度回到基线，1 强度到达目标水平。"""
    weak = emotion_targets_for("happy", 0.0)
    strong = emotion_targets_for("happy", 1.0)

    assert weak["happiness"] == pytest.approx(EMOTION_BASELINE_VALUES["happiness"])
    assert strong["happiness"] == pytest.approx(EMOTION_TARGETS["happy"]["happiness"])
    assert strong["happiness"] > weak["happiness"]


def test_unknown_emotion_yields_no_targets() -> None:
    """模型偶尔会给出词表外的词（parser 已归一化，这里是第二道防线）。"""
    assert emotion_targets_for("欢迎", 0.8) == {}


# ── 事件规则 ────────────────────────────────────────────


def _rule(name: str):
    return next(rule for rule in TRANSITION_RULES if rule.name == name)


@pytest.mark.parametrize("text", ["谢谢你", "你真棒", "太厉害了", "我很喜欢你", "辛苦了"])
def test_praise_rule_matches_common_phrasings(text: str) -> None:
    assert _rule("user_praised_companion").pattern.search(text)


@pytest.mark.parametrize("text", ["你错了", "真没用", "答非所问"])
def test_critique_rule_matches_common_phrasings(text: str) -> None:
    assert _rule("user_criticised_companion").pattern.search(text)


@pytest.mark.parametrize("text", ["今天天气不错", "帮我看看这段代码", "你好呀"])
def test_ordinary_text_matches_no_rule(text: str) -> None:
    assert not any(rule.pattern.search(text) for rule in TRANSITION_RULES)


def test_rule_deltas_are_small_and_within_reasonable_range() -> None:
    """状态变化是**小 nudge**：单条规则不该一步改变半个状态。"""
    for rule in TRANSITION_RULES:
        assert rule.emotion or rule.relationship
        assert 0.0 < rule.emotion_strength <= 0.5
        assert all(value in (0.0, 1.0) for value in rule.emotion.values())
        assert all(abs(value) <= 0.1 for value in rule.relationship.values())


def test_praise_rule_pushes_toward_the_top_and_critique_toward_the_bottom() -> None:
    assert _rule("user_praised_companion").emotion["happiness"] == 1.0
    assert _rule("user_criticised_companion").emotion["happiness"] == 0.0


# ── 关系 ────────────────────────────────────────────────


def test_relationship_relevance_is_the_mean_of_positive_dims() -> None:
    state = _relationship(familiarity=0.6, trust=0.3, affection=0.0, reliance=1.0, conflict=1.0)

    assert state.relevance() == pytest.approx(0.3)


def test_new_relationship_has_zero_relevance() -> None:
    """刚认识时关系分项为 0——这正是 M2 里那个恒为 0 的权重分项的起点。"""
    assert baseline_relationship().relevance() == 0.0


def test_relationship_deltas_accumulate_and_clamp() -> None:
    state = baseline_relationship()

    for _ in range(100):
        state = state.with_deltas({"affection": 0.02})

    assert state.affection == 1.0


# ── 快照 ────────────────────────────────────────────────


def _snapshot() -> StateSnapshot:
    return StateSnapshot(
        emotion=baseline_emotion(),
        relationship=baseline_relationship(),
        emotion_updated_at=_NOW,
        relationship_updated_at=_NOW,
    )


def test_snapshot_as_dict_is_json_friendly() -> None:
    payload = _snapshot().as_dict()

    assert set(payload["emotion"]) == set(EMOTION_DIMS)  # type: ignore[arg-type]
    assert payload["emotion_updated_at"] == _NOW.isoformat()


def test_snapshot_updates_are_targeted() -> None:
    snapshot = _snapshot()

    moved = with_emotion(snapshot, _emotion(happiness=0.9), at=_NOW)

    assert moved.emotion.happiness == 0.9
    assert moved.relationship == snapshot.relationship
    assert moved.relationship_updated_at == snapshot.relationship_updated_at


def test_relationship_update_does_not_touch_the_emotion_timestamp() -> None:
    snapshot = _snapshot()
    later = datetime(2026, 9, 25, tzinfo=UTC)

    moved = with_relationship(snapshot, _relationship(trust=0.8), at=later)

    assert moved.relationship.trust == 0.8
    assert moved.emotion_updated_at == snapshot.emotion_updated_at
    assert moved.relationship_updated_at == later


def test_snapshot_dict_has_no_non_finite_values() -> None:
    payload = _snapshot().as_dict()

    assert all(math.isfinite(value) for value in payload["emotion"].values())  # type: ignore[union-attr]
