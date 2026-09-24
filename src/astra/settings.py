"""应用配置。

所有配置通过环境变量或 `.env` 注入，统一前缀 ``ASTRA_``。

设计要点（见 docs/decisions/0006 与本文件顶部注释）：

- **扁平结构**：不使用嵌套子模型。pydantic-settings v2 中嵌套 ``BaseSettings``
  子模型的 env 填充顺序与 ``default_factory`` 交互存在歧义，扁平字段零歧义。
  **M2 已到 ~47 个字段**，超过了原先"~30 个时再评估分组"的阈值——但分组要动
  全部读取点，风险与收益不成比例，所以 M2 保持扁平，把分组排到 M3/M4（ADR 0018
  记为技术债）。新增字段时务必同步 ``.env.example``。
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

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from astra.domain.llm import ReasoningEffort

LogFormat = Literal["json", "text"]
Environment = Literal["dev", "test", "prod"]
LLMProviderName = Literal["none", "lmstudio"]
EmbeddingProviderName = Literal["none", "lmstudio"]


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
    #: M1 还没有 users 表，用一个固定本地用户标识
    user_id: str = "local"
    #: 人格配置（随仓库分发，不是用户数据）
    persona_path: Path = Path("./personas/astra.toml")
    #: 空闲多久后自动结束当前会话并新开一个（ADR 0012）
    conversation_idle_minutes: int = Field(default=60, ge=1)

    # ── LLM ─────────────────────────────────────────────
    llm_provider: LLMProviderName = "none"
    llm_base_url: str = "http://localhost:1234/v1"
    llm_api_key: SecretStr = SecretStr("lm-studio-local")
    llm_model: str = "qwen/qwen3.5-9b"
    llm_fallback_model: str = "nvidia/nemotron-3-nano-4b"
    llm_connect_timeout_s: float = Field(default=5.0, gt=0)
    llm_request_timeout_s: float = Field(default=120.0, gt=0)
    #: 4 字段 profile 实测只用 ~90-115 token；给最坏延迟一个上界（ADR 0011）
    llm_max_tokens: int = Field(default=400, ge=1)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    #: 默认 none：实测 qwen3.5-9b 开思考时一次问候要 15.4s，关掉后 3.4s。
    #: `enable_thinking=false` 与 `/no_think` 都无效，只有这个参数管用。
    llm_reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    #: 上下文窗口预算（按字符，不是 token——本地没有 qwen 的词表）
    llm_history_max_messages: int = Field(default=20, ge=1)
    llm_history_max_chars: int = Field(default=12000, ge=1)

    # ── 健康检查 ────────────────────────────────────────
    health_probe_timeout_s: float = Field(default=2.0, gt=0)
    health_db_required: bool = True

    # ── Embedding（M2）──────────────────────────────────
    # none = 不向量化（记忆子系统降级为"不检索、不写入"）
    embedding_provider: EmbeddingProviderName = "none"
    #: 通常与 llm_base_url 相同（LM Studio 同一个端口），但分开配置：
    #: embedding 模型与 chat 模型是两个独立加载项，端点也可能不同。
    embedding_base_url: str = "http://localhost:1234/v1"
    embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    #: **必须与迁移 0003 里 memory_vectors 的 float[N] 一致**，装配期会校验。
    #: 实测 text-embedding-nomic-embed-text-v1.5 输出 768 维。
    embedding_dim: int = Field(default=768, gt=0)
    embedding_timeout_s: float = Field(default=30.0, gt=0)

    # ── Memory（M2）─────────────────────────────────────
    #: 启动期的默认值。运行时开关存 app_flags（PUT /v1/memories/settings），
    #: 那份记录存在时以它为准（docs/06 §9 要求"禁用记忆"能立即生效）。
    memory_enabled: bool = True
    #: 每轮注入 prompt 的记忆条数上限
    memory_top_k: int = Field(default=5, ge=0)
    #: KNN 过采样数：先取这么多候选，重排序后再截断到 memory_top_k
    memory_candidate_limit: int = Field(default=20, ge=1)
    #: 一轮对话最多落库几条候选（docs/05 §8："候选不是每轮都创建"）
    memory_max_candidates_per_turn: int = Field(default=3, ge=0)
    #: 余弦相似度超过它就合并而不是新建（docs/06 §6）
    memory_dedup_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    #: **准入闸门**：原始余弦相似度低于它的记忆根本不进 prompt。
    #: 与排序总分是两件事——加权公式会把"重不重要"混进来，不适合当闸门。
    #: 默认 0.70 是按实测校准的：相关对 0.64–0.83、无关对 0.62–0.74（重叠），
    #: 取偏严的一侧，宁可漏也不要塞不相干的记忆（ADR 0018）。
    memory_min_similarity: float = Field(default=0.70, ge=0.0, le=1.0)
    #: 注入 prompt 的记忆层字符预算
    memory_context_max_chars: int = Field(default=2000, ge=1)

    # ── State（M3）──────────────────────────────────────
    #: 情绪衰减半衰期（分钟）。**这个值缺乏依据**——先给 120 分钟，
    #: 需要 docs/10 §5 的固定对话集来调参（ADR 0020）。
    state_emotion_decay_half_life_minutes: int = Field(default=120, gt=0)
    #: 朝"已表达情绪的目标水平"靠拢的比例。0 = 完全不受表达影响。
    #: **不能改成加法**——加法会形成正反馈把情绪顶到 1.0（ADR 0020）。
    state_emotion_pull: float = Field(default=0.15, ge=0.0, le=1.0)

    # ── 检索权重（docs/06 §4）───────────────────────────
    #: 文档明文要求"放在配置中，不能硬编码在多个文件"。**五者之和必须为 1.0**，
    #: 否则排序分的量纲会随配置漂移（见下方 validator）。
    #: 注意：它们只影响**排序**，不影响准入（准入由 memory_min_similarity 决定）。
    memory_weight_semantic: float = Field(default=0.50, ge=0.0, le=1.0)
    memory_weight_importance: float = Field(default=0.20, ge=0.0, le=1.0)
    memory_weight_recency: float = Field(default=0.15, ge=0.0, le=1.0)
    memory_weight_access: float = Field(default=0.05, ge=0.0, le=1.0)
    memory_weight_relationship: float = Field(default=0.10, ge=0.0, le=1.0)

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """把 ``ASTRA_LOG_FILE=`` 这类空串归一化为 None。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _retrieval_weights_sum_to_one(self) -> Settings:
        """检索权重必须构成加权平均（`docs/06` §4）。

        和不为 1 时 ``memory_min_score`` 的含义会随配置改变——那是个很难查的
        配置漂移，所以在启动期直接拒绝，而不是等到检索结果"看起来怪怪的"。
        """
        total = (
            self.memory_weight_semantic
            + self.memory_weight_importance
            + self.memory_weight_recency
            + self.memory_weight_access
            + self.memory_weight_relationship
        )
        if abs(total - 1.0) > 1e-6:
            msg = (
                "memory retrieval weights must sum to 1.0 "
                f"(semantic + importance + recency + access + relationship), got {total}"
            )
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    缓存是为了避免每次请求都重新解析 `.env`。测试中若需不同配置，
    应直接构造 ``Settings(...)`` 而不是依赖本函数。
    """
    return Settings()
