"""应用配置。

所有配置通过环境变量或 `.env` 注入，统一前缀 ``ASTRA_``。

**本包是 M4 从单个 ``settings.py`` 拆出来的**，动机是字段数已经涨到 45+，
超过原注释里"~30 个时再评估分组"的阈值（ADR 0018/0020 把分组排到 M4）。

拆法：**按域拆成 mixin 模块，字段名与 ``ASTRA_*`` 环境变量名逐字不变**，
最终仍然是**一个** ``BaseSettings``。所以：

- 读取点**一个都没改**（``settings.llm_model`` 照旧）——这是纯组织性重构。
- ``.env.example`` 不用因为拆分而动一个字节。
- ``extra="forbid"`` / ``SecretStr`` / 各 validator 的语义全部保持。

为什么不用嵌套的 ``BaseSettings`` 子模型（那样读起来更像"分组"）：
pydantic-settings v2 中嵌套 ``BaseSettings`` 子模型的 env 填充顺序与
``default_factory`` 交互存在歧义；而且嵌套会把 env 名改成
``ASTRA_LLM__BASE_URL`` 这种双下划线形式，等于换了一套配置契约。
mixin 方案两个问题都没有。

设计要点（原 ``settings.py`` 顶部注释，结论不变）：

- **扁平结构**：不使用嵌套子模型。字段定义分散在 ``base`` / ``llm`` / ``memory`` /
  ``state`` / ``audio`` 五个 mixin 里，但组合后仍是同一层扁平字段。
  新增字段时务必同步 ``.env.example``。
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

from pydantic_settings import BaseSettings, SettingsConfigDict

from astra.settings.audio import AudioProviderName, AudioSettingsMixin
from astra.settings.base import BaseSettingsMixin, Environment, LogFormat
from astra.settings.llm import EmbeddingProviderName, LLMProviderName, LLMSettingsMixin
from astra.settings.memory import MemorySettingsMixin
from astra.settings.state import StateSettingsMixin

__all__ = [
    "AudioProviderName",
    "EmbeddingProviderName",
    "Environment",
    "LLMProviderName",
    "LogFormat",
    "Settings",
    "get_settings",
]


class Settings(
    BaseSettingsMixin,
    LLMSettingsMixin,
    MemorySettingsMixin,
    StateSettingsMixin,
    AudioSettingsMixin,
    BaseSettings,
):
    """Astra 运行时配置。

    字段定义在五个 mixin 里（按域），这里只放 ``model_config`` 与全局约定。
    """

    model_config = SettingsConfigDict(
        env_prefix="ASTRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    缓存是为了避免每次请求都重新解析 `.env`。测试中若需不同配置，
    应直接构造 ``Settings(...)`` 而不是依赖本函数。
    """
    return Settings()
