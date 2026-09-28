"""``astra.settings.env_check`` 的单元测试（会真起一次子进程）。

这里验的是**整条链**：候选文件 → 子进程里实例化 ``Settings`` → JSON 结论。
最后两条用真实消费方（pydantic-settings + python-dotenv）验证引号写法能精确还原——
写坏一个 Windows 路径在真机上很难发现，在测试里是一行断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astra.infrastructure.dotenv_store import render
from astra.settings import Settings
from astra.settings.env_check import check_env_file

_MINIMAL = "ASTRA_LLM_PROVIDER=none\nASTRA_LLM_MODEL=qwen/qwen3.5-9b\n"


def test_valid_file_passes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(_MINIMAL, encoding="utf-8")

    result = check_env_file(env_file)

    assert result.ok is True
    assert result.message == ""


def test_invalid_value_is_reported_with_a_readable_message(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(_MINIMAL + "ASTRA_LLM_TEMPERATURE=abc\n", encoding="utf-8")

    result = check_env_file(env_file)

    assert result.ok is False
    assert "llm_temperature" in result.message


def test_cross_field_constraint_is_caught(tmp_path: Path) -> None:
    """五条检索权重之和必须为 1.0——单看某一项是合法的，只有整份文件一起才验得出。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ASTRA_MEMORY_WEIGHT_SEMANTIC=0.9\n"
        "ASTRA_MEMORY_WEIGHT_IMPORTANCE=0.9\n"
        "ASTRA_MEMORY_WEIGHT_RECENCY=0.15\n"
        "ASTRA_MEMORY_WEIGHT_ACCESS=0.05\n"
        "ASTRA_MEMORY_WEIGHT_RELATIONSHIP=0.10\n",
        encoding="utf-8",
    )

    result = check_env_file(env_file)

    assert result.ok is False


def test_unknown_key_is_caught(tmp_path: Path) -> None:
    """``extra="forbid"``：多一个键 = 启动直接失败，写配置时必须挡住。"""
    env_file = tmp_path / ".env"
    env_file.write_text(_MINIMAL + "ASTRA_TYPO_KEY=1\n", encoding="utf-8")

    result = check_env_file(env_file)

    assert result.ok is False


def test_missing_file_still_validates_the_defaults(tmp_path: Path) -> None:
    result = check_env_file(tmp_path / "does-not-exist.env")

    assert result.ok is True


def test_windows_path_round_trips_through_the_real_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带空格与 ``\\t``／``\\r`` 的路径：单引号写法必须能被 python-dotenv 精确还原。"""
    monkeypatch.delenv("ASTRA_TTS_REFERENCE_AUDIO", raising=False)
    raw = r"E:\My Files\temp\ref.wav"
    env_file = tmp_path / ".env"
    env_file.write_text(render("", {"ASTRA_TTS_REFERENCE_AUDIO": raw}), encoding="utf-8")

    assert check_env_file(env_file).ok is True
    # 注意：ASTRA_TTS_REFERENCE_AUDIO 是 Path 字段，pydantic 会归一成 WindowsPath——
    # 要点是"它不是被 \\t 解成制表符"，而不是字面量完全一致
    assert Settings(_env_file=env_file).tts_reference_audio == Path(raw)
