"""``astra.infrastructure.persona.toml_loader`` 的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astra.domain.persona import Persona
from astra.infrastructure.persona.toml_loader import PersonaLoadError, load_persona

_REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PERSONA = _REPO_ROOT / "personas" / "astra.toml"

_MINIMAL = """
[identity]
name = "Test"
display_name = "Test"
pronouns = "它"
self_description = "  一个测试角色。  "

[communication]
sentence_length = "short"
emoji_frequency = "low"
rhetorical_questions = "rare"
preferred_interaction = "conversational"
"""


def _write(tmp_path: Path, content: str, name: str = "persona.toml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ── 随仓库分发的人格文件 ────────────────────────────────


def test_shipped_persona_loads() -> None:
    """仓库里的 personas/astra.toml 必须始终可加载——它是应用的身份。"""
    persona = load_persona(SHIPPED_PERSONA)

    assert persona.identity.name == "Astra"
    assert persona.traits
    assert persona.behavior_rules


def test_shipped_persona_traits_within_range() -> None:
    persona = load_persona(SHIPPED_PERSONA)

    assert all(0.0 <= value <= 1.0 for value in persona.traits.values())


def test_shipped_persona_provides_llm_unavailable_message() -> None:
    persona = load_persona(SHIPPED_PERSONA)

    assert "llm_unavailable" in persona.error_messages


# ── 最小合法文件 ────────────────────────────────────────


def test_minimal_file_loads(tmp_path: Path) -> None:
    persona = load_persona(_write(tmp_path, _MINIMAL))

    assert isinstance(persona, Persona)
    assert persona.identity.name == "Test"
    assert persona.identity.version == "1"  # 默认值
    assert persona.identity.backstory is None
    assert persona.traits == {}
    assert persona.behavior_rules == ()
    assert persona.communication.language == "zh-CN"  # 默认值


def test_self_description_is_stripped(tmp_path: Path) -> None:
    """TOML 多行字符串首尾会带换行，必须去掉，否则 prompt 出现空行。"""
    persona = load_persona(_write(tmp_path, _MINIMAL))

    assert persona.identity.self_description == "一个测试角色。"


# ── 失败路径 ────────────────────────────────────────────


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PersonaLoadError, match="not readable"):
        load_persona(tmp_path / "nope.toml")


def test_directory_instead_of_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PersonaLoadError, match="not readable"):
        load_persona(tmp_path)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    with pytest.raises(PersonaLoadError, match="not valid TOML"):
        load_persona(_write(tmp_path, "this is not = = toml"))


def test_missing_identity_raises(tmp_path: Path) -> None:
    content = """
[communication]
sentence_length = "short"
emoji_frequency = "low"
rhetorical_questions = "rare"
preferred_interaction = "conversational"
"""
    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, content))


def test_missing_communication_raises(tmp_path: Path) -> None:
    content = """
[identity]
name = "Test"
display_name = "Test"
pronouns = "它"
self_description = "x"
"""
    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, content))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """`extra="forbid"`：拼错的键必须报错，否则"改了配置没生效"会静默发生。"""
    content = _MINIMAL.replace('pronouns = "它"', 'pronouns = "它"\nnam = "typo"')

    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, content))


def test_unknown_top_level_section_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, _MINIMAL + "\n[typo_section]\nx = 1\n"))


@pytest.mark.parametrize("value", ["-0.01", "1.01", "5"])
def test_trait_out_of_range_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, _MINIMAL + f"\n[traits]\ncurious = {value}\n"))


def test_non_numeric_trait_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PersonaLoadError, match="failed validation"):
        load_persona(_write(tmp_path, _MINIMAL + '\n[traits]\ncurious = "high"\n'))


# ── error_message 回落 ──────────────────────────────────


def test_error_message_falls_back_to_default(tmp_path: Path) -> None:
    persona = load_persona(_write(tmp_path, _MINIMAL))

    assert persona.error_message("llm_unavailable", default="fallback") == "fallback"


def test_error_message_uses_override(tmp_path: Path) -> None:
    content = _MINIMAL + '\n[errors]\nllm_unavailable = "自定义文案"\n'
    persona = load_persona(_write(tmp_path, content))

    assert persona.error_message("llm_unavailable", default="fallback") == "自定义文案"
