"""``astra.infrastructure.lmstudio_cli`` 的单元测试。

样本取自**本机实测输出**（`lms ls --json` / `lms server status`）。解析器必须防御式：
CLI 输出会随 LM Studio 版本变，缺字段不能炸掉整个模型页。
"""

from __future__ import annotations

import json

from astra.infrastructure.lmstudio_cli import (
    LmsResult,
    parse_loaded,
    parse_models,
    parse_server_status,
)

#: 本机 `lms ls --json` 的真实结构（截掉了与本功能无关的字段）
_LS_SAMPLE = json.dumps(
    [
        {
            "type": "llm",
            "modelKey": "nvidia/nemotron-3-nano-4b",
            "displayName": "Nemotron 3 Nano 4B",
            "paramsString": "4.0B",
            "sizeBytes": 2837173509,
            "maxContextLength": 1048576,
        },
        {
            "type": "embedding",
            "modelKey": "text-embedding-nomic-embed-text-v1.5",
            "displayName": "Nomic Embed Text v1.5",
            "sizeBytes": 84106624,
            "maxContextLength": 2048,
        },
    ]
)

#: `lms ps --json` 的结构（当前本机没有加载模型，字段名按 ADR 0025 §6 记下的那些取）
_PS_SAMPLE = json.dumps(
    [
        {
            "identifier": "nvidia/nemotron-3-nano-4b",
            "modelKey": "nvidia/nemotron-3-nano-4b",
            "contextLength": 16384,
            "sizeBytes": 2837173509,
        }
    ]
)


# ── 模型清单 ────────────────────────────────────────────


def test_parse_models_reads_the_fields_we_show() -> None:
    models = parse_models(json.loads(_LS_SAMPLE))

    assert [model.key for model in models] == [
        "nvidia/nemotron-3-nano-4b",
        "text-embedding-nomic-embed-text-v1.5",
    ]
    first = models[0]
    assert first.display_name == "Nemotron 3 Nano 4B"
    assert first.model_type == "llm"
    assert first.params == "4.0B"
    assert first.max_context == 1048576


def test_parse_models_is_defensive() -> None:
    assert parse_models(None) == []
    assert parse_models({"not": "a list"}) == []
    assert parse_models([{"noKey": True}, "junk", {"modelKey": "ok"}])[0].key == "ok"


def test_parse_models_falls_back_to_display_name() -> None:
    models = parse_models([{"modelKey": "a/b"}])

    assert models[0].display_name == "a/b"


# ── 已加载 ──────────────────────────────────────────────


def test_parse_loaded_reads_identifier_and_context() -> None:
    loaded = parse_loaded(json.loads(_PS_SAMPLE))

    assert loaded[0].identifier == "nvidia/nemotron-3-nano-4b"
    assert loaded[0].context_length == 16384
    assert loaded[0].size_bytes == 2837173509


def test_parse_loaded_empty_means_nothing_loaded() -> None:
    assert parse_loaded([]) == []


def test_parse_loaded_is_defensive() -> None:
    assert parse_loaded(None) == []
    assert parse_loaded([{"contextLength": 1}]) == []  # 没有 identifier 就跳过


# ── server 状态 ─────────────────────────────────────────


def test_parse_server_status_from_the_real_english_text() -> None:
    assert parse_server_status("The server is not running.") == "stopped"
    assert parse_server_status("The server is running on port 1234.") == "running"
    assert parse_server_status("") == "unknown"


# ── JSON 抽取 ───────────────────────────────────────────


def test_last_json_handles_noise_before_the_payload() -> None:
    result = LmsResult(ok=True, code=0, stdout='booting...\n[{"modelKey": "a/b"}]', stderr="")

    assert result.last_json() == [{"modelKey": "a/b"}]


def test_last_json_returns_none_when_there_is_none() -> None:
    assert LmsResult(ok=True, code=0, stdout="no json here", stderr="").last_json() is None
    assert LmsResult(ok=True, code=0, stdout="", stderr="").last_json() is None
