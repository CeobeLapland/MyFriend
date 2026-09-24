"""人格的领域值对象。

加载与校验在 :mod:`astra.infrastructure.persona.toml_loader`（ADR 0015）。
本模块只承载数据，不依赖任何框架。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Identity:
    """Layer 1：身份。"""

    name: str
    display_name: str
    pronouns: str
    self_description: str
    version: str = "1"
    backstory: str | None = None


@dataclass(frozen=True, slots=True)
class Communication:
    """Layer 3：说话风格。"""

    sentence_length: str
    emoji_frequency: str
    rhetorical_questions: str
    preferred_interaction: str
    language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class Persona:
    """一个角色人格的完整描述。"""

    identity: Identity
    communication: Communication
    #: Layer 2：性格倾向，键 -> 0..1 的强度
    traits: Mapping[str, float] = field(default_factory=dict)
    #: Layer 2：行为规则（`docs/05` §3 的"行为规则"）
    behavior_rules: tuple[str, ...] = ()
    #: Layer 0：追加到基础硬约束之后的角色特有约束
    hard_constraints: tuple[str, ...] = ()
    #: 错误码 -> 自定义话术（ADR 0014）
    error_messages: Mapping[str, str] = field(default_factory=dict)

    def error_message(self, code: str, *, default: str) -> str:
        """取该错误码的自定义话术；未配置则回落到代码默认文案。"""
        return self.error_messages.get(code, default)
