"""System prompt 的分层组装（`docs/05_PERSONA_AND_PROMPTS.md` §1）。

纯函数：同一组输入必须产出**逐字节相同**的字符串（`docs/10` §2 要求确定性）。

层序与 M1 状态：

======  ==========================  ==========================================
层      内容                        M1
======  ==========================  ==========================================
0       Safety / hard constraints   实现
1       Identity                    实现
2       Stable personality          实现（traits + behavior rules）
3       Speech style                实现
4       Current state               **未实现** —— M3 在 Layer 3 之后插入
5       Relevant memories           **未实现** —— M2 在 Layer 4 之后插入
6       Current conversation        走 ``LLMRequest.messages``，不进 system prompt
7       Tool definitions            **未实现** —— M6 在 Layer 5 之后插入
8       Output schema               实现
======  ==========================  ==========================================

未实现的层**整段省略**，不输出空标题——空标题会让模型以为存在相应信息。

关于 Layer 8：这里只放**人话版字段说明 + 英文词表清单**，不贴整段 JSON Schema。
硬约束交给 ``response_format`` 的 ``json_schema``；词表必须写进 prompt，因为实测
模型会在自由字符串字段里写中文（``emotion.primary="欢迎"``），只靠代码归一化
会让几乎所有情绪被降级成 neutral，人格表达直接消失。
"""

from __future__ import annotations

from astra.domain.persona import Persona
from astra.domain.vocabulary import ACTION_CHOICES, EMOTION_CHOICES, VOICE_STYLE_CHOICES

#: 层与层之间
SECTION_SEPARATOR = "\n\n"
#: 层内换行
LINE_SEPARATOR = "\n"

#: 各层的标题。用常量而非内联字符串，测试才能断言层序。
SECTION_HARD_CONSTRAINTS = "HARD CONSTRAINTS"
SECTION_IDENTITY = "IDENTITY"
SECTION_PERSONALITY = "PERSONALITY"
SECTION_SPEECH_STYLE = "SPEECH STYLE"
SECTION_OUTPUT_FORMAT = "OUTPUT FORMAT"

#: Layer 0 的基础约束。刻意放在代码里而不是人格 TOML：
#: 这些是安全底线，不应该通过编辑一个配置文件就被移除。
BASE_HARD_CONSTRAINTS: tuple[str, ...] = (
    "不要声称自己是生物学意义上的人类。",
    "不要编造记忆、工具执行结果或观察。",
    "不要在回复里直接说出你的内部状态数值，除非用户明确询问。",
)


def build_system_prompt(persona: Persona) -> str:
    """把人格组装成 system prompt。

    M2/M3/M6 会在 Layer 3 与 Layer 8 之间插入新层（记忆 / 状态 / 工具），
    插入点见模块文档字符串。
    """
    sections = (
        _hard_constraints(persona),
        _identity(persona),
        _personality(persona),
        _speech_style(persona),
        _output_format(),
    )
    return SECTION_SEPARATOR.join(sections)


def _hard_constraints(persona: Persona) -> str:
    lines = [SECTION_HARD_CONSTRAINTS]
    lines.extend(f"- {item}" for item in BASE_HARD_CONSTRAINTS)
    lines.extend(f"- {item}" for item in persona.hard_constraints)
    return LINE_SEPARATOR.join(lines)


def _identity(persona: Persona) -> str:
    identity = persona.identity
    lines = [
        SECTION_IDENTITY,
        f"你是 {identity.name}（{identity.display_name}），人称「{identity.pronouns}」。",
        identity.self_description,
    ]
    if identity.backstory:
        lines.append(identity.backstory)
    return LINE_SEPARATOR.join(lines)


def _personality(persona: Persona) -> str:
    lines = [SECTION_PERSONALITY, "性格倾向（0 到 1，数值越高该特质越明显）："]
    # sorted()：保证输出逐字节确定，不受 dict 插入顺序影响
    lines.extend(f"- {name}: {value:.2f}" for name, value in sorted(persona.traits.items()))

    if persona.behavior_rules:
        lines.append("行为规则：")
        lines.extend(f"- {rule}" for rule in persona.behavior_rules)

    return LINE_SEPARATOR.join(lines)


def _speech_style(persona: Persona) -> str:
    communication = persona.communication
    return LINE_SEPARATOR.join(
        [
            SECTION_SPEECH_STYLE,
            f"- 主要语言：{communication.language}",
            f"- 句子长度：{communication.sentence_length}",
            f"- 表情符号频率：{communication.emoji_frequency}",
            f"- 反问句：{communication.rhetorical_questions}",
            f"- 交互方式：{communication.preferred_interaction}",
        ]
    )


def _output_format() -> str:
    return LINE_SEPARATOR.join(
        [
            SECTION_OUTPUT_FORMAT,
            "你必须输出一个 JSON 对象，包含且仅包含以下 4 个字段。",
            "",
            "reply: 字符串。你对用户说的话。用中文，口语化、自然，不要写成列表或小标题。",
            "",
            "emotion: 对象 {primary, intensity}。",
            "  primary 必须严格从下面这张表里选一个英文词，不要用中文、不要自造词：",
            f"  {', '.join(EMOTION_CHOICES)}",
            "  intensity 是 0 到 1 之间的小数，表示强度。",
            "",
            "action: 对象 {name, intensity}。",
            "  name 必须严格从下面这张表里选一个：",
            f"  {', '.join(ACTION_CHOICES)}",
            "  intensity 是 0 到 1 之间的小数。",
            "",
            "voice: 对象 {style, speed, pitch}。",
            "  style 必须严格从下面这张表里选一个：",
            f"  {', '.join(VOICE_STYLE_CHOICES)}",
            "  speed 在 0.5 到 2.0 之间，pitch 在 -1.0 到 1.0 之间。",
        ]
    )
