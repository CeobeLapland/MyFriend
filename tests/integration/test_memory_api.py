"""记忆子系统的集成测试（`docs/08_IMPLEMENTATION_PLAN.md` 阶段 2 的验收）。

**核心是那 5 步验收**：

1. 第一次告诉 AI 一个稳定偏好
2. 关闭程序
3. 重新启动
4. 新会话中能在相关语境检索出来
5. 删除后不再检索到

用 ``TestClient`` 而不是真实 uvicorn：这几条验的是**数据与协议**（落库、重启后还在、
删除后检索不到），不是 WS 握手——WS 的生产路径另有 ``test_chat_ws.py`` 覆盖。

向量由 ``FakeEmbeddingProvider`` 显式给定，这样"相关语境"是可复现的前提，
而不是碰运气。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider, assistant_json, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DIM = 768

#: 用户原话与它对应的偏好记忆
_PREFERENCE_TURN = "我特别喜欢深夜写代码，请记住这个。"
_PREFERENCE = "用户喜欢深夜写代码"
#: 之后换一个说法问——语义相近，应该能检索到
_RECALL_TURN = "我一般什么时候写代码效率最高？"

_VECTORS = {
    _PREFERENCE: (1.0, 0.0, *([0.0] * (_DIM - 2))),
    _RECALL_TURN: (1.0, 0.05, *([0.0] * (_DIM - 2))),
}


def _settings(db_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": db_path,
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


def _embedder() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dim=_DIM, vectors=_VECTORS)


def _remembering_llm() -> FakeLLMProvider:
    """第一轮的 LLM：回复 + 一条候选。"""
    return FakeLLMProvider(
        [
            llm_response(
                assistant_json(memory_candidates=[{"type": "preference", "content": _PREFERENCE}])
            )
        ]
    )


def _plain_llm() -> FakeLLMProvider:
    return FakeLLMProvider([llm_response(), llm_response()])


def _post_turn(client: TestClient, content: str) -> dict:
    response = client.post(
        "/v1/chat/turns",
        content=json.dumps({"content": content}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── 验收 5 步 ───────────────────────────────────────────


def test_memory_survives_restart_and_is_retrieved(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    # 1) 告诉 AI 一个稳定偏好
    with TestClient(
        create_app(_settings(db_path), llm=_remembering_llm(), embeddings=_embedder())
    ) as first:
        result = _post_turn(first, _PREFERENCE_TURN)
        assert result["memories_created"] == 1

        stored = first.get("/v1/memories").json()
        assert stored["total"] == 1
        assert stored["memories"][0]["content"] == _PREFERENCE
        assert stored["memories"][0]["source_message_id"]

    # 2) 关闭程序（with 块退出 = lifespan shutdown）
    # 3) 重新启动，并在新会话里问一个相关的问题
    recall_llm = _plain_llm()
    with TestClient(
        create_app(_settings(db_path), llm=recall_llm, embeddings=_embedder())
    ) as second:
        result = _post_turn(second, _RECALL_TURN)

        # 4) 相关语境能检索出来
        assert result["memories_retrieved"] == 1
        prompt = recall_llm.requests[0].system_prompt
        assert "RELEVANT MEMORIES" in prompt
        assert _PREFERENCE in prompt


def test_deleted_memory_is_no_longer_retrieved(tmp_path: Path) -> None:
    """验收第 5 步——**这条是"真的删了"的端到端证据**。"""
    db_path = tmp_path / "astra.db"

    with TestClient(
        create_app(_settings(db_path), llm=_remembering_llm(), embeddings=_embedder())
    ) as first:
        _post_turn(first, _PREFERENCE_TURN)
        memory_id = first.get("/v1/memories").json()["memories"][0]["id"]

        assert first.delete(f"/v1/memories/{memory_id}").status_code == 204
        assert first.get("/v1/memories").json()["total"] == 0

    recall_llm = _plain_llm()
    with TestClient(
        create_app(_settings(db_path), llm=recall_llm, embeddings=_embedder())
    ) as second:
        result = _post_turn(second, _RECALL_TURN)

        assert result["memories_retrieved"] == 0
        assert "RELEVANT MEMORIES" not in recall_llm.requests[0].system_prompt


def test_clear_removes_everything(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    with TestClient(
        create_app(_settings(db_path), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)

        cleared = client.post("/v1/memories/clear").json()

        assert cleared["removed"] == 1
        assert client.get("/v1/memories").json()["total"] == 0


def test_deleting_the_conversation_keeps_the_memories(tmp_path: Path) -> None:
    """ADR 0029：会话删除只删消息。记忆有自己的生命周期与删除入口。"""
    db_path = tmp_path / "astra.db"

    with TestClient(
        create_app(_settings(db_path), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)
        conversation_id = client.get("/v1/conversations").json()["conversations"][0][
            "conversation_id"
        ]

        assert client.delete(f"/v1/conversations/{conversation_id}").status_code == 204

        stored = client.get("/v1/memories").json()
        assert stored["total"] == 1
        assert stored["memories"][0]["content"] == _PREFERENCE


# ── 用户控制面（docs/06 §9）─────────────────────────────


def test_list_filters_by_type_and_importance(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)

        assert client.get("/v1/memories", params={"type": "preference"}).json()["total"] == 1
        assert client.get("/v1/memories", params={"type": "episodic"}).json()["total"] == 0
        assert client.get("/v1/memories", params={"min_importance": 0.99}).json()["total"] == 0


def test_unknown_type_filter_is_rejected(tmp_path: Path) -> None:
    """不认识的类型名直接 422，而不是静默返回空列表。"""
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        response = client.get("/v1/memories", params={"type": "goal"})

    assert response.status_code == 422


def test_semantic_query_returns_ranked_results(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)

        found = client.get("/v1/memories", params={"query": _RECALL_TURN}).json()

    assert found["total"] == 1
    assert found["memories"][0]["content"] == _PREFERENCE


def test_read_single_memory(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)
        memory_id = client.get("/v1/memories").json()["memories"][0]["id"]

        body = client.get(f"/v1/memories/{memory_id}").json()

    assert body["id"] == memory_id
    assert body["content"] == _PREFERENCE
    assert body["type"] == "preference"


def test_patch_updates_fields(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)
        memory_id = client.get("/v1/memories").json()["memories"][0]["id"]

        patched = client.patch(
            f"/v1/memories/{memory_id}",
            json={"content": "用户习惯在深夜写代码", "importance": 0.33, "summary": "作息偏好"},
        ).json()

    assert patched["content"] == "用户习惯在深夜写代码"
    assert patched["importance"] == 0.33
    assert patched["summary"] == "作息偏好"


def test_export_includes_everything(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        _post_turn(client, _PREFERENCE_TURN)

        exported = client.get("/v1/memories/export").json()

    assert exported["total"] == 1
    assert exported["memories"][0]["content"] == _PREFERENCE
    assert exported["exported_at"]


def test_settings_toggle_round_trips(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        assert client.get("/v1/memories/settings").json() == {"enabled": True}

        assert client.put("/v1/memories/settings", json={"enabled": False}).json() == {
            "enabled": False
        }
        assert client.get("/v1/config").json()["memory_enabled"] is False


def test_disabling_memory_stops_both_reads_and_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "astra.db"

    with TestClient(
        create_app(_settings(db_path), llm=_plain_llm(), embeddings=_embedder())
    ) as client:
        client.put("/v1/memories/settings", json={"enabled": False})
        result = _post_turn(client, _PREFERENCE_TURN)

        assert result["memories_created"] == 0
        assert client.get("/v1/memories").json()["total"] == 0


# ── 404 / 边界 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/v1/memories/nope"), ("delete", "/v1/memories/nope")],
)
def test_unknown_memory_returns_404(tmp_path: Path, method: str, path: str) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        response = getattr(client, method)(path)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "memory_not_found"


def test_patch_unknown_memory_returns_404(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        response = client.patch("/v1/memories/nope", json={"importance": 0.5})

    assert response.status_code == 404


def test_static_routes_are_not_shadowed_by_the_id_route(tmp_path: Path) -> None:
    """``/settings`` / ``/export`` / ``/clear`` 必须排在 ``/{memory_id}`` 之前。"""
    with TestClient(
        create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm(), embeddings=_embedder())
    ) as client:
        assert client.get("/v1/memories/settings").status_code == 200
        assert client.get("/v1/memories/export").status_code == 200
        assert client.post("/v1/memories/clear").status_code == 200


# ── 无 embedding 能力时的降级 ───────────────────────────


def test_memory_is_inert_without_an_embedding_provider(tmp_path: Path) -> None:
    """``ASTRA_EMBEDDING_PROVIDER=none``：记忆整体缺席，但聊天照常。"""
    with TestClient(create_app(_settings(tmp_path / "astra.db"), llm=_remembering_llm())) as client:
        result = _post_turn(client, _PREFERENCE_TURN)

        assert result["status"] == "ok"
        assert result["memories_created"] == 0
        assert client.get("/v1/memories").json()["total"] == 0


def test_dimension_mismatch_fails_fast(tmp_path: Path) -> None:
    """维度与迁移里写死的 float[768] 不符时必须启动即失败，而不是运行时才炸。"""
    settings = _settings(tmp_path / "astra.db", embedding_dim=384)

    with pytest.raises(ValueError, match="does not match the vector index dimension"):
        create_app(settings, embeddings=FakeEmbeddingProvider(dim=384))
