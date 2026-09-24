"""角色状态配置（M3）。

见 ``settings/base.py`` 的模块文档字符串：这是 mixin，不是独立 BaseSettings。
"""

from __future__ import annotations

from pydantic import Field


class StateSettingsMixin:
    """情绪衰减与靠拢比例。"""

    # ── State（M3）──────────────────────────────────────
    #: 情绪衰减半衰期（分钟）。**这个值缺乏依据**——先给 120 分钟，
    #: 需要 docs/10 §5 的固定对话集来调参（ADR 0020）。
    state_emotion_decay_half_life_minutes: int = Field(default=120, gt=0)
    #: 朝"已表达情绪的目标水平"靠拢的比例。0 = 完全不受表达影响。
    #: **不能改成加法**——加法会形成正反馈把情绪顶到 1.0（ADR 0020）。
    state_emotion_pull: float = Field(default=0.15, ge=0.0, le=1.0)
