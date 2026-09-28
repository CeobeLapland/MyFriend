"""``astra.infrastructure.dotenv_store`` 的单元测试。

这个模块的错法很贵：**写坏 ``.env`` = 后端下次启动直接失败**（``extra="forbid"``），
所以"注释与顺序保留""只改值""引号能精确还原"三条都要有测试守着。
"""

from __future__ import annotations

import pytest

from astra.infrastructure.dotenv_store import parse_values, render

_SAMPLE = """# 头部注释：不要动我
ASTRA_ENV=dev

# ── LLM ────────────────────────────────────────────────
ASTRA_LLM_PROVIDER=none
ASTRA_LLM_MODEL=qwen/qwen3.5-9b

# 预留（注释状态，不能被当成赋值）
# ASTRA_TIMEZONE=Asia/Shanghai
"""


# ── 解析 ────────────────────────────────────────────────


def test_parse_skips_comments_and_blank_lines() -> None:
    values = parse_values(_SAMPLE)

    assert values == {
        "ASTRA_ENV": "dev",
        "ASTRA_LLM_PROVIDER": "none",
        "ASTRA_LLM_MODEL": "qwen/qwen3.5-9b",
    }


def test_parse_handles_quotes_and_inline_comments() -> None:
    text = "A=plain\nB='has space'\nC=\"double\"\nD=value # 行内注释\nE=\n"

    assert parse_values(text) == {
        "A": "plain",
        "B": "has space",
        "C": "double",
        "D": "value",
        "E": "",
    }


def test_parse_export_prefix() -> None:
    assert parse_values("export ASTRA_ENV=prod\n") == {"ASTRA_ENV": "prod"}


# ── 改写 ────────────────────────────────────────────────


def test_render_changes_only_the_value() -> None:
    rendered = render(_SAMPLE, {"ASTRA_LLM_MODEL": "nvidia/nemotron-3-nano-4b"})

    assert "ASTRA_LLM_MODEL=nvidia/nemotron-3-nano-4b" in rendered
    # 注释、顺序、空行原样
    assert "# 头部注释：不要动我" in rendered
    assert "# 预留（注释状态，不能被当成赋值）" in rendered
    assert rendered.replace("nvidia/nemotron-3-nano-4b", "qwen/qwen3.5-9b") == _SAMPLE


def test_render_appends_keys_that_are_missing() -> None:
    rendered = render("ASTRA_ENV=dev\n", {"ASTRA_LOG_LEVEL": "DEBUG"})

    assert rendered.startswith("ASTRA_ENV=dev\n")
    assert "# 由 Studio 追加" in rendered
    assert "ASTRA_LOG_LEVEL=DEBUG" in rendered
    assert parse_values(rendered) == {"ASTRA_ENV": "dev", "ASTRA_LOG_LEVEL": "DEBUG"}


def test_render_keeps_crlf_endings() -> None:
    rendered = render("ASTRA_ENV=dev\r\nASTRA_LOG_LEVEL=INFO\r\n", {"ASTRA_LOG_LEVEL": "DEBUG"})

    assert rendered == "ASTRA_ENV=dev\r\nASTRA_LOG_LEVEL=DEBUG\r\n"


def test_render_quotes_values_that_need_it() -> None:
    rendered = render("", {"ASTRA_TTS_REFERENCE_AUDIO": r"E:\My Files\ref.wav"})

    # 单引号：python-dotenv 对双引号会解码 \t / \r 这类转义（Windows 路径的坑）
    assert "ASTRA_TTS_REFERENCE_AUDIO='E:\\\\My Files\\\\ref.wav'" in rendered
    assert parse_values(rendered)["ASTRA_TTS_REFERENCE_AUDIO"] == r"E:\My Files\ref.wav"


def test_render_rejects_newlines_in_values() -> None:
    with pytest.raises(ValueError, match="换行"):
        render("", {"ASTRA_LOG_LEVEL": "DEBUG\nASTRA_ENV=prod"})


def test_render_is_idempotent() -> None:
    once = render(_SAMPLE, {"ASTRA_ENV": "prod"})
    twice = render(once, {"ASTRA_ENV": "prod"})

    assert once == twice
