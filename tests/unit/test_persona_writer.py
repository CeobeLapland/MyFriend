"""``toml_writer`` 的单元测试（ADR 0029）。

核心不变量是 **round-trip**：写出来的文件必须能被 ``read_document`` 原样读回。
人格文件是用户资产，序列化出一点偏差就是静默改配置。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astra.infrastructure.persona.toml_loader import (
    PersonaDocument,
    read_document,
    validate_document,
)
from astra.infrastructure.persona.toml_writer import render_persona

_REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PERSONA = _REPO_ROOT / "personas" / "astra.toml"


def _minimal(**overrides: object) -> PersonaDocument:
    data: dict[str, object] = {
        "identity": {
            "name": "Test",
            "display_name": "Test",
            "pronouns": "它",
            "self_description": "一个测试角色。",
        },
        "communication": {
            "sentence_length": "short",
            "emoji_frequency": "low",
            "rhetorical_questions": "rare",
            "preferred_interaction": "conversational",
        },
    }
    data.update(overrides)
    return validate_document(data)


def _round_trip(document: PersonaDocument, tmp_path: Path) -> PersonaDocument:
    path = tmp_path / "roundtrip.toml"
    path.write_text(render_persona(document), encoding="utf-8")
    return read_document(path)


def test_shipped_persona_round_trips(tmp_path: Path) -> None:
    """随仓库分发的 astra.toml 必须能原样往返——它是应用的身份。"""
    document = read_document(SHIPPED_PERSONA)

    reloaded = _round_trip(document, tmp_path)

    assert reloaded.model_dump() == document.model_dump()


def test_minimal_document_round_trips(tmp_path: Path) -> None:
    document = _minimal()

    reloaded = _round_trip(document, tmp_path)

    assert reloaded.model_dump() == document.model_dump()
    assert reloaded.identity.version == "1"
    assert reloaded.identity.backstory is None
    assert reloaded.traits == {}


def test_multiline_text_round_trips(tmp_path: Path) -> None:
    """三引号渲染：正文里有换行、结尾还有换行时都不能变形。"""
    document = _minimal(
        identity={
            "name": "Test",
            "display_name": "Test",
            "pronouns": "它",
            "self_description": "第一行\n第二行\n",
            "backstory": "第一行\n第二行",  # 结尾没有换行
        }
    )

    reloaded = _round_trip(document, tmp_path)

    assert reloaded.model_dump() == document.model_dump()


@pytest.mark.parametrize(
    "tricky",
    [
        '他说 "你好"',
        '以引号结尾"',
        "有反斜杠 \\ 的值",
        '三个引号 """ 在里面',
        "制表符\t在这里",
        "emoji 🌙 与中文",
        "回车\r换行\n混用",
        "",
    ],
)
def test_tricky_strings_round_trip(tmp_path: Path, tricky: str) -> None:
    """任何字符串都得能安全写出去——危险形态退回转义的单行。"""
    document = _minimal(
        behavior={"rules": [tricky]},
        errors={"llm_unavailable": tricky},
    )

    reloaded = _round_trip(document, tmp_path)

    assert reloaded.model_dump() == document.model_dump()


def test_trait_keys_round_trip_even_when_not_bare(tmp_path: Path) -> None:
    document = _minimal(traits={"curious": 0.88, "好奇 度": 0.5, "key.with.dot": 1.0})

    reloaded = _round_trip(document, tmp_path)

    assert reloaded.traits == document.traits


def test_empty_collections_are_written_as_empty_arrays(tmp_path: Path) -> None:
    text = render_persona(_minimal(behavior={"rules": []}, prompt={"hard_constraints": []}))

    assert "rules = []" in text
    assert "hard_constraints = []" in text
    assert "[traits]" in text


def test_output_is_utf8_text_ending_with_newline(tmp_path: Path) -> None:
    text = render_persona(_minimal())

    assert text.endswith("\n")
    assert text.startswith("#")  # 标准头注释：说明这份文件由 Studio 生成
    assert "注释" in text
