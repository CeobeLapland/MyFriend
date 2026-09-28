"""``/v1/admin/personas`` 的集成测试（ADR 0029）。

**会写文件**：人格目录与 ``.env`` 都指到 ``tmp_path``，绝不碰仓库里的 ``personas/``
与真实的 ``.env``。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED = _REPO_ROOT / "personas" / "astra.toml"

_EXAMPLE = "# 测试用示例\n#\n# 注释要保留\nASTRA_LLM_PROVIDER=none\n"


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        db_path=tmp_path / "astra.db",
        persona_path=tmp_path / "personas" / "astra.toml",
    )


@pytest.fixture
def personas_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "personas"
    directory.mkdir()
    shutil.copy2(_SHIPPED, directory / "astra.toml")
    return directory


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))
    app.state.env_file_path = tmp_path / ".env"
    with TestClient(app) as client:
        yield client


@pytest.fixture
def admin(tmp_path: Path, personas_dir: Path) -> Iterator[TestClient]:
    (tmp_path / ".env.example").write_text(_EXAMPLE, encoding="utf-8")
    with _client(tmp_path) as client:
        yield client


def _persona(display_name: str = "Luna", **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "identity": {
            "name": "Luna",
            "display_name": display_name,
            "pronouns": "她",
            "self_description": "一个安静的测试角色。",
        },
        "communication": {
            "sentence_length": "short",
            "emoji_frequency": "low",
            "rhetorical_questions": "rare",
            "preferred_interaction": "conversational",
        },
    }
    document.update(overrides)
    return document


def _create(client: TestClient, persona_id: str = "luna", **overrides: Any) -> None:
    response = client.post(
        "/v1/admin/personas", json={"id": persona_id, "persona": _persona(**overrides)}
    )
    assert response.status_code == 201, response.text


def _by_id(client: TestClient, persona_id: str) -> dict[str, Any]:
    listing = client.get("/v1/admin/personas").json()
    return next(item for item in listing["personas"] if item["id"] == persona_id)


# ── 列表与读取 ──────────────────────────────────────────


def test_list_reports_the_running_persona(admin: TestClient, tmp_path: Path) -> None:
    body = admin.get("/v1/admin/personas").json()

    assert body["directory"] == str(tmp_path / "personas")
    assert body["running_id"] == "astra"
    assert body["pending_id"] == "astra"  # .env 还没写，重启后仍是同一位
    assert body["supervised"] is False

    assert [item["id"] for item in body["personas"]] == ["astra"]
    entry = body["personas"][0]
    assert entry["display_name"] == "Astra"
    assert entry["is_running"] is True
    assert entry["is_pending"] is False
    assert entry["error"] is None


def test_read_returns_the_document(admin: TestClient) -> None:
    body = admin.get("/v1/admin/personas/astra").json()

    assert body["id"] == "astra"
    assert body["path"].endswith("astra.toml")
    assert body["document"]["identity"]["name"] == "Astra"
    assert body["document"]["traits"]["curious"] == pytest.approx(0.88)
    assert body["document"]["behavior"]["rules"]
    assert body["document"]["prompt"]["hard_constraints"]


def test_read_unknown_is_404(admin: TestClient) -> None:
    response = admin.get("/v1/admin/personas/nope")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "persona_not_found"


def test_read_with_an_invalid_id_is_400(admin: TestClient) -> None:
    response = admin.get("/v1/admin/personas/BadName")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_id"


def test_list_reports_a_broken_file_without_failing(admin: TestClient, personas_dir: Path) -> None:
    (personas_dir / "broken.toml").write_text("this is not = = toml", encoding="utf-8")

    listing = admin.get("/v1/admin/personas").json()

    assert [item["id"] for item in listing["personas"]] == ["astra", "broken"]
    broken = listing["personas"][1]
    assert broken["display_name"] is None
    assert broken["error"] is not None


# ── 新建 ────────────────────────────────────────────────


def test_create_writes_a_new_persona(admin: TestClient, personas_dir: Path) -> None:
    response = admin.post("/v1/admin/personas", json={"id": "luna", "persona": _persona()})

    assert response.status_code == 201, response.text
    assert response.json()["document"]["identity"]["display_name"] == "Luna"
    assert (personas_dir / "luna.toml").is_file()

    # 新建**不自动激活**
    assert _by_id(admin, "luna")["is_running"] is False
    assert admin.get("/v1/admin/personas").json()["pending_id"] == "astra"


def test_create_duplicate_is_409(admin: TestClient) -> None:
    _create(admin)

    response = admin.post("/v1/admin/personas", json={"id": "luna", "persona": _persona()})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "persona_exists"


def test_create_with_an_invalid_id_is_400(admin: TestClient) -> None:
    response = admin.post("/v1/admin/personas", json={"id": "Bad Name", "persona": _persona()})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_id"


def test_create_with_an_invalid_document_is_400_with_the_field_path(admin: TestClient) -> None:
    identity = {"name": "Luna", "display_name": "Luna"}  # 缺 pronouns
    response = admin.post(
        "/v1/admin/personas", json={"id": "luna", "persona": _persona(identity=identity)}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_persona"
    assert "identity.pronouns" in detail["message"]


# ── 保存 ────────────────────────────────────────────────


def test_update_rewrites_the_file_and_keeps_a_backup(admin: TestClient, personas_dir: Path) -> None:
    _create(admin)

    response = admin.put("/v1/admin/personas/luna", json={"persona": _persona("Luna 二号")})

    assert response.status_code == 200, response.text
    assert (personas_dir / "luna.toml.bak").is_file()
    assert "Luna" in (personas_dir / "luna.toml.bak").read_text(encoding="utf-8")
    assert (
        admin.get("/v1/admin/personas/luna").json()["document"]["identity"]["display_name"]
        == "Luna 二号"
    )


def test_update_unknown_is_404(admin: TestClient) -> None:
    response = admin.put("/v1/admin/personas/nope", json={"persona": _persona()})

    assert response.status_code == 404


def test_update_with_an_invalid_document_is_400(admin: TestClient) -> None:
    _create(admin)

    response = admin.put(
        "/v1/admin/personas/luna", json={"persona": _persona(traits={"curious": 5.0})}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_persona"


# ── 删除 ────────────────────────────────────────────────


def test_delete_removes_the_file(admin: TestClient, personas_dir: Path) -> None:
    _create(admin)

    assert admin.delete("/v1/admin/personas/luna").status_code == 204

    assert not (personas_dir / "luna.toml").exists()
    assert [item["id"] for item in admin.get("/v1/admin/personas").json()["personas"]] == ["astra"]


def test_delete_the_running_persona_is_refused(admin: TestClient) -> None:
    _create(admin)

    response = admin.delete("/v1/admin/personas/astra")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "persona_active"


def test_delete_a_pending_persona_is_refused(admin: TestClient) -> None:
    """已被写进 .env、只是还没重启——删了它下次启动就没有身份了。"""
    _create(admin)
    assert admin.post("/v1/admin/personas/luna/activate").status_code == 200

    response = admin.delete("/v1/admin/personas/luna")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "persona_active"


def test_delete_the_last_persona_is_refused(admin: TestClient) -> None:
    response = admin.delete("/v1/admin/personas/astra")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "persona_last"


def test_delete_unknown_is_404(admin: TestClient) -> None:
    _create(admin)

    assert admin.delete("/v1/admin/personas/nope").status_code == 404


# ── 切换（写 .env，重启后生效）──────────────────────────


def test_activate_writes_both_keys_and_marks_pending(admin: TestClient, tmp_path: Path) -> None:
    _create(admin)

    response = admin.post("/v1/admin/personas/luna/activate")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == "luna"
    assert body["restart_required"] is True
    assert body["supervised"] is False  # 测试里没开监督——UI 要据此提示手动重启
    assert sorted(body["written"]) == ["ASTRA_COMPANION_ID", "ASTRA_PERSONA_PATH"]

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ASTRA_COMPANION_ID=luna" in env_text
    assert "ASTRA_PERSONA_PATH=" in env_text
    assert "luna.toml" in env_text

    listing = admin.get("/v1/admin/personas").json()
    assert listing["running_id"] == "astra"  # 还没重启，运行中仍是 Astra
    assert listing["pending_id"] == "luna"
    assert _by_id(admin, "luna")["is_pending"] is True
    assert _by_id(admin, "astra")["is_running"] is True
    assert _by_id(admin, "astra")["is_pending"] is False


def test_activate_keeps_the_rest_of_the_env_file(admin: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("# 我的手写注释\nASTRA_ENV=dev\n", encoding="utf-8")
    _create(admin)

    assert admin.post("/v1/admin/personas/luna/activate").status_code == 200

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ASTRA_ENV=dev" in env_text
    assert "# 我的手写注释" in env_text
    assert (tmp_path / ".env.bak").is_file()


def test_activate_unknown_is_404(admin: TestClient) -> None:
    assert admin.post("/v1/admin/personas/nope/activate").status_code == 404


def test_activate_a_broken_file_is_400(admin: TestClient, personas_dir: Path) -> None:
    (personas_dir / "broken.toml").write_text("this is not = = toml", encoding="utf-8")

    response = admin.post("/v1/admin/personas/broken/activate")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_persona"
    # 拒绝写出：坏文件不能被写进 .env，否则下次启动直接起不来
    assert not (personas_dir.parent / ".env").exists()
