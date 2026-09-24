"""``/health`` 的集成测试。

走真实的 FastAPI + lifespan + 临时 SQLite 文件。

用 ``TestClient`` 的上下文管理器形式（同步）：``httpx.ASGITransport`` 不会触发
lifespan，而建库迁移就发生在 lifespan 里，用它测试会得到 "no such table" 的
诡异失败。``TestClient`` 本身也基于 httpx，且 ``with`` 块会跑 lifespan。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astra.api.app import create_app
from astra.domain.health import CANONICAL_COMPONENTS
from astra.infrastructure.db.migrations import expected_version
from astra.settings import Settings

#: WAL 模式会额外产生 -wal / -shm 边车文件，清理时必须一并覆盖
_DB_FILES = ("astra.db", "astra.db-wal", "astra.db-shm")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "astra.db"


def _make_settings(db_path: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, db_path=db_path, log_level="WARNING", **overrides)


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_make_settings(db_path))) as test_client:
        yield test_client


def _remove_database(db_path: Path) -> None:
    for name in _DB_FILES:
        db_path.with_name(name).unlink(missing_ok=True)


# ── 验收项：health returns 200 ──────────────────────────


def test_health_returns_200(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_health_is_repeatable(client: TestClient) -> None:
    for _ in range(3):
        assert client.get("/health").status_code == 200


def test_uptime_does_not_go_backwards(client: TestClient) -> None:
    first = client.get("/health").json()["uptime_s"]
    second = client.get("/health").json()["uptime_s"]

    assert second >= first


# ── 验收项：empty DB can be initialized from scratch ────


def test_lifespan_creates_database_file(client: TestClient, db_path: Path) -> None:
    client.get("/health")

    assert db_path.is_file()


def test_lifespan_reports_schema_version(client: TestClient) -> None:
    latest = expected_version()
    assert latest is not None

    assert client.get("/health").json()["schema_version"] == int(latest)


# ── 响应契约（ADR 0006）─────────────────────────────────


def test_response_top_level_keys_are_fixed(client: TestClient) -> None:
    body = client.get("/health").json()

    assert set(body) == {"status", "version", "schema_version", "uptime_s", "components"}


def test_all_canonical_components_present(client: TestClient) -> None:
    """M0 就要输出全部 5 个 key，客户端从第一天起就能写死解析逻辑。"""
    body = client.get("/health").json()

    assert set(body["components"]) == set(CANONICAL_COMPONENTS)


def test_m0_baseline_is_ok(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["components"]["api"]["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"


def test_llm_stt_tts_are_not_configured(client: TestClient) -> None:
    """M0 不接 LLM/STT/TTS，必须诚实上报 not_configured 而不是 ok。"""
    components = client.get("/health").json()["components"]

    for name in ("llm", "stt", "tts"):
        assert components[name]["status"] == "not_configured"


# ── M4：STT/TTS 接上之后只改值，不改 key 集合 ────────────


@pytest.mark.parametrize(
    ("component", "overrides"),
    [
        ("stt", {"stt_provider": "cosyvoice"}),
        ("tts", {"tts_provider": "cosyvoice"}),
    ],
)
def test_configured_audio_provider_reports_a_real_status(
    db_path: Path, component: str, overrides: dict[str, object]
) -> None:
    """配了 CosyVoice 就注册探针——状态是 ``ok`` 还是 ``down`` 取决于后端是否在跑，
    但**绝不能**还是 ``not_configured``（那等于假装没配）。
    """
    with TestClient(create_app(_make_settings(db_path, **overrides))) as client:
        body = client.get("/health").json()

    assert set(body["components"]) == set(CANONICAL_COMPONENTS)
    assert body["components"][component]["status"] in {"ok", "down"}


def test_audio_providers_do_not_change_the_component_key_set(db_path: Path) -> None:
    """ADR 0006：``/health`` 的 5 个 key 不可变，M4 只改 ``stt``/``tts`` 的值。"""
    settings = _make_settings(db_path, stt_provider="cosyvoice", tts_provider="cosyvoice")

    with TestClient(create_app(settings)) as client:
        components = client.get("/health").json()["components"]

    assert set(components) == set(CANONICAL_COMPONENTS)


# ── 降级行为（HTTP 恒 200）──────────────────────────────


def test_health_returns_200_when_database_disappears(client: TestClient, db_path: Path) -> None:
    """运行期数据库故障不能让 /health 失败：HTTP 恒 200，严重程度只在 body。"""
    _remove_database(db_path)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["components"]["database"]["status"] == "down"
    assert body["status"] == "down"


def test_database_not_required_degrades_instead_of_down(db_path: Path) -> None:
    settings = _make_settings(db_path, health_db_required=False)

    with TestClient(create_app(settings)) as client:
        _remove_database(db_path)
        body = client.get("/health").json()

    assert body["components"]["database"]["status"] == "down"
    assert body["status"] == "degraded"
