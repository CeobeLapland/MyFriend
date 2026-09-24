"""记忆子系统与检索权重配置（M2）。

见 ``settings/base.py`` 的模块文档字符串：这是 mixin，不是独立 BaseSettings。
"""

from __future__ import annotations

from pydantic import Field, model_validator


class MemorySettingsMixin:
    """记忆开关、候选与检索参数。"""

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

    # ── 检索权重（docs/06 §4）───────────────────────────
    #: 文档明文要求"放在配置中，不能硬编码在多个文件"。**五者之和必须为 1.0**，
    #: 否则排序分的量纲会随配置漂移（见下方 validator）。
    #: 注意：它们只影响**排序**，不影响准入（准入由 memory_min_similarity 决定）。
    memory_weight_semantic: float = Field(default=0.50, ge=0.0, le=1.0)
    memory_weight_importance: float = Field(default=0.20, ge=0.0, le=1.0)
    memory_weight_recency: float = Field(default=0.15, ge=0.0, le=1.0)
    memory_weight_access: float = Field(default=0.05, ge=0.0, le=1.0)
    memory_weight_relationship: float = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _retrieval_weights_sum_to_one(self) -> MemorySettingsMixin:
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
