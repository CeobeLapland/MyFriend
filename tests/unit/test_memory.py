"""``astra.domain.memory`` 的单元测试。

重点在**不变量**：``Memory`` 必须"构造即合法"——实测模型会给越界数值
（M1 的 ``pitch=200``、M2 的 ``importance=10``），所以范围检查不是防御性代码。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from astra.domain.memory import (
    DEFAULT_MEMORY_TYPE,
    MEMORY_TTL_DAYS,
    Memory,
    MemoryCandidate,
    MemoryFilter,
    MemoryHit,
    MemoryType,
)

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _memory(**overrides: object) -> Memory:
    defaults: dict[str, object] = {
        "id": "01J000000000000000000000AA",
        "user_id": "local",
        "companion_id": "astra",
        "type": MemoryType.PREFERENCE,
        "content": "用户喜欢深夜写代码",
        "created_at": _NOW,
        "importance": 0.8,
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return Memory(**defaults)  # type: ignore[arg-type]


# ── 不变量 ──────────────────────────────────────────────


@pytest.mark.parametrize("value", [-0.01, 1.01, 10.0, float("nan"), float("inf")])
def test_importance_must_be_a_unit_number(value: float) -> None:
    with pytest.raises(ValueError, match="importance"):
        _memory(importance=value)


@pytest.mark.parametrize("value", [-1.0, 2.0, float("-inf")])
def test_confidence_must_be_a_unit_number(value: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        _memory(confidence=value)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_boundary_values_are_accepted(value: float) -> None:
    assert _memory(importance=value, confidence=value).importance == value


def test_nan_is_rejected_even_though_comparisons_are_false() -> None:
    """``nan`` 能骗过 `0 <= v <= 1` 的直觉写法，必须靠 ``isfinite`` 拦。"""
    assert not (0.0 <= math.nan <= 1.0)
    with pytest.raises(ValueError, match="importance"):
        _memory(importance=math.nan)


# ── 生命周期 ────────────────────────────────────────────


def test_memory_without_expiry_never_expires() -> None:
    memory = _memory(expires_at=None)
    assert memory.is_expired(_NOW + timedelta(days=3650)) is False
    assert memory.is_retrievable(_NOW + timedelta(days=3650)) is True


def test_expired_memory_is_not_retrievable() -> None:
    memory = _memory(expires_at=_NOW - timedelta(seconds=1))
    assert memory.is_expired(_NOW) is True
    assert memory.is_retrievable(_NOW) is False


def test_expiry_is_inclusive_at_the_boundary() -> None:
    memory = _memory(expires_at=_NOW)
    assert memory.is_expired(_NOW) is True


def test_archived_memory_is_not_retrievable_even_without_expiry() -> None:
    memory = _memory(archived_at=_NOW)
    assert memory.is_archived() is True
    assert memory.is_retrievable(_NOW) is False


# ── repetition ──────────────────────────────────────────


def test_repetition_defaults_to_zero() -> None:
    assert _memory().repetition == 0


def test_repetition_reads_from_metadata() -> None:
    assert _memory(metadata={"repetition": 3}).repetition == 3


@pytest.mark.parametrize("value", [-1, "3", None, 2.5, True])
def test_malformed_repetition_is_treated_as_zero(value: object) -> None:
    """metadata 是自由 JSON，不能相信它的类型。"""
    assert _memory(metadata={"repetition": value}).repetition == 0


# ── 类型与 TTL 策略 ─────────────────────────────────────


def test_every_memory_type_has_a_ttl_policy() -> None:
    """漏一个类型会让新记忆拿到 ``None`` 之外的意外行为——必须有显式策略。"""
    assert set(MEMORY_TTL_DAYS) == set(MemoryType)


def test_short_term_types_expire_and_long_term_ones_do_not() -> None:
    """docs/06 §8：episodic 是短期、semantic 通常是长期。"""
    assert MEMORY_TTL_DAYS[MemoryType.EPISODIC] == 7
    assert MEMORY_TTL_DAYS[MemoryType.EVENT] == 30
    for long_term in (MemoryType.SEMANTIC, MemoryType.PREFERENCE, MemoryType.RELATIONSHIP):
        assert MEMORY_TTL_DAYS[long_term] is None


def test_default_memory_type_is_semantic() -> None:
    assert DEFAULT_MEMORY_TYPE is MemoryType.SEMANTIC


# ── 其余值对象 ──────────────────────────────────────────


def test_candidate_carries_only_type_and_content() -> None:
    """刻意只有两个字段——让模型自评 importance 会把延迟从 +2.4s 推到 +4.5s。"""
    candidate = MemoryCandidate(type=MemoryType.EVENT, content="用户完成了 M1")

    assert candidate.type is MemoryType.EVENT
    assert candidate.content == "用户完成了 M1"


def test_hit_keeps_score_components() -> None:
    hit = MemoryHit(memory=_memory(), score=0.77, components={"semantic": 0.9, "recency": 0.2})

    assert hit.score == 0.77
    assert hit.components["semantic"] == 0.9


def test_filter_defaults_are_conservative() -> None:
    """默认不返回归档 / 已过期的记忆——那是"用户看不见"的两类。"""
    query = MemoryFilter(user_id="local", companion_id="astra")

    assert query.types == ()
    assert query.min_importance is None
    assert query.created_after is None
    assert query.include_archived is False
    assert query.include_expired is False
    assert query.limit is None
