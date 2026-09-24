"""``EventBus`` 的单元测试。

重点验证 ADR 0013 的承诺：**publish 永不阻塞**、队满时**淘汰最旧**、
重复 event_id 被跳过。
"""

from __future__ import annotations

import asyncio

import pytest

from astra.application.event_bus import EventBus, Subscription
from astra.domain.events import BinaryFrame, EventType, make_event


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


# ── 二进制帧（M4，ADR 0021）─────────────────────────────


def test_publish_binary_reaches_subscriber_as_a_binary_frame() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    bus.publish_binary(b"\x01\x02\x03")

    frame = asyncio.run(subscription.get())
    assert isinstance(frame, BinaryFrame)
    assert frame.payload == b"\x01\x02\x03"


def test_events_and_binary_frames_share_one_ordered_queue() -> None:
    """``tts.started`` → 音频帧 → ``tts.completed`` 的顺序必须保持。

    分成两条通道就保证不了——所以它们共用一个队列（这正是本测试守的东西）。
    """
    bus = EventBus()
    subscription = bus.subscribe()

    bus.publish(_event(event_type=EventType.TTS_STARTED))
    bus.publish_binary(b"chunk-1")
    bus.publish_binary(b"chunk-2")
    bus.publish(_event(event_type=EventType.TTS_COMPLETED))

    frames = [asyncio.run(subscription.get()) for _ in range(4)]

    assert [type(frame).__name__ for frame in frames] == [
        "EventEnvelope",
        "BinaryFrame",
        "BinaryFrame",
        "EventEnvelope",
    ]
    assert frames[1].payload == b"chunk-1"
    assert frames[3].type == "tts.completed"


def test_identical_binary_frames_are_both_delivered() -> None:
    """二进制帧没有 ``event_id``，**不进幂等去重**（docs/04 §6 只管事件）。"""
    bus = EventBus()
    subscription = bus.subscribe()

    bus.publish_binary(b"same")
    bus.publish_binary(b"same")

    assert subscription.pending == 2


def test_binary_frame_can_be_evicted_by_overflow() -> None:
    """队满时"淘汰最旧"对二进制帧一视同仁——这会丢掉音频中间帧。

    本地回环 + 128 深度队列下概率极低，客户端可用 ``tts.started.chunk_count``
    自检。这是**已知局限**，记在 ADR 0021 与 README，不是 bug。
    """
    bus = EventBus()
    subscription = bus.subscribe(maxsize=2)

    bus.publish_binary(b"first")
    bus.publish(_event())
    bus.publish(_event(event_type=EventType.TURN_COMPLETED))

    frames = [asyncio.run(subscription.get()) for _ in range(2)]

    assert subscription.dropped == 1
    assert not any(isinstance(frame, BinaryFrame) for frame in frames)


def test_publish_binary_without_subscribers_is_a_noop() -> None:
    bus = EventBus()

    bus.publish_binary(b"\x00")  # 不该抛

    assert bus.subscriber_count == 0
