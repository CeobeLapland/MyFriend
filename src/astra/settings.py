"""应用配置。

所有配置通过环境变量或 `.env` 注入，统一前缀 ``ASTRA_``。

设计要点（见 docs/decisions/0006 与本文件顶部注释）：

- **扁平结构**：不使用嵌套子模型。pydantic-settings v2 中嵌套 ``BaseSettings``
  子模型的 env 填充顺序与 ``default_factory`` 交互存在歧义，扁平字段零歧义。
  字段超过 ~30 个时（预计 M4）再评估分组。
- **``extra="forbid"``**：`.env` 中拼错的键会直接导致启动失败，避免"改了配置没生效"
  这类静默故障。副作用是 ``.env.example`` 中未实现的预留键必须保持注释状态。

  已知边界：该设置**只作用于 `.env` 文件和构造参数**，不覆盖系统环境变量
  （pydantic-settings 会忽略环境里未匹配到字段的 ``ASTRA_*`` 键）。
  `.env` 是本项目的主要配置面，因此这个覆盖范围足够；但若直接 `setx` 了一个拼错
  的环境变量，它会被静默忽略。见 `tests/unit/test_settings.py::test_stray_env_var_is_ignored`。
- **``SecretStr``**：API key 在 ``repr()`` / 日志中不会泄露明文。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["json", "text"]
Environment = Literal["dev", "test", "prod"]
LLMProviderName = Literal["none", "lmstudio"]


class Settings(BaseSettings):
    """Astra 运行时配置。"""

    model_config = SettingsConfigDict(
        env_prefix="ASTRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    # ── 运行环境 ────────────────────────────────────────
    env: Environment = "dev"

    # ── 日志 ────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    log_file: Path | None = None

    # ── HTTP ────────────────────────────────────────────
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65535)

    # ── 数据 ────────────────────────────────────────────
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/astra.db")
    companion_id: str = "astra"

    # ── LLM（M1 起生效；M0 保持 none）────────────────────
    llm_provider: LLMProviderName = "none"
    llm_base_url: str = "http://localhost:1234/v1"
    llm_api_key: SecretStr = SecretStr("lm-studio-local")
    llm_model: str = "qwen/qwen3.5-9b"
    llm_fallback_model: str = "nvidia/nemotron-3-nano-4b"
    llm_connect_timeout_s: float = Field(default=5.0, gt=0)
    llm_request_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_tokens: int = Field(default=1024, ge=1)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # ── 健康检查 ────────────────────────────────────────
    health_probe_timeout_s: float = Field(default=2.0, gt=0)
    health_db_required: bool = True

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """把 ``ASTRA_LOG_FILE=`` 这类空串归一化为 None。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    缓存是为了避免每次请求都重新解析 `.env`。测试中若需不同配置，
    应直接构造 ``Settings(...)`` 而不是依赖本函数。
    """
    return Settings()
