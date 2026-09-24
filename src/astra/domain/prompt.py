"""System prompt 的分层组装（`docs/05_PERSONA_AND_PROMPTS.md` §1）。

纯函数：同一组输入必须产出**逐字节相同**的字符串（`docs/10` §2 要求确定性）。

层序与当前状态：

======  ==========================  ==========================================
层      内容                        状态
======  ==========================  ==========================================
0       Safety / hard constraints   实现
1       Identity                    实现
2       Stable personality          实现（traits + behavior rules）
3       Speech style                实现
4       Current state               **未实现** —— M3 在 Layer 3 之后插入
5       Relevant memories           **M2 实现**（在 Layer 3 之后插入）
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

from collections.abc import Sequence
from datetime import datetime

from astra.domain.memory import Memory
from astra.domain.persona import Persona
from astra.domain.vocabulary import (
    ACTION_CHOICES,
    EMOTION_CHOICES,
    MEMORY_TYPE_CHOICES,
    VOICE_STYLE_CHOICES,
)

#: 层与层之间
SECTION_SEPARATOR = "\n\n"
#: 层内换行
LINE_SEPARATOR = "\n"

#: 各层的标题。用常量而非内联字符串，测试才能断言层序。
SECTION_HARD_CONSTRAINTS = "HARD CONSTRAINTS"
SECTION_IDENTITY = "IDENTITY"
SECTION_PERSONALITY = "PERSONALITY"
SECTION_SPEECH_STYLE = "SPEECH STYLE"
SECTION_RELEVANT_MEMORIES = "RELEVANT MEMORIES"
SECTION_OUTPUT_FORMAT = "OUTPUT FORMAT"

#: Layer 0 的基础约束。刻意放在代码里而不是人格 TOML：
#: 这些是安全底线，不应该通过编辑一个配置文件就被移除。
BASE_HARD_CONSTRAINTS: tuple[str, ...] = (
    "不要声称自己是生物学意义上的人类。",
    "不要编造记忆、工具执行结果或观察。",
    "不要在回复里直接说出你的内部状态数值，除非用户明确询问。",
)

#: 每条记忆的渲染格式（`docs/05` §5）。低 confidence 的记忆不允许写成确定事实。
_MEMORY_LABEL = "[MEMORY]"


def build_system_prompt(
    persona: Persona,
    *,
    memories: Sequence[Memory] = (),
    memory_max_chars: int | None = None,
) -> str:
    """把人格（可选：检索到的记忆）组装成 system prompt。

    Args:
        persona: 人格配置。
        memories: 本轮检索到的记忆，**调用方负责排好序**（``MemoryService`` 按
            `docs/06` §4 的总分降序、同分按 id 升序），这样同一组记忆的输出
            逐字节确定。
        memory_max_chars: 记忆层的字符预算；超出时从**末尾**（分最低的那条）
            开始丢弃。``None`` 表示不限制。

    M3/M6 会在 Layer 3 与 Layer 8 之间继续插入新层（状态 / 工具），插入点见
    模块文档字符串。
    """
    sections = [
        _hard_constraints(persona),
        _identity(persona),
        _personality(persona),
        _speech_style(persona),
    ]

    memory_section = _relevant_memories(memories, max_chars=memory_max_chars)
    if memory_section is not None:
        sections.append(memory_section)

    sections.append(_output_format())
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


def _relevant_memories(memories: Sequence[Memory], *, max_chars: int | None) -> str | None:
    """Layer 5：检索到的记忆。空则返回 ``None``（整层省略）。

    ``docs/05`` §5 要求逐条给出 content / importance / confidence / last_accessed，
    并明文规定"confidence 低时不允许写成确定事实"——所以数值必须给模型看，
    而不是只给正文让它猜可信度。
    """
    if not memories:
        return None

    lines = [SECTION_RELEVANT_MEMORIES]
    used = len(SECTION_RELEVANT_MEMORIES)

    for memory in memories:
        block = _render_memory(memory)
        block_chars = sum(len(line) for line in block)
        if max_chars is not None and lines[1:] and used + block_chars > max_chars:
            # 预算只约束记忆层本身；第一条永远保留，否则等于白检索
            break
        lines.extend(block)
        used += block_chars

    return LINE_SEPARATOR.join(lines)


def _render_memory(memory: Memory) -> list[str]:
    lines = [
        _MEMORY_LABEL,
        f"content: {memory.content}",
        f"importance: {memory.importance:.2f}",
        f"confidence: {memory.confidence:.2f}",
    ]
    if memory.last_accessed_at is not None:
        lines.append(f"last_accessed: {_as_date(memory.last_accessed_at)}")
    return lines


def _as_date(value: datetime) -> str:
    return value.date().isoformat()


def _output_format() -> str:
    return LINE_SEPARATOR.join(
        [
            SECTION_OUTPUT_FORMAT,
            "你必须输出一个 JSON 对象，包含且仅包含以下 5 个字段。",
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
            "",
            "memory_candidates: 数组，元素是对象 {type, content}。",
            "  这里放**值得长期记住**的信息，由系统负责打分与去重，你不需要判断权重。",
            "  值得放：稳定偏好、长期目标、重要关系事实、共同完成的关键事件、",
            "          用户明确要求你记住的内容。",
            "  不要放：一次性的寒暄、临时数字（如「今天几号」）、明显的玩笑、",
            "          你自己推测而用户没说过的事实。",
            "  content 用第三人称陈述句，简洁完整，例如「用户喜欢深夜写代码」。",
            "  type 必须严格从下面这张表里选一个：",
            f"  {', '.join(MEMORY_TYPE_CHOICES)}",
            "  **大多数轮次没有值得记住的内容，此时给空数组 []**——不要为了填字段而硬编。",
        ]
    )
