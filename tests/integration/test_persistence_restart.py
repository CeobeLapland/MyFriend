"""重启持久化与 LLM 不可用的集成测试。

对应 `docs/10_TESTING_AND_ACCEPTANCE.md` §4 的两条集成验收：

- restart persistence：发消息 → 关掉 backend → 重启 → 历史还在
- provider failure：LM Studio 不可用 → health 标记 degraded、API 不崩、
  用户拿到清晰可恢复的错误
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(db_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": db_path,
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


def _free_port() -> int:
    """拿一个刚被释放的空闲端口——连上去必然失败。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_turn(client: TestClient, content: str) -> dict:
    response = client.post(
        "/v1/chat/turns",
        content=json.dumps({"content": content}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── 重启持久化 ──────────────────────────────────────────


def test_history_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    with TestClient(create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))) as first:
        created = _post_turn(first, "请记住我正在做一个本地 AI 伙伴项目。")

    # app#1 已随 with 块退出（lifespan shutdown），这里相当于进程重启
    with TestClient(create_app(_settings(db_path))) as second:
        history = second.get(f"/v1/conversations/{created['conversation_id']}/messages").json()

    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][0]["content"] == "请记住我正在做一个本地 AI 伙伴项目。"
    assert history["messages"][0]["id"] < history["messages"][1]["id"]


def test_restart_does_not_duplicate_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    with TestClient(create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))) as first:
        created = _post_turn(first, "你好")

    with TestClient(create_app(_settings(db_path))) as second:
        second.get("/health")  # 触发 lifespan（会重跑 migrate，应幂等）
        history = second.get(f"/v1/conversations/{created['conversation_id']}/messages").json()

    assert len(history["messages"]) == 2


def test_restart_continues_the_same_conversation(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    with TestClient(create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))) as first:
        created = _post_turn(first, "第一句")

    with TestClient(
        create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))
    ) as second:
        continued = _post_turn(second, "第二句")

    assert continued["conversation_id"] == created["conversation_id"]


# ── provider 不可用 ─────────────────────────────────────


def test_health_reports_llm_down_when_unreachable(tmp_path: Path) -> None:
    """docs/10 §4：LM Studio unavailable → health marks degraded。"""
    settings = _settings(
        tmp_path / "astra.db",
        llm_provider="lmstudio",
        llm_base_url=f"http://127.0.0.1:{_free_port()}/v1",
        health_probe_timeout_s=0.5,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["components"]["llm"]["status"] == "down"
    assert body["status"] == "degraded"
    assert body["components"]["database"]["status"] == "ok"


def test_api_does_not_crash_when_llm_unreachable(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "astra.db",
        llm_provider="lmstudio",
        llm_base_url=f"http://127.0.0.1:{_free_port()}/v1",
        llm_connect_timeout_s=0.5,
        llm_request_timeout_s=1.0,
        health_probe_timeout_s=0.5,
    )

    with TestClient(create_app(settings)) as client:
        body = _post_turn(client, "你好")
        health = client.get("/health").json()

    assert body["status"] == "failed"
    assert body["failure"]["code"] == "llm_unavailable"
    assert body["reply"]
    assert health["status"] == "degraded"


def test_user_message_survives_provider_failure(tmp_path: Path) -> None:
    """重连的客户端不该看到一条悬空的用户消息。"""
    settings = _settings(
        tmp_path / "astra.db",
        llm_provider="lmstudio",
        llm_base_url=f"http://127.0.0.1:{_free_port()}/v1",
        llm_connect_timeout_s=0.5,
        llm_request_timeout_s=1.0,
    )

    with TestClient(create_app(settings)) as client:
        body = _post_turn(client, "这句话必须留下")
        history = client.get(f"/v1/conversations/{body['conversation_id']}/messages").json()

    assert history["messages"][0]["content"] == "这句话必须留下"
    assert history["messages"][1]["content"]


# ── persona 快速失败 ────────────────────────────────────


def test_missing_persona_fails_fast(tmp_path: Path) -> None:
    """人格是应用的身份，缺文件应当快速失败而不是降级启动。"""
    from astra.infrastructure.persona.toml_loader import PersonaLoadError

    settings = _settings(tmp_path / "astra.db", persona_path=tmp_path / "nope.toml")

    with pytest.raises(PersonaLoadError):
        create_app(settings)
