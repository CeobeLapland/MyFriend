"""``EventBus`` 的单元测试。

重点验证 ADR 0013 的承诺：**publish 永不阻塞**、队满时**淘汰最旧**、
重复 event_id 被跳过。
"""

from __future__ import annotations

import asyncio

import pytest

from astra.application.event_bus import EventBus, Subscription
from astra.domain.events import EventType, make_event


def _event(event_id: str | None = None, *, event_type: EventType = EventType.TURN_STARTED):
    return make_event(event_type, correlation_id="01CORR", event_id=event_id)


# ── 基本投递 ────────────────────────────────────────────


def test_publish_reaches_subscriber() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    event = _event()

    bus.publish(event)

    assert asyncio.run(subscription.get()) is event


def test_publish_reaches_every_subscriber() -> None:
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()
    event = _event()

    bus.publish(event)

    assert asyncio.run(first.get()) is event
    assert asyncio.run(second.get()) is event


def test_publish_without_subscribers_is_a_noop() -> None:
    EventBus().publish(_event())


def test_subscriber_count() -> None:
    bus = EventBus()

    assert bus.subscriber_count == 0
    subscription = bus.subscribe()
    assert bus.subscriber_count == 1

    bus.unsubscribe(subscription)
    assert bus.subscriber_count == 0


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.unsubscribe(subscription)

    bus.publish(_event())

    assert subscription.closed is True
    # 队列里只剩关闭哨兵，没有事件
    assert asyncio.run(subscription.get()) is None


def test_unsubscribe_is_idempotent() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    bus.unsubscribe(subscription)
    bus.unsubscribe(subscription)

    assert bus.subscriber_count == 0


def test_events_are_delivered_in_order() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    events = [_event(f"01{index:03d}") for index in range(5)]

    for event in events:
        bus.publish(event)

    received = [asyncio.run(subscription.get()) for _ in range(5)]
    assert [event.event_id for event in received if event] == [event.event_id for event in events]


# ── 关闭 ────────────────────────────────────────────────


def test_close_wakes_up_waiting_consumer() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    async def scenario() -> object:
        waiter = asyncio.create_task(subscription.get())
        await asyncio.sleep(0)
        bus.unsubscribe(subscription)
        return await asyncio.wait_for(waiter, timeout=1.0)

    assert asyncio.run(scenario()) is None


def test_offer_after_close_is_rejected() -> None:
    subscription = Subscription()
    subscription.close()

    assert subscription.offer(_event()) is False
    assert asyncio.run(subscription.get()) is None


# ── 背压：淘汰最旧 ──────────────────────────────────────


def test_queue_overflow_drops_the_oldest_event() -> None:
    """淘汰最旧而不是最新——turn.completed 绝不能被积压的旧事件挡住。"""
    bus = EventBus()
    subscription = bus.subscribe(maxsize=3)

    for index in range(5):
        bus.publish(_event(f"01{index:03d}"))

    assert subscription.pending == 3
    assert subscription.dropped == 2

    received = [asyncio.run(subscription.get()) for _ in range(3)]
    assert [event.event_id for event in received if event] == ["01002", "01003", "01004"]


def test_publish_never_blocks_even_when_full() -> None:
    """publish 只用 put_nowait——一个连上就不读的客户端不能拖垮 turn。"""
    bus = EventBus()
    bus.subscribe(maxsize=1)

    for index in range(1000):
        bus.publish(_event(f"01{index:05d}"))


def test_slow_subscriber_does_not_affect_others() -> None:
    bus = EventBus()
    stuck = bus.subscribe(maxsize=1)
    healthy = bus.subscribe(maxsize=100)

    for index in range(20):
        bus.publish(_event(f"01{index:03d}"))

    assert stuck.dropped > 0
    assert healthy.dropped == 0
    assert healthy.pending == 20


# ── 去重 ────────────────────────────────────────────────


def test_duplicate_event_id_is_dropped() -> None:
    """docs/04 §6：同一 event 不能重复执行。"""
    bus = EventBus()
    subscription = bus.subscribe()

    bus.publish(_event("01SAME"))
    bus.publish(_event("01SAME"))

    assert subscription.pending == 1


def test_dedup_window_evicts_old_ids() -> None:
    bus = EventBus(dedup_window=2)
    subscription = bus.subscribe()

    bus.publish(_event("01A"))
    bus.publish(_event("01B"))
    bus.publish(_event("01C"))  # 把 01A 挤出窗口
    bus.publish(_event("01A"))  # 不再视为重复

    assert subscription.pending == 4


def test_distinct_events_are_not_deduped() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    for index in range(10):
        bus.publish(_event(f"01{index:03d}"))

    assert subscription.pending == 10


# ── 订阅者隔离 ──────────────────────────────────────────


def test_one_subscriber_error_does_not_break_others() -> None:
    """offer 不会抛异常，所以一个订阅者的状态不会影响别的。"""
    bus = EventBus()
    closed = bus.subscribe()
    closed.close()
    healthy = bus.subscribe()

    bus.publish(_event())

    assert healthy.pending == 1


@pytest.mark.parametrize("maxsize", [1, 2, 128])
def test_dropped_counter_is_monotonic(maxsize: int) -> None:
    bus = EventBus()
    subscription = bus.subscribe(maxsize=maxsize)
    previous = 0

    for index in range(maxsize * 3):
        bus.publish(_event(f"01{index:05d}"))
        assert subscription.dropped >= previous
        previous = subscription.dropped
