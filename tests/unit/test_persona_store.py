"""``PersonaStore`` 的单元测试（ADR 0029）。

覆盖目录扫描、id 校验、备份与"坏文件不能拖垮整个列表"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astra.infrastructure.persona.store import (
    InvalidPersonaIdError,
    PersonaStore,
    validate_persona_id,
)
from astra.infrastructure.persona.toml_loader import (
    PersonaDocument,
    PersonaLoadError,
    validate_document,
)


def _document(display_name: str = "Luna") -> PersonaDocument:
    return validate_document(
        {
            "identity": {
                "name": "Luna",
                "display_name": display_name,
                "pronouns": "她",
                "self_description": "一个测试角色。",
            },
            "communication": {
                "sentence_length": "short",
                "emoji_frequency": "low",
                "rhetorical_questions": "rare",
                "preferred_interaction": "conversational",
            },
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> PersonaStore:
    return PersonaStore(tmp_path / "personas")


# ── id ──────────────────────────────────────────────────


@pytest.mark.parametrize("persona_id", ["Astra", "1abc", "", "a" * 33, "a b", "a/b", "中文", "a.b"])
def test_invalid_ids_are_rejected(store: PersonaStore, persona_id: str) -> None:
    with pytest.raises(InvalidPersonaIdError, match="persona id must match"):
        store.path_for(persona_id)


@pytest.mark.parametrize("persona_id", ["astra", "luna-2", "my_companion", "a"])
def test_valid_ids_are_accepted(persona_id: str) -> None:
    assert validate_persona_id(persona_id) == persona_id


def test_path_for_uses_the_persona_directory(store: PersonaStore, tmp_path: Path) -> None:
    assert store.path_for("luna") == tmp_path / "personas" / "luna.toml"


# ── 读写 ────────────────────────────────────────────────


def test_create_writes_a_readable_file(store: PersonaStore) -> None:
    path = store.create("luna", _document())

    assert path.is_file()
    assert store.exists("luna")
    assert store.read("luna").model_dump() == _document().model_dump()


def test_create_creates_the_directory_when_missing(store: PersonaStore) -> None:
    assert not store.directory.exists()

    store.create("luna", _document())

    assert store.directory.is_dir()


def test_create_refuses_an_existing_id(store: PersonaStore) -> None:
    store.create("luna", _document())

    with pytest.raises(FileExistsError, match="already exists"):
        store.create("luna", _document())


def test_write_backs_up_the_previous_file(store: PersonaStore) -> None:
    path = store.create("luna", _document("旧名字"))

    store.write("luna", _document("新名字"))

    assert store.read("luna").identity.display_name == "新名字"
    backup = path.with_name("luna.toml.bak")
    assert backup.is_file()
    assert "旧名字" in backup.read_text(encoding="utf-8")


def test_write_creates_when_missing(store: PersonaStore) -> None:
    store.write("luna", _document())

    assert store.exists("luna")


def test_read_missing_file_raises(store: PersonaStore) -> None:
    with pytest.raises(PersonaLoadError, match="not readable"):
        store.read("nope")


# ── 列表 ────────────────────────────────────────────────


def test_list_is_empty_when_the_directory_is_missing(store: PersonaStore) -> None:
    assert store.list() == ()


def test_list_is_sorted_and_carries_display_names(store: PersonaStore) -> None:
    store.create("luna", _document("Luna"))
    store.create("astra", _document("Astra"))

    entries = store.list()

    assert [entry.id for entry in entries] == ["astra", "luna"]
    assert [entry.display_name for entry in entries] == ["Astra", "Luna"]
    assert all(entry.error is None for entry in entries)


def test_list_reports_broken_files_instead_of_failing(store: PersonaStore) -> None:
    """一个坏文件不该让整个人格列表打不开——如实标出是哪一份、坏在哪。"""
    store.create("luna", _document())
    (store.directory / "broken.toml").write_text("this is not = = toml", encoding="utf-8")

    entries = store.list()

    assert [entry.id for entry in entries] == ["broken", "luna"]
    broken = entries[0]
    assert broken.display_name is None
    assert broken.error is not None
    assert "not valid TOML" in broken.error


def test_list_reports_invalid_ids(store: PersonaStore) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "BadName.toml").write_text("", encoding="utf-8")

    entries = store.list()

    assert len(entries) == 1
    assert entries[0].id == "BadName"
    assert entries[0].error is not None
    assert "persona id must match" in entries[0].error


def test_list_ignores_backups(store: PersonaStore) -> None:
    store.create("luna", _document())
    store.write("luna", _document("Luna 2"))

    entries = store.list()

    assert [entry.id for entry in entries] == ["luna"]


# ── 删除 ────────────────────────────────────────────────


def test_delete_removes_the_file(store: PersonaStore) -> None:
    store.create("luna", _document())

    assert store.delete("luna") is True

    assert not store.exists("luna")
    assert store.list() == ()


def test_delete_missing_returns_false(store: PersonaStore) -> None:
    assert store.delete("luna") is False


def test_delete_keeps_the_backup(store: PersonaStore) -> None:
    """删除只删人格文件；``.bak`` 是用户自己的飞线，不动它。"""
    path = store.create("luna", _document())
    store.write("luna", _document("Luna 2"))

    store.delete("luna")

    assert not path.exists()
    assert path.with_name("luna.toml.bak").is_file()
