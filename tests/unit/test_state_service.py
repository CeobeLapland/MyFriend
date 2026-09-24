"""``StateService`` 的单元测试。

重点：惰性衰减**不重复施加**、事件规则命中、久别重逢、以及并发不丢更新。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.application.event_bus import EventBus
from astra.application.state_service import StateConfig, StateService
from astra.domain.events import EventType
from astra.domain.response import Emotion
from astra.domain.state import (
    EMOTION_BASELINE_VALUES,
    baseline_emotion,
    baseline_relationship,
)
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.db.state_repository import SQLiteStateStore

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_COMPANION = "astra"
_HALF_LIFE = 120


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _happy(intensity: float = 0.75) -> Emotion:
    return Emotion(primary="happy", intensity=intensity)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStateStore:
    db_path = tmp_path / "astra.db"
    upgrade(db_path)
    return SQLiteStateStore(db_path)


class _Clock:
    """可推进的时钟——衰减与久别重逢都要靠它。"""

    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _service(
    store: SQLiteStateStore, clock: _Clock, *, bus: EventBus | None = None, **overrides: object
) -> StateService:
    params: dict[str, object] = {
        "companion_id": _COMPANION,
        "decay_half_life_minutes": _HALF_LIFE,
    }
    params.update(overrides)
    return StateService(store, config=StateConfig(**params), bus=bus, clock=clock)  # type: ignore[arg-type]


# ── 读取与基线 ──────────────────────────────────────────


def test_first_load_returns_the_baseline(store: SQLiteStateStore, clock: _Clock) -> None:
    snapshot = _run(_service(store, clock).load_snapshot())

    assert snapshot.emotion == baseline_emotion()
    assert snapshot.relationship == baseline_relationship()


def test_load_does_not_write_anything(store: SQLiteStateStore, clock: _Clock) -> None:
    """惰性衰减是纯函数：读不该产生副作用，否则读多少次就衰减多少次。"""
    service = _service(store, clock)
    _run(service.load_snapshot())
    _run(service.load_snapshot())

    assert _run(store.load(_COMPANION)) is None


def test_reading_twice_does_not_double_decay(store: SQLiteStateStore, clock: _Clock) -> None:
    """**这条锁住"锚点"设计**：有效值是 (锚点, now) 的纯函数。"""
    service = _service(store, clock)
    _run(service.apply_turn(user_text="你好", emotion=_happy(1.0)))
    clock.advance(hours=2)

    first = _run(service.load_snapshot())
    second = _run(service.load_snapshot())

    assert first.emotion == second.emotion


# ── 事件规则 ────────────────────────────────────────────


def test_praise_raises_happiness_and_affection(store: SQLiteStateStore, clock: _Clock) -> None:
    """验收核心：user praises companion -> happiness increases。"""
    change = _run(_service(store, clock).apply_turn(user_text="谢谢你，你真的很厉害"))

    assert change is not None
    assert change.snapshot.emotion.happiness > EMOTION_BASELINE_VALUES["happiness"]
    assert change.snapshot.relationship.affection > 0.0
    assert "user_praised_companion" in change.rules


def test_critique_lowers_happiness_and_raises_conflict(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    change = _run(_service(store, clock).apply_turn(user_text="你错了，答非所问"))

    assert change is not None
    assert change.snapshot.emotion.happiness < EMOTION_BASELINE_VALUES["happiness"]
    assert change.snapshot.relationship.conflict > 0.0
    assert "user_criticised_companion" in change.rules


def test_ordinary_message_hits_no_rule(store: SQLiteStateStore, clock: _Clock) -> None:
    change = _run(_service(store, clock).apply_turn(user_text="今天天气不错"))

    assert change is not None
    assert change.rules == ()
    assert change.emotion_deltas == {}
    assert change.relationship_deltas == {}


def test_relationship_timestamp_is_not_touched_without_a_relationship_delta(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    """没有关系增量就不该刷新它的时间戳——否则"最近更新"会撒谎。"""
    service = _service(store, clock)
    first = _run(service.apply_turn(user_text="谢谢你"))
    assert first is not None
    stamped = first.snapshot.relationship_updated_at

    clock.advance(minutes=5)
    second = _run(service.apply_turn(user_text="今天天气不错"))

    assert second is not None
    assert second.snapshot.relationship_updated_at == stamped


# ── 久别重逢 ────────────────────────────────────────────


def test_long_absence_raises_happiness_and_familiarity(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    service = _service(store, clock)
    _run(service.apply_turn(user_text="你好"))
    clock.advance(hours=30)

    change = _run(service.apply_turn(user_text="我回来了"))

    assert change is not None
    assert "user_returned_after_long_absence" in change.rules
    assert change.snapshot.relationship.familiarity > 0.0


def test_short_absence_does_not_trigger_reunion(store: SQLiteStateStore, clock: _Clock) -> None:
    service = _service(store, clock)
    _run(service.apply_turn(user_text="你好"))
    clock.advance(hours=1)

    change = _run(service.apply_turn(user_text="在吗"))

    assert change is not None
    assert "user_returned_after_long_absence" not in change.rules


# ── 衰减 ────────────────────────────────────────────────


def test_emotion_decays_toward_baseline_over_time(store: SQLiteStateStore, clock: _Clock) -> None:
    service = _service(store, clock)
    raised = _run(service.apply_turn(user_text="谢谢你，太厉害了"))
    assert raised is not None
    peak = raised.snapshot.emotion.happiness
    assert peak > EMOTION_BASELINE_VALUES["happiness"]

    clock.advance(minutes=_HALF_LIFE)
    after = _run(service.load_snapshot())

    baseline = EMOTION_BASELINE_VALUES["happiness"]
    assert after.emotion.happiness == pytest.approx(baseline + (peak - baseline) * 0.5, abs=1e-6)


def test_emotion_returns_to_baseline_after_many_half_lives(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    service = _service(store, clock)
    _run(service.apply_turn(user_text="谢谢你"))
    clock.advance(minutes=_HALF_LIFE * 20)

    after = _run(service.load_snapshot())

    assert after.emotion.happiness == pytest.approx(EMOTION_BASELINE_VALUES["happiness"], abs=1e-6)


def test_relationship_does_not_decay(store: SQLiteStateStore, clock: _Clock) -> None:
    """关系只累加、不衰减（ADR 0020）。"""
    service = _service(store, clock)
    raised = _run(service.apply_turn(user_text="谢谢你，我很喜欢你"))
    assert raised is not None
    affection = raised.snapshot.relationship.affection

    clock.advance(days=365)
    after = _run(service.load_snapshot())

    assert after.relationship.affection == affection


# ── 情绪靠拢（正反馈防线）───────────────────────────────


def test_repeated_happy_does_not_saturate(store: SQLiteStateStore, clock: _Clock) -> None:
    """**正反馈回归测试**：连说 50 轮"开心"也不能把 happiness 顶到 1.0。"""
    service = _service(store, clock)
    for _ in range(50):
        _run(service.apply_turn(user_text="你好", emotion=_happy(1.0)))

    snapshot = _run(service.load_snapshot())

    assert 0.0 <= snapshot.emotion.happiness <= 1.0
    assert snapshot.emotion.happiness < 1.0
    # 目标水平是 0.80（EMOTION_TARGETS["happy"]），应该收敛到它附近而不是满值
    assert snapshot.emotion.happiness == pytest.approx(0.80, abs=0.02)


def test_repeated_praise_does_not_saturate(store: SQLiteStateStore, clock: _Clock) -> None:
    """**实机验收发现的回归**：事件规则用加法 + 情绪用比例靠拢时，
    均衡点在 ``T + d/p ≈ 1.13``，连续被夸会把 happiness 钉死在 1.0。
    改成比例推进后均衡点恒小于 1（ADR 0020）。
    """
    service = _service(store, clock)
    for _ in range(20):
        _run(service.apply_turn(user_text="谢谢你，你真的很厉害", emotion=_happy(1.0)))

    snapshot = _run(service.load_snapshot())

    assert snapshot.emotion.happiness < 1.0
    assert snapshot.emotion.happiness == pytest.approx(0.91, abs=0.03)
    # 关系是**累积**语义：20 轮 × 0.02 = 0.40（还没碰到上限）
    assert snapshot.relationship.affection == pytest.approx(0.40, abs=1e-6)


def test_praise_gets_weaker_as_happiness_approaches_the_top(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    """越接近极值推进量越小——这正是"不饱和"的机制。"""
    service = _service(store, clock, pull=0.0)
    first = _run(service.apply_turn(user_text="谢谢你"))
    assert first is not None
    early = first.snapshot.emotion.happiness - EMOTION_BASELINE_VALUES["happiness"]

    for _ in range(10):
        _run(service.apply_turn(user_text="谢谢你"))
    last = _run(service.apply_turn(user_text="谢谢你"))
    assert last is not None
    before_last = _run(service.load_snapshot())
    late = last.snapshot.emotion.happiness - before_last.emotion.happiness

    assert late < early


def test_expression_moves_state_even_without_any_rule(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    """模型说开心但用户没说任何触发词时，情绪也该动一点。"""
    change = _run(_service(store, clock).apply_turn(user_text="嗯", emotion=_happy(1.0)))

    assert change is not None
    assert change.snapshot.emotion.happiness > EMOTION_BASELINE_VALUES["happiness"]
    assert change.rules == ()
    # 靠拢不来自规则，但它**确实改变了状态**——所以事件必须发得出去，
    # 而且 payload 的 deltas 报的就是这个实际变化量
    assert change.emotion_deltas["happiness"] > 0.0
    assert change.emotion_changed is True


def test_pull_of_zero_disables_expression_influence(store: SQLiteStateStore, clock: _Clock) -> None:
    change = _run(_service(store, clock, pull=0.0).apply_turn(user_text="嗯", emotion=_happy(1.0)))

    assert change is not None
    assert change.snapshot.emotion == baseline_emotion()


# ── 事件 ────────────────────────────────────────────────


async def _drain(subscription: object) -> list:
    """把订阅队列里现有的事件读出来（同一个事件循环内调用）。"""
    events = []
    while subscription.pending:  # type: ignore[attr-defined]
        event = await subscription.get()  # type: ignore[attr-defined]
        if event is None:
            break
        events.append(event)
    return events


def test_publishes_emotion_event_on_change(store: SQLiteStateStore, clock: _Clock) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    service = _service(store, clock, bus=bus)

    async def scenario() -> list:
        await service.apply_turn(user_text="谢谢你")
        return await _drain(subscription)

    events = asyncio.run(scenario())

    # 称赞同时改情绪与关系，所以两条事件都该发
    types = [event.type for event in events]
    assert types == [EventType.STATE_EMOTION_UPDATED, EventType.STATE_RELATIONSHIP_UPDATED]

    emotion_event = events[0]
    # payload 报的是**实际变化量**，不是规则表里的意图——已经接近极值时两者差很多
    assert emotion_event.payload["deltas"]["happiness"] > 0.0
    assert "user_praised_companion" in emotion_event.payload["rules"]
    assert emotion_event.payload["emotion"]["happiness"] > EMOTION_BASELINE_VALUES["happiness"]


def test_publishes_relationship_event_only_when_it_changes(
    store: SQLiteStateStore, clock: _Clock
) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    service = _service(store, clock, bus=bus)

    async def scenario() -> list:
        await service.apply_turn(user_text="谢谢你")  # 情绪 + 关系都变
        await service.apply_turn(user_text="嗯", emotion=_happy(1.0))  # 只有情绪变
        return await _drain(subscription)

    types = [event.type for event in asyncio.run(scenario())]

    assert types.count(EventType.STATE_EMOTION_UPDATED) == 2
    assert types.count(EventType.STATE_RELATIONSHIP_UPDATED) == 1


def test_no_event_when_nothing_changed(store: SQLiteStateStore, clock: _Clock) -> None:
    """状态没变就不该发事件——否则 WS 上全是噪声。"""
    bus = EventBus()
    subscription = bus.subscribe()
    service = _service(store, clock, bus=bus, pull=0.0)

    async def scenario() -> list:
        await service.apply_turn(user_text="今天天气不错")
        return await _drain(subscription)

    assert asyncio.run(scenario()) == []


# ── 并发 ────────────────────────────────────────────────


def test_concurrent_turns_do_not_lose_updates(store: SQLiteStateStore, clock: _Clock) -> None:
    """两个会话同时称赞 → 两次增量都必须生效（专用锁的意义）。"""
    service = _service(store, clock)

    async def scenario() -> None:
        await asyncio.gather(
            service.apply_turn(user_text="谢谢你"),
            service.apply_turn(user_text="谢谢你"),
        )

    asyncio.run(scenario())

    snapshot = _run(service.load_snapshot())
    assert snapshot.relationship.affection == pytest.approx(0.04)  # 0.02 × 2


# ── reset ───────────────────────────────────────────────


def test_reset_returns_to_baseline(store: SQLiteStateStore, clock: _Clock) -> None:
    service = _service(store, clock)
    _run(service.apply_turn(user_text="谢谢你"))

    _run(service.reset())

    assert _run(service.load_snapshot()).emotion == baseline_emotion()


# ── 降级 ────────────────────────────────────────────────


class _BrokenStore:
    async def load(self, companion_id: str) -> object:
        raise RuntimeError("load exploded")

    async def save(self, companion_id: str, snapshot: object) -> None:
        raise RuntimeError("save exploded")

    async def reset(self, companion_id: str) -> None:
        raise RuntimeError("reset exploded")


def test_load_failure_falls_back_to_baseline(clock: _Clock) -> None:
    service = StateService(_BrokenStore(), config=StateConfig(companion_id=_COMPANION), clock=clock)

    assert _run(service.load_snapshot()).emotion == baseline_emotion()


def test_apply_failure_returns_none_instead_of_raising(clock: _Clock) -> None:
    """状态失败绝不能让 turn 失败。"""
    service = StateService(_BrokenStore(), config=StateConfig(companion_id=_COMPANION), clock=clock)

    assert _run(service.apply_turn(user_text="谢谢你")) is None
