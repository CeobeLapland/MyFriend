"""LLM 与 Embedding 配置。

见 ``settings/base.py`` 的模块文档字符串：这是 mixin，不是独立 BaseSettings。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr

from astra.domain.llm import ReasoningEffort

LLMProviderName = Literal["none", "lmstudio"]
EmbeddingProviderName = Literal["none", "lmstudio"]


class LLMSettingsMixin:
    """LLM 与向量化后端。"""

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
