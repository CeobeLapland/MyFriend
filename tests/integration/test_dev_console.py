"""开发者控制台的集成测试。

它只在 ``ASTRA_ENV=dev`` 时挂载——这条边界必须有测试守着，否则"绑 0.0.0.0 时
把 env 设成 prod 就能关掉"这个承诺会悄悄失效。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: node 只是开发机上的便利工具，不是本项目的运行依赖——没有就跳过那条测试
_NODE = shutil.which("node")


def _extract_script(html: str) -> str:
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None, "dev.html 里找不到 <script> 块"
    return match.group(1)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": tmp_path / "astra.db",
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


@pytest.fixture
def dev_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))) as c:
        yield c


# ── 挂载条件 ────────────────────────────────────────────


def test_dev_console_is_served_in_dev_env(dev_client: TestClient) -> None:
    response = dev_client.get("/dev")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_dev_console_returns_html(dev_client: TestClient) -> None:
    body = dev_client.get("/dev").text

    assert body.lstrip().startswith("<!doctype html>")
    assert "开发者控制台" in body


def test_dev_console_uses_no_external_resources(dev_client: TestClient) -> None:
    """必须离线可用：不引任何 CDN / 外部脚本 / 外部样式。"""
    body = dev_client.get("/dev").text

    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "https://" not in body
    assert "<script src=" not in body
    assert "<link" not in body


def test_dev_console_never_assigns_html_directly(dev_client: TestClient) -> None:
    """模型输出是不可信内容，渲染必须走 textContent。"""
    body = dev_client.get("/dev").text

    assert ".innerHTML" not in body
    assert "innerHTML =" not in body
    assert "textContent" in body


def test_dev_console_is_absent_in_prod_env(tmp_path: Path) -> None:
    settings = _settings(tmp_path, env="prod")

    with TestClient(create_app(settings, llm=FakeLLMProvider([llm_response()]))) as client:
        response = client.get("/dev")

    assert response.status_code == 404


def test_dev_console_is_not_in_the_openapi_schema(dev_client: TestClient) -> None:
    """它不是对外 API 的一部分。"""
    paths = dev_client.get("/openapi.json").json()["paths"]

    assert "/dev" not in paths
    assert "/health" in paths
    assert "/v1/chat/turns" in paths


# ── 控制台依赖的端点都在 ────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/v1/config",
        "/v1/chat/turns",
        "/v1/chat/messages",
        "/v1/conversations/{conversation_id}/messages",
        # M2 记忆面板用到的端点
        "/v1/memories",
        "/v1/memories/settings",
        "/v1/memories/clear",
        "/v1/memories/{memory_id}",
    ],
)
def test_console_backing_endpoints_exist(dev_client: TestClient, path: str) -> None:
    """控制台用到的端点必须都在路由表里。"""
    paths = dev_client.get("/openapi.json").json()["paths"]

    assert path in paths


def test_dev_console_has_a_memory_panel(dev_client: TestClient) -> None:
    """M2 起控制台要能看记忆——否则"记忆有没有在工作"只能靠查库验收。"""
    body = dev_client.get("/dev").text

    assert "memory-list" in body
    assert "memory-enabled" in body
    assert "/v1/memories" in body


def test_websocket_route_is_mounted(dev_client: TestClient) -> None:
    """WS 路由不在 OpenAPI 里，用实际握手验证。"""
    with dev_client.websocket_connect("/ws/v1/session/01DEV") as socket:
        socket.send_json({"type": "input.user_text", "version": 1, "payload": {"text": "你好"}})
        event = socket.receive_json()

    assert event["type"] == "conversation.created"


@pytest.mark.skipif(_NODE is None, reason="需要 node 才能解析 JS")
def test_dev_console_script_parses(dev_client: TestClient, tmp_path: Path) -> None:
    """整段脚本必须能被解析。

    这不是洁癖：一个重复的 ``const`` 声明就是 ``SyntaxError``，会让**整个**页面
    静默失效——按钮点了没反应、WebSocket 根本不连，而服务端一切正常、
    控制台也不报错。M4 开发时真的踩过一次（``const el`` 声明了两遍）。

    没有 node 就跳过：它只是开发机上的便利工具，不是本项目的运行依赖。
    """
    script = tmp_path / "dev.js"
    script.write_text(_extract_script(dev_client.get("/dev").text), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - 参数固定，无 shell，不涉及用户输入
        [_NODE, "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
