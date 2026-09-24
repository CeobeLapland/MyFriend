"""进程内事件总线（ADR 0013）。

设计要点：

- 每个订阅者一个**有界队列**（默认 128）。队满时**淘汰最旧**再放入新的，
  并累加 ``dropped`` 计数。
- ``publish`` **永不 await 消费者**——只用 ``put_nowait``。这正是"一个连上就不读
  的 WS 客户端拖垮整个 turn"的成因，必须避开。
- **为什么淘汰最旧而不是最新**：对实时 UI / Avatar 而言，**最新**状态才有意义，
  ``turn.completed`` 绝不能被积压的旧事件挡住。
- 幂等性（`docs/04` §6）：持一个固定长度的 ``deque`` + ``set`` 记录最近的
  ``event_id``，重复则记 WARNING 并跳过。

**M4 起队列里装的不只是事件**：还有 :class:`~astra.domain.events.BinaryFrame`
（音频字节）。它没有 ``event_id``，所以 :meth:`EventBus.publish_binary` 不做去重。
两者共用同一个队列是刻意的——``tts.started`` → 若干二进制帧 → ``tts.completed``
的顺序必须保持，分成两条通道就保证不了。

``publish`` 是**同步**的（不写成 ``async def``）：它从不 await，写成 async 会让
调用方误以为需要 await，ruff 的 RUF029 也会拦下来。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Iterator

from astra.domain.events import BinaryFrame, EventEnvelope, ServerFrame

_logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 128
DEFAULT_DEDUP_WINDOW = 512

#: 队列里的关闭哨兵。``get()`` 收到它就返回 ``None``。
_CLOSED = None


class Subscription:
    """一个订阅者的事件队列。

    只能通过 :class:`EventBus` 创建与销毁。
    """

    def __init__(self, *, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[ServerFrame | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        """队列里的条目数。**关闭后为 1**——那一个是唤醒消费者的关闭哨兵。"""
        return self._queue.qsize()

    def offer(self, frame: ServerFrame) -> bool:
        """非阻塞投递。队满时淘汰最旧的一条。返回是否发生了淘汰。"""
        if self._closed:
            return False

        try:
            self._queue.put_nowait(frame)
            return False
        except asyncio.QueueFull:
            pass

        # 淘汰最旧，为新的腾位置
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()

        self.dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(frame)

        return True

    async def get(self) -> ServerFrame | None:
        """取下一个帧。订阅已关闭且队列已空时返回 ``None``。"""
        frame = await self._queue.get()
        return None if frame is _CLOSED else frame

    def close(self) -> None:
        """关闭订阅并唤醒正在等待的消费者。幂等。"""
        if self._closed:
            return
        self._closed = True

        # 先清空，保证哨兵不会被淘汰逻辑挤掉
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_CLOSED)


class EventBus:
    """极简的进程内 pub/sub。"""

    def __init__(self, *, dedup_window: int = DEFAULT_DEDUP_WINDOW) -> None:
        self._subscribers: set[Subscription] = set()
        self._seen: deque[str] = deque(maxlen=dedup_window)
        self._seen_set: set[str] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, *, maxsize: int = DEFAULT_QUEUE_SIZE) -> Subscription:
        subscription = Subscription(maxsize=maxsize)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """摘除订阅并关闭它。幂等。"""
        self._subscribers.discard(subscription)
        subscription.close()

    def publish(self, event: EventEnvelope) -> None:
        """把事件投递给所有订阅者。**不会阻塞**。"""
        if self._is_duplicate(event.event_id):
            _logger.warning(
                "dropped duplicate event",
                extra={"event": "bus.duplicate", "detail": {"type": event.type}},
            )
            return

        for subscription in tuple(self._subscribers):
            if subscription.offer(event):
                _logger.warning(
                    "slow subscriber dropped the oldest event",
                    extra={"event": "bus.slow_consumer", "dropped": subscription.dropped},
                )

    def publish_binary(self, payload: bytes) -> None:
        """把一段二进制负载（音频）投递给所有订阅者。**不会阻塞**。

        与 :meth:`publish` 的区别只有一点：**不做幂等去重**——二进制帧没有
        ``event_id``，去重无从谈起（`docs/04` §6 的幂等性针对的是事件）。

        顺序由队列保证：``tts.started`` → 若干二进制帧 → ``tts.completed``
        在同一个队列里，消费者看到的顺序与发布顺序一致。
        """
        frame = BinaryFrame(payload=payload)
        for subscription in tuple(self._subscribers):
            if subscription.offer(frame):
                _logger.warning(
                    "slow subscriber dropped the oldest frame",
                    extra={"event": "bus.slow_consumer", "dropped": subscription.dropped},
                )

    # ── 去重 ────────────────────────────────────────────

    def _is_duplicate(self, event_id: str) -> bool:
        if event_id in self._seen_set:
            return True

        if len(self._seen) == self._seen.maxlen and self._seen.maxlen is not None:
            self._seen_set.discard(self._seen[0])

        self._seen.append(event_id)
        self._seen_set.add(event_id)
        return False

    # ── 测试辅助 ────────────────────────────────────────

    def __iter__(self) -> Iterator[Subscription]:  # pragma: no cover - 调试用
        return iter(tuple(self._subscribers))
