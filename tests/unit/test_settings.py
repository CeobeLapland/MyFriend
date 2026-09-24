"""``astra.settings`` 的单元测试。

覆盖：默认值、环境变量覆盖、非法值拒绝、`extra="forbid"` 生效、SecretStr 不泄露。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from astra.domain.llm import ReasoningEffort
from astra.settings import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.env == "dev"
    assert settings.log_format == "json"
    assert settings.log_file is None
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 8000
    assert settings.data_dir == Path("data")
    assert settings.db_path == Path("data/astra.db")
    assert settings.companion_id == "astra"
    assert settings.user_id == "local"
    assert settings.persona_path == Path("personas/astra.toml")
    assert settings.conversation_idle_minutes == 60
    assert settings.llm_provider == "none"
    assert settings.llm_model == "qwen/qwen3.5-9b"
    assert settings.llm_fallback_model == "nvidia/nemotron-3-nano-4b"
    assert settings.llm_max_tokens == 400
    assert settings.llm_reasoning_effort is ReasoningEffort.NONE
    assert settings.llm_history_max_messages == 20
    assert settings.llm_history_max_chars == 12000
    assert settings.health_probe_timeout_s == 2.0
    assert settings.health_db_required is True
    # M2
    assert settings.embedding_provider == "none"
    assert settings.embedding_model == "text-embedding-nomic-embed-text-v1.5"
    assert settings.embedding_dim == 768
    assert settings.memory_enabled is True
    assert settings.memory_top_k == 5
    assert settings.memory_max_candidates_per_turn == 3
    assert settings.memory_dedup_threshold == 0.90
    assert settings.memory_min_similarity == 0.70
    assert settings.memory_context_max_chars == 2000


def test_reasoning_effort_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测 qwen3.5-9b 默认开思考，必须能通过配置关掉。"""
    monkeypatch.setenv("ASTRA_LLM_REASONING_EFFORT", "none")

    assert Settings(_env_file=None).llm_reasoning_effort is ReasoningEffort.NONE


def test_invalid_reasoning_effort_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_LLM_REASONING_EFFORT", "maximum")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_HTTP_PORT", "9001")
    monkeypatch.setenv("ASTRA_LLM_PROVIDER", "lmstudio")
    monkeypatch.setenv("ASTRA_COMPANION_ID", "neuro")
    monkeypatch.setenv("ASTRA_HEALTH_DB_REQUIRED", "false")

    settings = Settings(_env_file=None)

    assert settings.http_port == 9001
    assert settings.llm_provider == "lmstudio"
    assert settings.companion_id == "neuro"
    assert settings.health_db_required is False


def test_blank_log_file_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` 里写的是 `ASTRA_LOG_FILE=`，空串必须归一化为 None。"""
    monkeypatch.setenv("ASTRA_LOG_FILE", "   ")

    assert Settings(_env_file=None).log_file is None


def test_log_file_path_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_LOG_FILE", "./logs/astra.log")

    assert Settings(_env_file=None).log_file == Path("logs/astra.log")


@pytest.mark.parametrize("port", ["0", "65536", "-1", "not-a-number"])
def test_invalid_port_rejected(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("ASTRA_HTTP_PORT", port)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_llm_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_LLM_PROVIDER", "openai")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dotenv_file_is_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ASTRA_HTTP_PORT=9000\n", encoding="utf-8")

    assert Settings(_env_file=env_file).http_port == 9000


def test_extra_key_in_dotenv_rejected(tmp_path: Path) -> None:
    """`extra="forbid"`：`.env` 里拼错的键必须报错，而不是被静默忽略。"""
    env_file = tmp_path / ".env"
    env_file.write_text("ASTRA_HTTP_PORT=9000\nASTRA_HTTP_POTR=8000\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_extra_init_kwarg_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, not_a_field=1)


def test_stray_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """已知边界：pydantic-settings 不会因**系统环境变量**里的未知 ASTRA_ 键报错。

    `extra="forbid"` 只作用于 `.env` 文件和 init 参数。详见 settings.py 模块文档字符串。
    """
    monkeypatch.setenv("ASTRA_TYPO_KEY", "abc")

    assert Settings(_env_file=None).http_port == 8000


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 ASTRA_ 前缀的环境变量不应干扰配置（系统里有大量这类变量）。"""
    monkeypatch.setenv("HTTP_PORT", "1234")

    assert Settings(_env_file=None).http_port == 8000


def test_secret_is_not_leaked() -> None:
    settings = Settings(_env_file=None, llm_api_key="super-secret-value")

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings)
    assert settings.llm_api_key.get_secret_value() == "super-secret-value"


def test_get_settings_is_cached() -> None:
    from astra.settings import get_settings

    assert get_settings() is get_settings()


# ── 检索权重（docs/06 §4）───────────────────────────────


def test_retrieval_weights_default_to_a_weighted_average() -> None:
    settings = Settings(_env_file=None)

    total = (
        settings.memory_weight_semantic
        + settings.memory_weight_importance
        + settings.memory_weight_recency
        + settings.memory_weight_access
        + settings.memory_weight_relationship
    )
    assert total == pytest.approx(1.0)


def test_retrieval_weights_reject_a_broken_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    """和不为 1 会让 memory_min_score 的含义随配置漂移——启动期就该拒绝。"""
    monkeypatch.setenv("ASTRA_MEMORY_WEIGHT_SEMANTIC", "0.9")

    with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
        Settings(_env_file=None)


def test_retrieval_weights_can_be_rebalanced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_MEMORY_WEIGHT_SEMANTIC", "0.60")
    monkeypatch.setenv("ASTRA_MEMORY_WEIGHT_IMPORTANCE", "0.10")

    settings = Settings(_env_file=None)

    assert settings.memory_weight_semantic == 0.60
    assert settings.memory_weight_importance == 0.10


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ASTRA_MEMORY_WEIGHT_SEMANTIC", "-0.1"),
        ("ASTRA_MEMORY_DEDUP_THRESHOLD", "1.5"),
        ("ASTRA_MEMORY_MIN_SIMILARITY", "-1"),
        ("ASTRA_MEMORY_TOP_K", "-1"),
        ("ASTRA_EMBEDDING_DIM", "0"),
        ("ASTRA_EMBEDDING_PROVIDER", "openai"),
    ],
)
def test_out_of_range_memory_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_embedding_provider_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_EMBEDDING_PROVIDER", "lmstudio")

    assert Settings(_env_file=None).embedding_provider == "lmstudio"
