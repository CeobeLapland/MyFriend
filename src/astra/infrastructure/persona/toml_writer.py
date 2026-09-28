"""把人格文档序列化成 TOML（ADR 0029）。

stdlib 只有 ``tomllib``（只读），所以这里手写一份**固定 schema** 的序列化——
结构就是 :class:`~astra.infrastructure.persona.toml_loader.PersonaDocument`，
不需要通用 TOML writer。

输出是**规范形态**：字段顺序固定、多行文本用三引号、数组一行一条。
**它不保留原文件的注释**——这是 ADR 0029 记下的取舍（写前有 ``<id>.toml.bak``，
Studio 页面上也明说）。写出来的文件必须能被 :func:`load_persona` 原样读回。
"""

from __future__ import annotations

import re

from astra.infrastructure.persona.toml_loader import PersonaDocument

_HEADER = """\
# 这份文件由 Astra Studio 的「人格」页保存生成（ADR 0029）。
# 字段含义见 docs/05_PERSONA_AND_PROMPTS.md。
# 注意：UI 保存会重写整份文件、不保留注释；原文件留在同目录的 .bak 里。"""

#: TOML 裸键允许的字符——不满足就用带引号的键（永不出错）
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def render_persona(document: PersonaDocument) -> str:
    """把一份人格文档渲染成 TOML 文本（以换行结尾）。"""
    lines: list[str] = [_HEADER, ""]

    identity = document.identity
    lines.append("[identity]")
    lines.append(f"name = {_quote(identity.name)}")
    lines.append(f"display_name = {_quote(identity.display_name)}")
    lines.append(f"version = {_quote(identity.version)}")
    lines.append(f"pronouns = {_quote(identity.pronouns)}")
    lines.append(_multiline_field("self_description", identity.self_description))
    if identity.backstory:
        lines.append(_multiline_field("backstory", identity.backstory))
    lines.append("")

    lines.append("[traits]")
    for name, value in document.traits.items():
        lines.append(f"{_key(name)} = {value!r}")
    lines.append("")

    communication = document.communication
    lines.append("[communication]")
    lines.append(f"language = {_quote(communication.language)}")
    lines.append(f"sentence_length = {_quote(communication.sentence_length)}")
    lines.append(f"emoji_frequency = {_quote(communication.emoji_frequency)}")
    lines.append(f"rhetorical_questions = {_quote(communication.rhetorical_questions)}")
    lines.append(f"preferred_interaction = {_quote(communication.preferred_interaction)}")
    lines.append("")

    lines.append("[behavior]")
    lines.append(_array_field("rules", document.behavior.rules))
    lines.append("")

    lines.append("[prompt]")
    lines.append(_array_field("hard_constraints", document.prompt.hard_constraints))
    lines.append("")

    if document.errors.llm_unavailable is not None:
        lines.append("[errors]")
        lines.append(f"llm_unavailable = {_quote(document.errors.llm_unavailable)}")
        lines.append("")

    return "\n".join(lines)


def _quote(value: str) -> str:
    """单行基本字符串：转义反斜杠、引号与控制字符。"""
    out: list[str] = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key(name: str) -> str:
    return name if _BARE_KEY_RE.match(name) else _quote(name)


def _multiline_field(key: str, value: str) -> str:
    """多行文本用三引号渲染；有反斜杠 / 三引号 / 结尾引号时退回转义的单行。

    TOML 规定三引号开引号**紧随的第一个换行会被吃掉**，所以这里刻意先写一个换行、
    再放正文——正文本身一个字符都不会变。
    """
    if _safe_multiline(value):
        return f'{key} = """\n{value}"""'
    return f"{key} = {_quote(value)}"


def _safe_multiline(value: str) -> bool:
    if '"""' in value or value.endswith('"') or "\\" in value:
        return False
    return all(char in "\n\t" or ord(char) >= 0x20 for char in value)


def _array_field(key: str, values: list[str]) -> str:
    if not values:
        return f"{key} = []"
    lines = [f"{key} = ["]
    lines.extend(f"  {_quote(item)}," for item in values)
    lines.append("]")
    return "\n".join(lines)
