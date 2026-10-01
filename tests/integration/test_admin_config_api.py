"""``/v1/admin/config`` 的集成测试。

**这个端点会写文件**，所以测试全部在 ``tmp_path`` 里做：``create_app`` 把 ``.env``
的路径放在 ``app.state.env_file_path`` 上，测试把它指到临时目录，
绝不碰仓库里真实的 ``.env``。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.api.routes.admin_config import _KNOWN_KEYS, _WHITELIST
from astra.settings import Settings
from tests.fakes import FakeLLMProvider, llm_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_EXAMPLE = """# 示例配置（测试用）
#
# 注释必须原样保留

# ── LLM ────────────────────────────────────────────────
ASTRA_LLM_PROVIDER=none
ASTRA_LLM_MODEL=qwen/qwen3.5-9b
ASTRA_LLM_TEMPERATURE=0.7

# 预留（注释状态）
# ASTRA_TIMEZONE=Asia/Shanghai
"""


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """刻意**不覆盖 log_level 之类的字段**：显式 init 参数会让"重启后生效值"与默认值
    不一致，从而把 ``restart_pending`` 弄成非空——那是测试脚手架的味道，不是产品行为。
    """
    params: dict[str, object] = {
        "db_path": tmp_path / "astra.db",
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path), llm=FakeLLMProvider([llm_response()]))
    app.state.env_file_path = tmp_path / ".env"
    with TestClient(app) as client:
        yield client


@pytest.fixture
def env_example(tmp_path: Path) -> Path:
    path = tmp_path / ".env.example"
    path.write_text(_EXAMPLE, encoding="utf-8")
    return path


@pytest.fixture
def admin_client(tmp_path: Path, env_example: Path) -> Iterator[TestClient]:
    with _client(tmp_path) as client:
        yield client


def _field(client: TestClient, key: str) -> dict[str, object]:
    body = client.get("/v1/admin/config").json()
    return next(item for item in body["fields"] if item["key"] == key)


# ── 白名单本身的守卫 ────────────────────────────────────


def test_whitelist_only_contains_real_settings_keys() -> None:
    """白名单里写错一个键名 = 用户一点保存就把后端写死（``extra="forbid"``）。"""
    assert set(_WHITELIST) <= _KNOWN_KEYS


def test_whitelist_covers_every_provider_and_secret_key() -> None:
    """新增 provider 时最容易漏掉的就是这两类键：枚举值本身与密钥（ADR 0032）。"""
    assert "ASTRA_LLM_PROVIDER" in _WHITELIST
    assert "ASTRA_STT_PROVIDER" in _WHITELIST
    assert "ASTRA_TTS_PROVIDER" in _WHITELIST
    assert {"ASTRA_LLM_API_KEY", "ASTRA_STT_API_KEY", "ASTRA_TTS_API_KEY"} <= set(_WHITELIST)


# ── 读 ──────────────────────────────────────────────────


def test_read_reports_paths_and_sources(admin_client: TestClient, tmp_path: Path) -> None:
    body = admin_client.get("/v1/admin/config").json()

    assert body["env_file"] == str(tmp_path / ".env")
    assert body["env_exists"] is False  # 还没写过
    assert body["example_file"] == str(tmp_path / ".env.example")
    assert body["unknown_keys"] == []
    assert body["restart_pending"] == []
    assert body["fields"], "至少要有白名单里的那些键"


def test_read_falls_back_to_the_field_default(admin_client: TestClient) -> None:
    field = _field(admin_client, "ASTRA_LLM_MODEL")

    assert field["source"] == "default"
    assert field["value"] == field["default"]
    assert field["kind"] == "text"


def test_read_derives_enum_choices_from_settings(admin_client: TestClient) -> None:
    field = _field(admin_client, "ASTRA_LLM_PROVIDER")

    assert field["kind"] == "enum"
    assert field["choices"] == ["none", "lmstudio", "openai_compatible"]


def test_read_reports_unknown_keys_in_the_file(admin_client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ASTRA_TYPO_KEY=1\nASTRA_ENV=dev\n", encoding="utf-8")

    body = admin_client.get("/v1/admin/config").json()

    assert body["unknown_keys"] == ["ASTRA_TYPO_KEY"]
    assert body["env_exists"] is True


def test_env_var_wins_over_the_file_and_is_reported_as_env(
    tmp_path: Path, env_example: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内联 ``$env:ASTRA_LLM_MODEL`` 启动时：进程环境赢，而且**重启也不会变**。

    这条是 ADR 0026 §5 的落地——不把"来源"说清楚，用户会在 Studio 里改了文件、
    重启、发现没生效，然后怀疑整个机制。
    """
    monkeypatch.setenv("ASTRA_LLM_MODEL", "inline/model")

    with _client(tmp_path) as client:
        field = _field(client, "ASTRA_LLM_MODEL")

        assert field["source"] == "env"
        assert field["effective"] == "inline/model"  # 进程真的在用这个
        assert field["restart_pending"] is False  # 重启也会继续用它

        body = client.put(
            "/v1/admin/config", json={"values": {"ASTRA_LLM_MODEL": "nvidia/nemotron-3-nano-4b"}}
        ).json()

        # 文件写了，但环境变量仍然赢——如实回答"重启也不会变"
        assert body["written"] == ["ASTRA_LLM_MODEL"]
        assert body["restart_pending"] == []


# ── 写 ──────────────────────────────────────────────────


def test_write_rejects_unknown_keys(admin_client: TestClient) -> None:
    response = admin_client.put("/v1/admin/config", json={"values": {"ASTRA_NOPE": "1"}})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_key"


def test_write_rejects_empty_body(admin_client: TestClient) -> None:
    response = admin_client.put("/v1/admin/config", json={"values": {}})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "empty_update"


def test_write_rejects_newlines(admin_client: TestClient) -> None:
    response = admin_client.put(
        "/v1/admin/config", json={"values": {"ASTRA_LOG_LEVEL": "DEBUG\nASTRA_ENV=prod"}}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_value"


def test_write_creates_env_from_the_example(admin_client: TestClient, tmp_path: Path) -> None:
    response = admin_client.put(
        "/v1/admin/config", json={"values": {"ASTRA_LLM_MODEL": "nvidia/nemotron-3-nano-4b"}}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["created"] is True  # 这次是从 .env.example 复制出来的
    assert body["written"] == ["ASTRA_LLM_MODEL"]

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ASTRA_LLM_MODEL=nvidia/nemotron-3-nano-4b" in text
    assert "# 注释必须原样保留" in text  # 注释没丢
    assert "# ASTRA_TIMEZONE=Asia/Shanghai" in text
    assert not (tmp_path / ".env.bak").exists()  # 之前没有文件，就没有备份


def test_write_backs_up_and_only_touches_the_given_key(
    admin_client: TestClient, tmp_path: Path
) -> None:
    admin_client.put("/v1/admin/config", json={"values": {"ASTRA_LLM_MODEL": "model-a"}})
    before = (tmp_path / ".env").read_text(encoding="utf-8")

    admin_client.put("/v1/admin/config", json={"values": {"ASTRA_LLM_TEMPERATURE": "0.9"}})

    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == before
    after = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ASTRA_LLM_MODEL=model-a" in after
    assert "ASTRA_LLM_TEMPERATURE=0.9" in after


def test_write_rejects_invalid_values_without_touching_the_file(
    admin_client: TestClient, tmp_path: Path
) -> None:
    admin_client.put("/v1/admin/config", json={"values": {"ASTRA_LLM_MODEL": "model-a"}})
    before = (tmp_path / ".env").read_text(encoding="utf-8")

    response = admin_client.put(
        "/v1/admin/config", json={"values": {"ASTRA_LLM_TEMPERATURE": "abc"}}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_value"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before
    assert not (tmp_path / ".env.candidate").exists()


def test_write_reports_restart_pending(admin_client: TestClient) -> None:
    body = admin_client.put(
        "/v1/admin/config", json={"values": {"ASTRA_LLM_MODEL": "nvidia/nemotron-3-nano-4b"}}
    ).json()

    # 改了文件但进程还在用旧值 —— UI 要知道"重启后才会变"
    assert body["restart_pending"] == ["ASTRA_LLM_MODEL"]
    assert _field(admin_client, "ASTRA_LLM_MODEL")["restart_pending"] is True


def test_write_is_not_in_the_openapi_paths_of_config(admin_client: TestClient) -> None:
    """``/v1/config`` 的 key 集合不可变（ADR 0016）——新信息只能走 /v1/admin。"""
    body = admin_client.get("/v1/config").json()

    assert "fields" not in body
    paths = admin_client.get("/openapi.json").json()["paths"]
    assert "/v1/admin/config" in paths


# ── 密钥：只写不读（ADR 0032）────────────────────────────


def test_secret_fields_never_echo_their_value(admin_client: TestClient) -> None:
    """读接口里不可能出现密钥明文——连默认值都不回显。"""
    field = _field(admin_client, "ASTRA_LLM_API_KEY")

    assert field["kind"] == "secret"
    assert field["value"] == ""
    assert field["default"] is None
    assert field["effective"] in {"已配置", "未配置"}
    assert "lm-studio-local" not in str(field)


def test_blank_secret_is_not_written(admin_client: TestClient, tmp_path: Path) -> None:
    """留空 = 不改动。否则每次保存都会把已配好的密钥清成空串。"""
    admin_client.put("/v1/admin/config", json={"values": {"ASTRA_LLM_API_KEY": "sk-real"}})
    before = (tmp_path / ".env").read_text(encoding="utf-8")

    response = admin_client.put(
        "/v1/admin/config",
        json={"values": {"ASTRA_LLM_API_KEY": "   ", "ASTRA_LLM_MODEL": "model-b"}},
    )

    assert response.status_code == 200
    assert response.json()["written"] == ["ASTRA_LLM_MODEL"]
    after = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ASTRA_LLM_API_KEY=sk-real" in after
    assert before != after  # 只有模型那一行变了


def test_only_blank_secrets_is_an_empty_update(admin_client: TestClient) -> None:
    response = admin_client.put("/v1/admin/config", json={"values": {"ASTRA_STT_API_KEY": " "}})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "empty_update"


def test_non_secret_blank_still_clears_the_value(admin_client: TestClient, tmp_path: Path) -> None:
    """非密钥键的留空照旧是"真的清空"——两者语义不同，不能一起改。"""
    response = admin_client.put(
        "/v1/admin/config", json={"values": {"ASTRA_TTS_REFERENCE_AUDIO": ""}}
    )

    assert response.status_code == 200
    assert response.json()["written"] == ["ASTRA_TTS_REFERENCE_AUDIO"]
