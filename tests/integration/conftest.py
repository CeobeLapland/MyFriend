"""集成测试的共享 fixture：**真实 uvicorn** 跑在后台线程里。

为什么不用 ``TestClient.websocket_connect()``：

1. 它**没有超时参数**——CI 里一个卡住的 ``receive_json()`` 是挂死而不是失败；
2. 它自己实现 WS 协议、**不经过 uvicorn**，因此会**掩盖 `websockets` 依赖缺失**
   ——而实测确认本项目最初 ``websockets`` 与 ``wsproto`` 都没装。

用真实 uvicorn 是唯一能验证"生产路径真的能握手"的方式。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from astra.api.app import create_app
from astra.domain.ports import LLMProvider
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_STARTUP_TIMEOUT_S = 15.0
_SHUTDOWN_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class LiveServer:
    host: str
    port: int

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _wait_until_started(server: uvicorn.Server) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if server.started:
            return
        time.sleep(0.02)
    msg = "uvicorn did not start within the timeout"
    raise RuntimeError(msg)


def _bound_port(server: uvicorn.Server) -> int:
    for asyncio_server in server.servers:
        for sock in asyncio_server.sockets:
            return int(sock.getsockname()[1])
    msg = "uvicorn server has no bound socket"
    raise RuntimeError(msg)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    params: dict[str, Any] = {
        "db_path": tmp_path / "astra.db",
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)


def default_llm(turns: int = 20) -> FakeLLMProvider:
    return FakeLLMProvider([llm_response() for _ in range(turns)])


@pytest.fixture
def run_server(tmp_path: Path) -> Iterator[Callable[..., LiveServer]]:
    """启动一个真实 uvicorn 实例，返回它的地址。测试结束时统一关停。"""
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []

    def _start(*, llm: LLMProvider | None = None, **overrides: Any) -> LiveServer:
        settings = make_settings(tmp_path, **overrides)
        app = create_app(settings, llm=llm if llm is not None else default_llm())

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,  # 让操作系统分配，避免测试之间抢端口
            log_config=None,
            ws_ping_interval=None,  # 测试里不需要心跳
            lifespan="on",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_until_started(server)

        servers.append(server)
        threads.append(thread)
        return LiveServer(host="127.0.0.1", port=_bound_port(server))

    yield _start

    for server in servers:
        server.should_exit = True
    for thread in threads:
        thread.join(timeout=_SHUTDOWN_TIMEOUT_S)
