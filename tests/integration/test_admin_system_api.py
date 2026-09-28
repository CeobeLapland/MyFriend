"""``/v1/admin/restart`` 的集成测试。

真实的重启（退出码 75 → 父进程拉起新子进程）只能真机验；这里钉住的是**决策**：
没监督者就不退、配置非法就不退、监督者在线就置位两个标志。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeServer:
    """只记 ``should_exit``：真实的 uvicorn.Server 由 cli 挂上去。"""

    def __init__(self) -> None:
        self.should_exit = False


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        db_path=tmp_path / "astra.db",
        persona_path=_REPO_ROOT / "personas" / "astra.toml",
    )


@contextmanager
def _client(tmp_path: Path, *, supervised: bool) -> Iterator[tuple[TestClient, _FakeServer]]:
    app = create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))
    app.state.env_file_path = tmp_path / ".env"
    server = _FakeServer()
    if supervised:
        app.state.supervised = True
        app.state.uvicorn_server = server
        app.state.restart_requested = False

    with TestClient(app) as client:
        yield client, server


def test_restart_is_refused_without_a_supervisor(tmp_path: Path) -> None:
    """没人会把它拉起来的时候，退出就等于把后端搞死——如实 409，不假装重启。"""
    with _client(tmp_path, supervised=False) as (client, _server):
        response = client.post("/v1/admin/restart")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_supervised"


def test_restart_asks_uvicorn_to_exit_and_flags_the_request(tmp_path: Path) -> None:
    with _client(tmp_path, supervised=True) as (client, server):
        response = client.post("/v1/admin/restart")

        assert response.status_code == 202
        assert response.json() == {"status": "restarting"}
        assert server.should_exit is True
        assert client.app.state.restart_requested is True  # type: ignore[attr-defined]


def test_restart_refuses_a_broken_env_file(tmp_path: Path) -> None:
    """写坏的 .env 会让重启直接失败——在退出之前就挡住，把原因说清楚。"""
    (tmp_path / ".env").write_text("ASTRA_LLM_TEMPERATURE=abc\n", encoding="utf-8")

    with _client(tmp_path, supervised=True) as (client, server):
        response = client.post("/v1/admin/restart")

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "env_invalid"
        assert "llm_temperature" in response.json()["detail"]["message"]
        assert server.should_exit is False
        assert client.app.state.restart_requested is False  # type: ignore[attr-defined]


def test_restart_accepts_a_valid_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ASTRA_LLM_PROVIDER=none\n", encoding="utf-8")

    with _client(tmp_path, supervised=True) as (client, _server):
        assert client.post("/v1/admin/restart").status_code == 202


def test_restart_needs_no_env_file(tmp_path: Path) -> None:
    """没有 .env 也能重启（用默认值跑是合法状态，仓库本来就没有 .env）。"""
    with _client(tmp_path, supervised=True) as (client, _server):
        assert client.post("/v1/admin/restart").status_code == 202


def test_restart_is_mounted_in_prod_too(tmp_path: Path) -> None:
    app = create_app(
        Settings(  # type: ignore[arg-type]
            _env_file=None,
            env="prod",
            db_path=tmp_path / "astra.db",
            persona_path=_REPO_ROOT / "personas" / "astra.toml",
        ),
        llm=FakeLLMProvider([llm_response()]),
    )

    with TestClient(app) as client:
        # 无条件挂载（ADR 0026 §2）：prod 下也是 409（没人监督），而不是 404
        assert client.post("/v1/admin/restart").status_code == 409


def test_restart_is_in_the_openapi_schema(tmp_path: Path) -> None:
    """它是真实接口（控制面），不像 /studio 那样藏在 schema 之外。"""
    with _client(tmp_path, supervised=True) as (client, _server):
        paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/admin/restart" in paths
    assert "/v1/admin/config" in paths
