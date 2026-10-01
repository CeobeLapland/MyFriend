"""LLM 与 Embedding 配置。

见 ``settings/base.py`` 的模块文档字符串：这是 mixin，不是独立 BaseSettings。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr

from astra.domain.llm import ReasoningEffort

#: ``openai_compatible`` = 任何 OpenAI 兼容的第三方 API（云端或自建）。
#: 它与 ``lmstudio`` 走同一个 ``/chat/completions`` 形状，差的是**参数容忍度**：
#: 本地 LM Studio 接受 ``reasoning_effort`` 与 ``json_schema``，而多数第三方厂商
#: 会对不认识的参数直接 400。所以那两个参数由下面两个开关控制，默认**不发**。
LLMProviderName = Literal["none", "lmstudio", "openai_compatible"]
#: 向量化**没有** openai_compatible 一档：`memory_vectors` 的 ``float[768]`` 写在
#: 迁移 0003 里（不可改），换成 1536 维的云端 embedding 需要新迁移 + 重建索引。
#: 所以这一档留给后续里程碑，见 README「已知限制」。
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

    # ── openai_compatible 的参数容忍度 ──────────────────
    #: 把 ``reasoning_effort`` 发给后端吗？
    #:
    #: **默认不发**：多数第三方厂商不认识这个参数，会直接回 400。只有在你确认
    #: 厂商支持它（OpenAI 的 o 系 / gpt-5 系、部分国产推理模型）时才打开。
    #: lmstudio 路径**不受本项影响**——它必须发 ``reasoning_effort=none``
    #: 才能关掉 qwen3.5-9b 的思考（实测 15.4s → 3.4s），那条是本地专属的已知事实。
    llm_send_reasoning_effort: bool = False
    #: 用 ``response_format: {type: json_schema, strict: true}`` 吗？
    #:
    #: **默认否**：退回更通用的 ``{"type": "json_object"}``（几乎所有兼容厂商都认）。
    #: 关掉它只是少了一层硬保证，不是不能用——system prompt 的 Layer 8 里本来就写着
    #: 逐字段的格式说明，且 ``response_parser`` 能容忍围栏 JSON 与多余键（ADR 0011）。
    #: lmstudio 路径同样不受影响（实测支持并一直使用 json_schema）。
    llm_use_json_schema: bool = False

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
    #: 向量化后端的密钥。与 ``llm_api_key`` **分开**：用了第三方 embedding 却仍
    #: 复用 LLM 的 key 会在换 LLM 厂商时静默断掉记忆。默认值与 LM Studio 的无鉴权
    #: 惯例一致（本地随便填一个非空值即可）。
    embedding_api_key: SecretStr = SecretStr("lm-studio-local")
