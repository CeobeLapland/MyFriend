"""``.env`` 的最小读写：**只改值、不增删键、保留注释与顺序**（ADR 0026 §3）。

不用 python-dotenv 的 ``set_key``：它会重排并丢掉注释，而这个文件是用户会手看的
单一真相（ADR 0025 §2）。这里需要的语义很窄——替换已存在键的值、必要时在末尾追加。

**引号规则照 python-dotenv 的真实解析行为定**（读过它的 ``parser.py``）：

- 不带引号的值会被 ``re.sub(r"\\s+#.*", "")`` 砍掉尾部的行内注释；
- **双引号会解码** ``\\\\ \\' \\" \\a \\b \\f \\n \\r \\t \\v``——于是
  ``"E:\\temp\\a.wav"`` 里的 ``\\t`` 会变成制表符（Windows 路径的经典坑）；
- 单引号**只解码** ``\\\\`` 与 ``\\'``。

所以这里只用单引号，并把 ``\\`` 与 ``'`` 转义出去——两者都能精确还原。
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Mapping

#: 匹配 ``KEY=value``（允许前导空白与 ``export `` 前缀；注释行天然不匹配）
_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[^\S\r\n]*(?:export[^\S\r\n]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"[^\S\r\n]*=(?P<value>.*)$"
)

_DOUBLE_ESCAPES = re.compile(r"\\[\\'\"abfnrtv]")
_SINGLE_ESCAPES = re.compile(r"\\[\\']")

#: 需要加引号的字符（空白 / 注释符 / 引号）
_NEEDS_QUOTES = re.compile(r"[\s#'\"]")

_APPEND_COMMENT = "# 由 Studio 追加（原文件里没有这些键）"


def parse_values(text: str) -> dict[str, str]:
    """取出文件里的 ``KEY=value``。注释行不会被当成赋值（``#`` 不匹配键模式）。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        values[match.group("key")] = _decode_value(match.group("value"))
    return values


def render(text: str, values: Mapping[str, str]) -> str:
    """把 ``values`` 落到 ``text`` 上：已存在的键改值，其余追加到末尾。

    换行风格（LF / CRLF）按原文件来——不因为改一个值就把整个文件的行尾换掉。
    """
    eol = _detect_eol(text)
    remaining = dict(values)
    out: list[str] = []

    for raw in text.splitlines(keepends=True):
        body, ending = _split_ending(raw)
        match = _ASSIGNMENT.match(body)
        if match is not None and match.group("key") in remaining:
            key = match.group("key")
            encoded = _encode_value(remaining.pop(key))
            out.append(f"{match.group('prefix')}{key}={encoded}{ending}")
        else:
            out.append(raw)

    if remaining:
        if out and out[-1].strip():
            out.append(eol)
        out.append(f"{_APPEND_COMMENT}{eol}")
        for key in sorted(remaining):
            out.append(f"{key}={_encode_value(remaining[key])}{eol}")

    joined = "".join(out)
    if not joined.endswith(("\n", "\r")):
        joined += eol
    return joined


def _split_ending(raw: str) -> tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if raw.endswith(ending):
            return raw[: -len(ending)], ending
    return raw, ""


def _detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _decode_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return _SINGLE_ESCAPES.sub(_decode_match, value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return _DOUBLE_ESCAPES.sub(_decode_match, value[1:-1])
    return re.sub(r"\s+#.*", "", value).rstrip()


def _decode_match(match: re.Match[str]) -> str:
    return codecs.decode(match.group(0), "unicode-escape")


def _encode_value(value: str) -> str:
    if value == "":
        return ""
    if "\n" in value or "\r" in value:
        # 值里有换行会直接写坏文件（等于插进一个新键）——宁可直接拒绝
        raise ValueError("值里不能有换行")
    if _NEEDS_QUOTES.search(value):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return value
