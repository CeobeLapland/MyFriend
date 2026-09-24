"""``/v1/chat/turns`` 的集成测试。

走真实的 FastAPI + lifespan + 临时 SQLite；LLM 用 ``FakeLLMProvider`` 注入，
这样测试确定、快速，且不依赖本机 LM Studio 是否在跑。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DB_FILES = ("astra.db", "astra.db-wal", "astra.db-shm")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "astra.db"


def _settings(db_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": db_path,
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(db_path), llm=FakeLLMProvider([llm_response()]))) as c:
        yield c


def _post_turn(client: TestClient, content: str, **extra: object) -> dict:
    body = {"content": content, **extra}
    response = client.post(
        "/v1/chat/turns",
        content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── 正常路径 ────────────────────────────────────────────


def test_turn_returns_structured_response(client: TestClient) -> None:
    body = _post_turn(client, "你好，今天陪我聊聊天吧。")

    assert body["status"] == "ok"
    assert body["reply"]
    assert body["response"]["emotion"]["primary"] in {
        "neutral",
        "happy",
        "sad",
        "angry",
        "surprised",
        "embarrassed",
        "sleepy",
        "excited",
    }
    assert -1.0 <= body["response"]["voice"]["pitch"] <= 1.0
    assert body["turn_id"]
    assert body["conversation_id"]
    assert body["failure"] is None
    assert body["stubs_used"] == ["apply_state_delta"]
    assert body["timings"]["total_ms"] >= 0


def test_history_endpoint_returns_both_messages(client: TestClient) -> None:
    created = _post_turn(client, "你好")

    history = client.get(f"/v1/conversations/{created['conversation_id']}/messages").json()

    assert history["conversation_id"] == created["conversation_id"]
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][0]["content"] == "你好"


def test_second_turn_reuses_conversation(client: TestClient) -> None:
    first = _post_turn(client, "第一句")
    second = _post_turn(client, "第二句")

    assert second["conversation_id"] == first["conversation_id"]


def test_explicit_conversation_id_is_honored(client: TestClient) -> None:
    created = _post_turn(client, "第一句")

    second = _post_turn(client, "第二句", conversation_id=created["conversation_id"])

    assert second["conversation_id"] == created["conversation_id"]


def test_unknown_conversation_id_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/turns",
        json={"content": "你好", "conversation_id": "01MISSING"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure"]["code"] == "conversation_not_found"


def test_empty_content_is_rejected_by_the_schema(client: TestClient) -> None:
    response = client.post("/v1/chat/turns", json={"content": ""})

    assert response.status_code == 422


def test_history_of_unknown_conversation_is_404(client: TestClient) -> None:
    response = client.get("/v1/conversations/01MISSING/messages")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "conversation_not_found"


# ── /v1/config ──────────────────────────────────────────


def test_config_exposes_vocabulary(client: TestClient) -> None:
    """词表必须从这里分发——Unity 端要按同一份词表做表情映射（ADR 0015）。"""
    body = client.get("/v1/config").json()

    assert body["companion_id"] == "astra"
    assert body["emotions"] == sorted(body["emotions"])
    assert "neutral" in body["emotions"]
    assert "idle" in body["actions"]
    assert "warm" in body["voice_styles"]
    assert body["stt_provider"] == "none"
    assert body["avatar_enabled"] is False


def test_config_never_exposes_secrets(client: TestClient) -> None:
    raw = client.get("/v1/config").text

    assert "lm-studio-local" not in raw
    assert "api_key" not in raw


# ── 降级 ────────────────────────────────────────────────


def test_llm_failure_returns_200_with_honest_reply(db_path: Path) -> None:
    """ADR 0014：不返 5xx，用 status=failed + 稳定错误码 + 诚实中文。"""
    from astra.domain.errors import LLMUnavailableError

    app = create_app(_settings(db_path), llm=FakeLLMProvider([LLMUnavailableError("refused")]))

    with TestClient(app) as client:
        body = _post_turn(client, "你好")

    assert body["status"] == "failed"
    assert body["response"] is None
    assert body["failure"]["code"] == "llm_unavailable"
    assert body["failure"]["recoverable"] is True
    assert body["reply"]
    assert "LM Studio" in body["reply"]


def test_failed_turn_still_persists_history(db_path: Path) -> None:
    from astra.domain.errors import LLMUnavailableError

    app = create_app(_settings(db_path), llm=FakeLLMProvider([LLMUnavailableError("refused")]))

    with TestClient(app) as client:
        body = _post_turn(client, "你好")
        history = client.get(f"/v1/conversations/{body['conversation_id']}/messages").json()

    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][1]["metadata"]["status"] == "failed"


def test_llm_disabled_returns_honest_reply(db_path: Path) -> None:
    """默认 provider=none，不注入任何 provider。"""
    with TestClient(create_app(_settings(db_path))) as client:
        body = _post_turn(client, "你好")

    assert body["status"] == "failed"
    assert body["failure"]["code"] == "llm_disabled"
    assert body["reply"]
