"""代码侧契约与 ``docs/schemas/`` 的对齐检查（ADR 0007）。

ADR 0007 要求 M1 建立这个机制。**不用 ``jsonschema``**——为一个测试加依赖不值，
结构化比对已经足够。

比对三件事：

1. 代码模型发送的 profile 是文档契约的**子集**（M1 只发 4 字段，ADR 0011）
2. 嵌套对象的字段名集合与文档一致
3. **数值范围**：JSON Schema 里写了 ``minimum``/``maximum``，但实测 ``strict: true``
   并不强制它们，所以真正的强制点在 ``domain/response.py``。这里直接断言
   「文档写的范围 == 领域常量」，把文档契约仍然验证到位。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from astra.application.assistant_schema import (
    NESTED_PAYLOAD_MODELS,
    AssistantResponsePayload,
)
from astra.domain.response import INTENSITY_RANGE, PITCH_RANGE, SPEED_RANGE

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_SCHEMA_PATH = _REPO_ROOT / "docs" / "schemas" / "assistant_response.schema.json"

#: M1 实际发送的字段（ADR 0011）
M1_FIELDS = frozenset({"reply", "emotion", "action", "voice"})

#: 文档里定义了但 M1 不发送的字段，按里程碑标注
DEFERRED_FIELDS = {
    "memory_candidates": "M2",
    "state_delta": "M3",
    "tool_calls": "M6",
}


def _docs_schema() -> dict[str, Any]:
    return json.loads(_DOCS_SCHEMA_PATH.read_text(encoding="utf-8"))


# ── 文件本身 ────────────────────────────────────────────


def test_docs_schema_exists_and_is_valid_json() -> None:
    schema = _docs_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_docs_schema_still_defines_the_full_contract() -> None:
    """文档保留 7 字段的完整定义——它是对外契约描述，不应因 M1 裁剪而缩水。"""
    properties = set(_docs_schema()["properties"])

    assert M1_FIELDS | set(DEFERRED_FIELDS) == properties


# ── 代码 profile ↔ 文档契约 ─────────────────────────────


def test_m1_profile_is_exactly_the_four_declared_fields() -> None:
    assert set(AssistantResponsePayload.model_fields) == M1_FIELDS


def test_m1_profile_is_a_subset_of_documented_contract() -> None:
    code = AssistantResponsePayload.model_json_schema()
    docs = _docs_schema()

    assert set(code["properties"]) <= set(docs["properties"])
    assert set(code["required"]) <= set(docs["required"])


@pytest.mark.parametrize("field", sorted(DEFERRED_FIELDS))
def test_deferred_fields_are_not_sent_in_m1(field: str) -> None:
    code = AssistantResponsePayload.model_json_schema()

    assert field not in code["properties"]


def test_nested_field_names_match_documented_contract() -> None:
    docs = _docs_schema()["properties"]

    for name, model in NESTED_PAYLOAD_MODELS.items():
        assert set(model.model_fields) == set(docs[name]["properties"]), name


def test_nested_objects_are_closed_in_both_definitions() -> None:
    code = AssistantResponsePayload.model_json_schema()
    docs = _docs_schema()["properties"]

    for name in NESTED_PAYLOAD_MODELS:
        assert docs[name]["additionalProperties"] is False
        assert code["$defs"][f"{name.capitalize()}Payload"]["additionalProperties"] is False


# ── 数值范围：文档 ↔ 领域常量 ───────────────────────────


@pytest.mark.parametrize(
    ("path", "bounds"),
    [
        (("emotion", "intensity"), INTENSITY_RANGE),
        (("action", "intensity"), INTENSITY_RANGE),
        (("voice", "speed"), SPEED_RANGE),
        (("voice", "pitch"), PITCH_RANGE),
    ],
)
def test_documented_ranges_match_domain_constants(
    path: tuple[str, str], bounds: tuple[float, float]
) -> None:
    """文档里的 minimum/maximum 必须与代码里的范围常量一致。

    JSON Schema 的数值边界**不会**被 strict 模式强制（实测 `pitch=200` 通过），
    所以真正的执行点在 `domain/response.py`。这条断言保证两者不漂移。
    """
    section: dict[str, Any] = _docs_schema()["properties"]
    for key in path[:-1]:
        section = section[key]["properties"]

    field = section[path[-1]]

    assert (field["minimum"], field["maximum"]) == bounds
