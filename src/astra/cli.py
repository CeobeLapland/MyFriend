"""命令行入口。

astra serve            启动 HTTP 服务（默认以监督进程运行，支持"一键重启"）
astra serve --no-supervisor   前台单进程运行（没有重启能力）
astra migrate          应用数据库迁移（幂等）
astra chat [--once X]  连上 WebSocket 聊天（REPL 或单轮）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import uvicorn
import websockets
from websockets.asyncio.client import ClientConnection

from astra import __version__
from astra.domain.ids import new_id
from astra.infrastructure.db.migrations import upgrade
from astra.logging_setup import configure_logging
from astra.settings import Settings, get_settings
from astra.supervisor import (
    EXIT_RESTART,
    SUPERVISED_ENV,
    child_command,
    is_supervised,
    run_supervised,
)

_logger = logging.getLogger("astra.cli")

#: WebSocket 协议层心跳。用协议层而不是自定义 ping 事件——不擅自扩协议。
_WS_PING_INTERVAL_S = 20
_WS_PING_TIMEOUT_S = 20

#: 一轮对话结束的事件
_TURN_TERMINATORS = frozenset({"turn.completed", "turn.failed"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra", description="Astra — local-first AI companion")
    parser.add_argument("--version", action="version", version=f"astra {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="启动 HTTP 服务（默认带重启监督）")
    serve_parser.add_argument(
        "--no-supervisor",
        action="store_true",
        help="前台单进程运行：没有'一键重启'能力（供脚本、或自己当监督者的桌面壳使用）",
    )
    subparsers.add_parser("migrate", help="应用数据库迁移（幂等）")

    chat_parser = subparsers.add_parser("chat", help="连上 WebSocket 聊天")
    chat_parser.add_argument("--once", metavar="TEXT", help="只发一轮然后退出（脚本化验收用）")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "migrate":
        return _migrate(settings)
    if args.command == "chat":
        return _chat(settings, once=args.once)
    return _serve(settings, supervised=not args.no_supervisor)


# ── serve / migrate ─────────────────────────────────────


def _migrate(settings: Settings) -> int:
    _setup_logging(settings)

    applied = upgrade(settings.db_path)
    if applied:
        _logger.info(
            "applied migrations: %s",
            ", ".join(applied),
            extra={"event": "db.migrated", "detail": applied},
        )
    else:
        _logger.info("database already up to date", extra={"event": "db.up_to_date"})

    return 0


def _serve(settings: Settings, *, supervised: bool = True) -> int:
    """启动服务。**默认带监督**：子进程请求重启（退出码 75）时把它拉起来。

    监督者的存在是 Studio"一键重启"的前提（ADR 0026 §1）；``--no-supervisor``
    是逃生门，此时重启端点会如实返回 409。
    """
    _setup_logging(settings)

    if supervised:
        return run_supervised(child_command(), {**os.environ, SUPERVISED_ENV: "1"})

    return _serve_foreground(settings)


def _serve_foreground(settings: Settings) -> int:
    """真正跑 uvicorn 的进程。

    **用显式的 ``Server`` 对象而不是 ``uvicorn.run``**：重启端点要把它 ``should_exit``
    置位，这里再把"是不是重启请求"翻译成退出码（75 = 请重启我）。
    """
    # 延迟导入：应用在 import 时构造，放到日志配置之后
    from astra.main import app

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.http_host,
            port=settings.http_port,
            # log_config=None：避免 uvicorn 自己再装一套 handler，导致格式不一致与重复输出
            log_config=None,
            ws_ping_interval=_WS_PING_INTERVAL_S,
            ws_ping_timeout=_WS_PING_TIMEOUT_S,
        )
    )

    # 管理端点从 app.state 取这两样东西（只有 uvicorn 进程里有；测试里是缺省值）
    app.state.uvicorn_server = server
    app.state.supervised = is_supervised()
    app.state.restart_requested = False

    server.run()

    return EXIT_RESTART if app.state.restart_requested else 0


# ── chat ────────────────────────────────────────────────


def _chat(settings: Settings, *, once: str | None) -> int:
    _setup_logging(settings)
    try:
        return asyncio.run(_chat_session(settings, once))
    except (OSError, websockets.WebSocketException) as exc:
        print(
            f"连不上 ws://{settings.http_host}:{settings.http_port} —— "
            f"`astra serve` 在跑吗？（{exc}）",
            file=sys.stderr,
        )
        return 1


async def _chat_session(settings: Settings, once: str | None) -> int:
    uri = f"ws://{settings.http_host}:{settings.http_port}/ws/v1/session/{new_id()}"

    async with websockets.connect(uri) as socket:
        if once is not None:
            await _send_text(socket, once)
            await _drain(socket)
            return 0
        return await _repl(socket)


async def _repl(socket: ClientConnection) -> int:
    loop = asyncio.get_running_loop()
    print("连上了。输入内容回车发送，/quit 退出。")

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return 0

        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0

        await _send_text(socket, text)
        await _drain(socket)


async def _send_text(socket: ClientConnection, text: str) -> None:
    await socket.send(
        json.dumps(
            {"type": "input.user_text", "version": 1, "payload": {"text": text}},
            ensure_ascii=False,
        )
    )


async def _drain(socket: ClientConnection) -> None:
    """读到本轮结束（``turn.completed`` / ``turn.failed``）。"""
    while True:
        event = json.loads(await socket.recv())
        _print_event(event)
        if event.get("type") in _TURN_TERMINATORS:
            return


def _print_event(event: dict[str, Any]) -> None:
    event_type = str(event.get("type", "?"))
    payload = event.get("payload") or {}

    if event_type == "assistant.text.completed":
        print(f"  [{event_type}] {payload.get('reply', '')}")
    elif event_type == "turn.completed":
        retries = payload.get("retries")
        total_ms = payload.get("total_ms")
        print(f"  [{event_type}] retries={retries} total={total_ms}ms")
    elif event_type == "turn.failed":
        print(f"  [{event_type}] {payload.get('code')}: {payload.get('reply', '')}")
    elif event_type in {"avatar.expression.set", "avatar.action.play"}:
        print(f"  [{event_type}] {json.dumps(payload, ensure_ascii=False)}")
    else:
        print(f"  [{event_type}]")


def _setup_logging(settings: Settings) -> None:
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=settings.log_file,
    )


if __name__ == "__main__":
    sys.exit(main())
