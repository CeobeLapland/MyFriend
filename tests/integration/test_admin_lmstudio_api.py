"""``/v1/admin/lmstudio`` 的集成测试（用**假 lms**）。

真机上的 ``lms load`` 要 ~5s 且会真的改机器状态，所以这里把 ``run_lms`` 换掉：
被钉住的是**端点行为**——状态怎么拼、失败怎么报、声明值写到哪。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.api.routes import admin_lmstudio
from astra.infrastructure import lmstudio_cli, studio_state
from astra.infrastructure.lmstudio_cli import LmsResult
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LS_JSON = json.dumps(
    [
        {
            "type": "llm",
            "modelKey": "nvidia/nemotron-3-nano-4b",
            "displayName": "Nemotron 3 Nano 4B",
            "paramsString": "4.0B",
            "sizeBytes": 2837173509,
            "maxContextLength": 1048576,
        }
    ]
)

_PS_JSON = json.dumps(
    [
        {
            "identifier": "nvidia/nemotron-3-nano-4b",
            "modelKey": "nvidia/nemotron-3-nano-4b",
            "contextLength": 16384,
            "sizeBytes": 2837173509,
        }
    ]
)


class _FakeLms:
    """按子命令分派固定输出，并记下所有被调用过的命令。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.results: dict[str, LmsResult] = {}

    def add(self, subcommand: str, result: LmsResult) -> None:
        self.results[subcommand] = result

    async def __call__(self, args: list[str], *, timeout_s: float = 30.0) -> LmsResult:
        self.calls.append(list(args))
        return self.results.get(args[0], LmsResult(ok=True, code=0, stdout="", stderr=""))


@pytest.fixture
def lms(monkeypatch: pytest.MonkeyPatch) -> _FakeLms:
    fake = _FakeLms()
    monkeypatch.setattr(admin_lmstudio, "run_lms", fake)
    monkeypatch.setattr(lmstudio_cli, "cli_path", lambda: r"C:\fake\lms.exe")
    return fake


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        db_path=tmp_path / "astra.db",
        data_dir=tmp_path,
        persona_path=_REPO_ROOT / "personas" / "astra.toml",
    )


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))) as c:
        yield c


# ── 读 ──────────────────────────────────────────────────


def test_status_without_the_cli_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lmstudio_cli, "cli_path", lambda: None)

    with _client(tmp_path) as client:
        body = client.get("/v1/admin/lmstudio").json()

    assert body["cli"]["available"] is False
    assert "lms" in body["cli"]["message"]
    assert body["models"] == []
    assert body["server"] == "unknown"


def test_status_reports_server_models_and_loaded(tmp_path: Path, lms: _FakeLms) -> None:
    lms.add(
        "server",
        LmsResult(ok=True, code=0, stdout="The server is running on port 1234.", stderr=""),
    )
    lms.add("ls", LmsResult(ok=True, code=0, stdout=_LS_JSON, stderr=""))
    lms.add("ps", LmsResult(ok=True, code=0, stdout=_PS_JSON, stderr=""))

    with _client(tmp_path) as client:
        body = client.get("/v1/admin/lmstudio").json()

    assert body["cli"] == {"available": True, "path": r"C:\fake\lms.exe", "message": None}
    assert body["server"] == "running"
    assert body["models"][0]["display_name"] == "Nemotron 3 Nano 4B"
    assert body["models"][0]["max_context"] == 1048576
    assert body["loaded"][0]["identifier"] == "nvidia/nemotron-3-nano-4b"
    assert body["loaded"][0]["context_length"] == 16384
    assert body["declared"] is None


# ── 加载：声明值 ────────────────────────────────────────


def test_load_runs_lms_and_records_the_declared_gpu_policy(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/admin/lmstudio/load",
            json={"model_key": "nvidia/nemotron-3-nano-4b", "gpu": "max", "context_length": 16384},
        )
        body = response.json()

    assert response.status_code == 200
    assert lms.calls[0] == [
        "load",
        "nvidia/nemotron-3-nano-4b",
        "--gpu",
        "max",
        "-y",
        "--context-length",
        "16384",
    ]
    assert body["declared"]["gpu"] == "max"

    # 声明值落在 data_dir 下（不是配置真相，UI 会标注"非实测"）
    stored = studio_state.read_declared(tmp_path / "studio-state.json")
    assert stored is not None
    assert stored.model_key == "nvidia/nemotron-3-nano-4b"
    assert stored.context_length == 16384


def test_load_accepts_a_fractional_gpu_split(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        client.post("/v1/admin/lmstudio/load", json={"model_key": "a/b", "gpu": 0.5})

    assert lms.calls[0] == ["load", "a/b", "--gpu", "0.5", "-y"]


def test_load_rejects_an_out_of_range_gpu_split(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        response = client.post("/v1/admin/lmstudio/load", json={"model_key": "a/b", "gpu": 7})

    assert response.status_code == 422


def test_failed_load_returns_502_with_the_raw_output(tmp_path: Path, lms: _FakeLms) -> None:
    lms.add(
        "load",
        LmsResult(ok=False, code=1, stdout="", stderr="model not found", message="lms 退出码 1"),
    )

    with _client(tmp_path) as client:
        response = client.post("/v1/admin/lmstudio/load", json={"model_key": "nope"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "lms_failed"
    assert "model not found" in detail["stderr"]
    # 失败不该留下声明值
    assert studio_state.read_declared(tmp_path / "studio-state.json") is None


def test_missing_cli_returns_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lmstudio_cli, "cli_path", lambda: None)

    with _client(tmp_path) as client:
        response = client.post("/v1/admin/lmstudio/server", json={"action": "start"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "lms_unavailable"


# ── 卸载与服务 ──────────────────────────────────────────


def test_unload_requires_a_target(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        response = client.post("/v1/admin/lmstudio/unload", json={})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_input"


def test_unload_one_and_all(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        client.post("/v1/admin/lmstudio/unload", json={"identifier": "nvidia/nemotron-3-nano-4b"})
        client.post("/v1/admin/lmstudio/unload", json={"unload_all": True})

    assert lms.calls[0] == ["unload", "nvidia/nemotron-3-nano-4b"]
    assert lms.calls[1] == ["unload", "--all"]


def test_server_start_and_stop(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        assert client.post("/v1/admin/lmstudio/server", json={"action": "start"}).status_code == 200
        assert client.post("/v1/admin/lmstudio/server", json={"action": "stop"}).status_code == 200

    assert lms.calls == [["server", "start"], ["server", "stop"]]


def test_server_rejects_unknown_actions(tmp_path: Path, lms: _FakeLms) -> None:
    with _client(tmp_path) as client:
        response = client.post("/v1/admin/lmstudio/server", json={"action": "restart"})

    assert response.status_code == 422


def test_endpoints_are_in_the_openapi_schema(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/admin/lmstudio" in paths
    assert "/v1/admin/lmstudio/load" in paths
    assert "/v1/admin/lmstudio/unload" in paths
    assert "/v1/admin/lmstudio/server" in paths
