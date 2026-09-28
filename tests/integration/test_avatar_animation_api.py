"""``/v1/avatar/animation`` 的集成测试。

用的是 ``tmp_path`` 作数据目录，所以**不会碰仓库里的 data/**。
这里钉住三条契约：还没有副本时 404（不是空配置）、PUT 原样落盘、非法配置不落盘。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 与 Unity 工程里那份同构（含未知字段——它必须被原样保留）
_CONFIG = {
    "idlePool": [{"clip": "Idle_1", "weight": 1.0, "loop": True}],
    "actions": [
        {"action": "wave", "clips": [{"clip": "Punching", "weight": 1.0, "loop": False}]},
        {"action": "yawn", "clips": []},
    ],
    "comment": "手写的注释性字段，后端不该吃掉它",
}


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


def test_get_is_404_until_something_provides_a_copy(tmp_path: Path) -> None:
    """**不能返回空配置**：Unity 会把空的当成"配置就是这样"，把用户配的映射冲掉。"""
    with _client(tmp_path) as client:
        response = client.get("/v1/avatar/animation")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "animation_not_set"
    assert "Unity" in response.json()["detail"]["message"]


def test_put_then_get_round_trips_the_original_text(tmp_path: Path) -> None:
    text = json.dumps(_CONFIG, ensure_ascii=False, indent=2)

    with _client(tmp_path) as client:
        written = client.put(
            "/v1/avatar/animation",
            content=text,
            headers={"Content-Type": "application/json"},
        )
        fetched = client.get("/v1/avatar/animation")

    assert written.status_code == 200
    assert written.json()["ok"] is True
    assert written.json()["path"] == str(tmp_path / "astra_animation.json")
    assert fetched.status_code == 200
    # 原样返回：未知字段与排版都没被动过
    assert fetched.text == text
    assert "comment" in fetched.json()


def test_put_accepts_a_minimal_config(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put("/v1/avatar/animation", json={})

    assert response.status_code == 200
    assert (tmp_path / "astra_animation.json").read_text(encoding="utf-8") == "{}"


def test_put_rejects_an_unknown_action_without_touching_the_file(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.put("/v1/avatar/animation", json=_CONFIG)
        before = (tmp_path / "astra_animation.json").read_text(encoding="utf-8")

        response = client.put(
            "/v1/avatar/animation",
            json={"actions": [{"action": "dance", "clips": [{"clip": "X"}]}]},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_animation"
    assert "actions[0].action" in detail["message"]
    assert (tmp_path / "astra_animation.json").read_text(encoding="utf-8") == before


def test_put_rejects_broken_json(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(
            "/v1/avatar/animation",
            content="{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_json"
    assert not (tmp_path / "astra_animation.json").exists()


def test_put_stores_the_exact_bytes(tmp_path: Path) -> None:
    """**原样落盘**：文本模式写入会把 ``\\n`` 变成 ``\\r\\n``（Windows），那就不是"不重写"了。

    实测踩过：2514 字节的 Unity 配置落盘后变 2657 字节（143 行各多一个 CR）。
    """
    text = '{\n  "idlePool": []\n}\n'

    with _client(tmp_path) as client:
        client.put(
            "/v1/avatar/animation",
            content=text.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    assert (tmp_path / "astra_animation.json").read_bytes() == text.encode("utf-8")


def test_get_returns_the_exact_bytes(tmp_path: Path) -> None:
    raw = b'{\r\n  "idlePool": []\r\n}\r\n'
    (tmp_path / "astra_animation.json").write_bytes(raw)

    with _client(tmp_path) as client:
        response = client.get("/v1/avatar/animation")

    assert response.content == raw


def test_put_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.put("/v1/avatar/animation", json=_CONFIG)

    assert not (tmp_path / "astra_animation.json.tmp").exists()


def test_endpoints_are_in_the_openapi_schema(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/avatar/animation" in paths
    # 既有的 key 集合没被这次改动碰过（ADR 0006 / ADR 0016）
    assert "/health" in paths
    assert "/v1/config" in paths
