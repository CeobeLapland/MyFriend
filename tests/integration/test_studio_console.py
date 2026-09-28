"""Studio 产品界面的集成测试。

Studio 与 ``/dev`` 的挂载规则**相反**（ADR 0025 §5）：它无条件挂载，把 ``ASTRA_ENV``
设成 ``prod`` 也照常可用。这条差异必须有测试守着——否则将来会有人"顺手"给 Studio
加回 env 门控，或者在放开 ``/dev`` 时把这条边界一起抹掉。

阶段 1 的 Studio **零新增业务端点**：页面只消费既有接口，所以这里除了"页面本身"
还验证它依赖的端点都在路由表里。
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
    assert match is not None, "studio.html 里找不到 <script> 块"
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
def studio_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))) as c:
        yield c


# ── 页面本身 ────────────────────────────────────────────


def test_studio_is_served(studio_client: TestClient) -> None:
    response = studio_client.get("/studio")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_studio_returns_html(studio_client: TestClient) -> None:
    body = studio_client.get("/studio").text

    assert body.lstrip().startswith("<!doctype html>")
    assert "Studio" in body


def test_studio_is_served_in_prod_env_while_dev_is_not(tmp_path: Path) -> None:
    """挂载规则与 ``/dev`` 相反（ADR 0025 §5）——显式钉住这条差异。"""
    settings = _settings(tmp_path, env="prod")

    with TestClient(create_app(settings, llm=FakeLLMProvider([llm_response()]))) as client:
        assert client.get("/studio").status_code == 200
        assert client.get("/dev").status_code == 404


def test_studio_uses_no_external_resources(studio_client: TestClient) -> None:
    """必须离线可用：不引任何 CDN / 外部脚本 / 外部样式。"""
    body = studio_client.get("/studio").text

    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "https://" not in body
    assert "<script src=" not in body
    assert "<link" not in body


def test_studio_never_assigns_html_directly(studio_client: TestClient) -> None:
    """模型输出是不可信内容，渲染必须走 textContent。"""
    body = studio_client.get("/studio").text

    assert ".innerHTML" not in body
    assert "innerHTML =" not in body
    assert "textContent" in body


def test_studio_is_not_in_the_openapi_schema(studio_client: TestClient) -> None:
    """它不是对外 API 的一部分。"""
    paths = studio_client.get("/openapi.json").json()["paths"]

    assert "/studio" not in paths
    assert "/health" in paths
    assert "/v1/chat/turns" in paths


def test_studio_has_all_phase_pages(studio_client: TestClient) -> None:
    """页签：对话 / 记忆 / 人格（ADR 0029）+ 设置 / 模型 / 事件 / 动画。"""
    body = studio_client.get("/studio").text

    assert 'data-page="chat"' in body
    assert 'data-page="memory"' in body
    assert 'data-page="persona"' in body
    assert 'data-page="config"' in body
    assert 'data-page="models"' in body
    assert 'data-page="events"' in body
    assert 'data-page="anime"' in body


# ── Studio 依赖的端点都在 ───────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/v1/config",
        "/v1/chat/turns",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/messages",
        # 记忆页用到的全部端点（含导出与开关）
        "/v1/memories",
        "/v1/memories/settings",
        "/v1/memories/clear",
        "/v1/memories/export",
        "/v1/memories/{memory_id}",
        # 右栏的角色状态
        "/v1/state",
        # 阶段 2 的设置页（读/写 .env + 一键重启）
        "/v1/admin/config",
        "/v1/admin/restart",
        # 阶段 2 的模型页（LM Studio）
        "/v1/admin/lmstudio",
        "/v1/admin/lmstudio/server",
        "/v1/admin/lmstudio/load",
        "/v1/admin/lmstudio/unload",
        # 阶段 3 的动画页（后端托管映射）
        "/v1/avatar/animation",
        # 人格页（ADR 0029：列表 / 文档 / 新建保存删除 / 切换）
        "/v1/admin/personas",
        "/v1/admin/personas/{persona_id}",
        "/v1/admin/personas/{persona_id}/activate",
    ],
)
def test_studio_backing_endpoints_exist(studio_client: TestClient, path: str) -> None:
    paths = studio_client.get("/openapi.json").json()["paths"]

    assert path in paths


@pytest.mark.skipif(_NODE is None, reason="需要 node 才能解析 JS")
def test_studio_script_parses(studio_client: TestClient, tmp_path: Path) -> None:
    """整段脚本必须能被解析。

    这不是洁癖：一个重复的 ``const`` 声明就是 ``SyntaxError``，会让**整个**页面
    静默失效——按钮点了没反应、WebSocket 根本不连，而服务端一切正常、
    控制台也不报错。M4 开发时真的踩过一次（``const el`` 声明了两遍）。
    """
    script = tmp_path / "studio.js"
    script.write_text(_extract_script(studio_client.get("/studio").text), encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - 参数固定，无 shell，不涉及用户输入
        [_NODE, "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
