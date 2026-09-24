"""运行环境 / 日志 / HTTP / 数据 / 健康检查配置。

**这是 ``Settings`` 的一个 mixin，不是独立的 BaseSettings。**
整个配置面仍然只有**一个** ``BaseSettings``（见 ``settings/__init__.py``），
字段名与 ``ASTRA_*`` 环境变量名与拆分前逐字一致——这是一次纯组织性重构，
不改任何调用方。

为什么不拆成嵌套的 ``BaseSettings`` 子模型：pydantic-settings v2 中嵌套
``BaseSettings`` 子模型的 env 填充顺序与 ``default_factory`` 交互存在歧义
（原 ``settings.py`` 顶部注释记录的结论）。mixin 方案没有这个问题。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

LogFormat = Literal["json", "text"]
Environment = Literal["dev", "test", "prod"]


def blank_to_none(value: object) -> object:
    """把 ``ASTRA_X=`` 这类空串归一化为 ``None``。

    ``.env.example`` 里"留空表示不启用"的键都走它。**不做这个归一化的话**
    ``Path("")`` 会被解析成当前目录，静默变成"配了一个空路径"。
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


class BaseSettingsMixin:
    """运行环境、日志、HTTP、数据目录与健康检查。"""

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
    #: M1 还没有 users 表，用一个固定本地用户标识
    user_id: str = "local"
    #: 人格配置（随仓库分发，不是用户数据）
    persona_path: Path = Path("./personas/astra.toml")
    #: 空闲多久后自动结束当前会话并新开一个（ADR 0012）
    conversation_idle_minutes: int = Field(default=60, ge=1)

    # ── 健康检查 ────────────────────────────────────────
    health_probe_timeout_s: float = Field(default=2.0, gt=0)
    health_db_required: bool = True

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """把 ``ASTRA_LOG_FILE=`` 这类空串归一化为 None。"""
        return blank_to_none(value)
