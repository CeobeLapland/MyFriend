"""WebSocket 连接注册表。

每个连接持有一个 :class:`~astra.application.event_bus.Subscription` 与一个独立的
**sender task**：sender 把事件队列里的内容推给客户端，与 turn 完全解耦。

**两层保护各管一件事**（ADR 0013）：

1. **Bus 侧**队满时淘汰最旧 → 保证 turn 永不阻塞；
2. **WS 侧** ``wait_for(send_json, timeout)`` 超时则关闭该连接
   → 保证一个卡死的 socket 只杀掉自己。

同一 ``session_id`` **允许多个连接**：M5 场景下 Unity 客户端与文本调试客户端会
同时在线；"后来者踢掉前者"会破坏"Unity 断开不影响 backend"的对称性。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from astra.application.event_bus import EventBus, Subscription
from astra.domain.events import BinaryFrame, ServerFrame
from astra.domain.ids import new_id

_logger = logging.getLogger(__name__)

DEFAULT_SEND_TIMEOUT_S = 5.0


@dataclass(slots=True)
class Connection:
    connection_id: str
    session_id: str
    websocket: WebSocket
    subscription: Subscription
    connected_at: datetime
    sender_task: asyncio.Task[None] | None = field(default=None)


class ConnectionManager:
    """管理所有活跃的 WebSocket 连接。"""

    def __init__(
        self,
        bus: EventBus,
        *,
        send_timeout_s: float = DEFAULT_SEND_TIMEOUT_S,
        queue_size: int | None = None,
    ) -> None:
        self._bus = bus
        self._send_timeout_s = send_timeout_s
        self._queue_size = queue_size
        self._connections: dict[str, Connection] = {}

    @property
    def active_count(self) -> int:
        return len(self._connections)

    def connections_for(self, session_id: str) -> tuple[Connection, ...]:
        return tuple(
            connection
            for connection in self._connections.values()
            if connection.session_id == session_id
        )

    async def connect(self, session_id: str, websocket: WebSocket) -> Connection:
        """接受握手、订阅事件、启动 sender task。"""
        await websocket.accept()

        subscription = (
            self._bus.subscribe()
            if self._queue_size is None
            else self._bus.subscribe(maxsize=self._queue_size)
        )
        connection = Connection(
            connection_id=new_id(),
            session_id=session_id,
            websocket=websocket,
            subscription=subscription,
            connected_at=datetime.now(tz=UTC),
        )
        connection.sender_task = asyncio.create_task(self._send_loop(connection))
        self._connections[connection.connection_id] = connection

        _logger.info(
            "websocket connected",
            extra={"event": "ws.connected", "detail": {"session_id": session_id}},
        )
        return connection

    async def disconnect(self, connection: Connection) -> None:
        """摘除连接并清理。**幂等**——receiver 的 ``finally`` 与 sender 的异常退出
        都会调用它。
        """
        if self._connections.pop(connection.connection_id, None) is None:
            return

        self._bus.unsubscribe(connection.subscription)

        task = connection.sender_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        _logger.info(
            "websocket disconnected",
            extra={
                "event": "ws.disconnected",
                "dropped": connection.subscription.dropped,
            },
        )

    # ── sender ──────────────────────────────────────────

    async def _send_loop(self, connection: Connection) -> None:
        try:
            while True:
                frame = await connection.subscription.get()
                if frame is None:
                    return  # 订阅已关闭

                try:
                    await asyncio.wait_for(
                        self._send(connection, frame),
                        timeout=self._send_timeout_s,
                    )
                except (TimeoutError, WebSocketDisconnect, RuntimeError):
                    # 慢的或已断的 socket 只影响它自己
                    return
        except asyncio.CancelledError:
            raise
        finally:
            # 主动关掉 socket，让阻塞在 receive 的 receiver 循环醒来并走 finally
            with contextlib.suppress(Exception):
                await connection.websocket.close()

    @staticmethod
    async def _send(connection: Connection, frame: ServerFrame) -> None:
        """按帧的类型分派：事件走文本帧，音频走二进制帧（ADR 0021）。"""
        if isinstance(frame, BinaryFrame):
            await connection.websocket.send_bytes(frame.payload)
        else:
            await connection.websocket.send_json(frame.to_dict())
