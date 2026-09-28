"""``SQLiteConversationRepository`` 的单元测试。

用真实临时 SQLite 文件（不用 ``:memory:``——每连接一个独立库，与"每工作单元
一连接"的设计冲突）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.domain.conversation import Conversation, ConversationStatus, Message, Role
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.migrations import upgrade

_BASE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteConversationRepository:
    db_path = tmp_path / "astra.db"
    upgrade(db_path)
    return SQLiteConversationRepository(db_path)


def _conversation(
    conversation_id: str = "01CONV",
    *,
    status: ConversationStatus = ConversationStatus.ACTIVE,
    started_at: datetime | None = None,
    user_id: str = "local",
) -> Conversation:
    return Conversation(
        id=conversation_id,
        user_id=user_id,
        companion_id="astra",
        started_at=started_at or _BASE,
        status=status,
    )


def _message(
    message_id: str,
    *,
    conversation_id: str = "01CONV",
    turn_id: str = "01TURN",
    role: Role = Role.USER,
    content: str = "你好",
    created_at: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> Message:
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        role=role,
        content=content,
        created_at=created_at or _BASE,
        metadata=metadata or {},
    )


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


# ── 会话 ────────────────────────────────────────────────


def test_create_and_get_roundtrip(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))

    loaded = _run(repository.get("01CONV"))

    assert loaded == _conversation()


def test_get_missing_returns_none(repository: SQLiteConversationRepository) -> None:
    assert _run(repository.get("nope")) is None


def test_ended_conversation_is_preserved(repository: SQLiteConversationRepository) -> None:
    conversation = _conversation(status=ConversationStatus.ENDED, started_at=_BASE)
    ended = _conversation(status=ConversationStatus.ENDED)
    object.__setattr__(ended, "ended_at", _BASE + timedelta(hours=1))
    _run(repository.create(conversation))

    loaded = _run(repository.get("01CONV"))

    assert loaded is not None
    assert loaded.status is ConversationStatus.ENDED
    assert ended.ended_at is not None


def test_find_active_returns_none_when_empty(repository: SQLiteConversationRepository) -> None:
    assert _run(repository.find_active(user_id="local", companion_id="astra")) is None


def test_find_active_ignores_ended(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation("01A", status=ConversationStatus.ENDED)))

    assert _run(repository.find_active(user_id="local", companion_id="astra")) is None


def test_find_active_returns_newest_by_id(repository: SQLiteConversationRepository) -> None:
    """排序用 id 而不是 started_at——时钟分辨率不足（ADR 0012）。"""
    _run(repository.create(_conversation("01A")))
    _run(repository.create(_conversation("01B")))

    found = _run(repository.find_active(user_id="local", companion_id="astra"))

    assert found is not None
    assert found.id == "01B"


def test_find_active_scopes_by_user_and_companion(
    repository: SQLiteConversationRepository,
) -> None:
    _run(repository.create(_conversation("01A", user_id="someone-else")))

    assert _run(repository.find_active(user_id="local", companion_id="astra")) is None


def test_end_marks_conversation_ended(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))

    _run(repository.end("01CONV", ended_at=_BASE + timedelta(minutes=5)))

    loaded = _run(repository.get("01CONV"))
    assert loaded is not None
    assert loaded.status is ConversationStatus.ENDED
    assert loaded.ended_at == _BASE + timedelta(minutes=5)
    assert _run(repository.find_active(user_id="local", companion_id="astra")) is None


# ── 消息 ────────────────────────────────────────────────


def test_last_message_at_on_empty_conversation(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))

    assert _run(repository.last_message_at("01CONV")) is None


def test_append_and_load_roundtrip(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))
    messages = (
        _message("01A", content="你好", metadata={"correlation_id": "01T"}),
        _message("01B", role=Role.ASSISTANT, content="你好呀"),
    )

    inserted = _run(repository.append_messages(messages))
    loaded = _run(repository.load_messages("01CONV"))

    assert inserted == ("01A", "01B")
    assert loaded == messages


def test_load_orders_by_id_not_created_at(repository: SQLiteConversationRepository) -> None:
    """同一时间戳的两条消息必须按 id 排序（时钟分辨率只有 15.6ms）。"""
    _run(repository.create(_conversation()))
    same_instant = _BASE
    _run(
        repository.append_messages(
            (
                _message("01B", role=Role.ASSISTANT, created_at=same_instant),
                _message("01A", role=Role.USER, created_at=same_instant),
            )
        )
    )

    loaded = _run(repository.load_messages("01CONV"))

    assert [message.id for message in loaded] == ["01A", "01B"]


def test_append_is_idempotent_per_turn_and_role(
    repository: SQLiteConversationRepository,
) -> None:
    """docs/04 §6：同一 (turn_id, role) 不能重复写入。"""
    _run(repository.create(_conversation()))
    message = _message("01A")

    first = _run(repository.append_messages((message,)))
    second = _run(repository.append_messages((_message("01B"),)))  # 同 turn_id + role

    assert first == ("01A",)
    assert second == ()
    assert len(_run(repository.load_messages("01CONV"))) == 1


def test_append_allows_same_turn_different_roles(
    repository: SQLiteConversationRepository,
) -> None:
    _run(repository.create(_conversation()))

    inserted = _run(
        repository.append_messages((_message("01A"), _message("01B", role=Role.ASSISTANT)))
    )

    assert inserted == ("01A", "01B")


def test_append_rolls_back_on_failure(repository: SQLiteConversationRepository) -> None:
    """两条消息里第二条失败时，第一条也不能留下。"""
    _run(repository.create(_conversation()))
    first = _message("01A")
    # 第二条复用同一个 id，但 turn_id/role 不同（绕过幂等检查）→ 主键冲突
    conflicting = _message("01A", turn_id="01T2", role=Role.ASSISTANT)

    with pytest.raises(sqlite3.Error):
        _run(repository.append_messages((first, conflicting)))

    assert _run(repository.load_messages("01CONV")) == ()


def test_last_message_at_reflects_latest_id(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))
    _run(
        repository.append_messages(
            (
                _message("01A", created_at=_BASE),
                _message("01B", role=Role.ASSISTANT, created_at=_BASE + timedelta(minutes=3)),
            )
        )
    )

    assert _run(repository.last_message_at("01CONV")) == _BASE + timedelta(minutes=3)


def test_metadata_survives_roundtrip(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation()))
    metadata = {"correlation_id": "01T", "status": "failed", "nested": {"a": 1}}

    _run(repository.append_messages((_message("01A", metadata=metadata),)))

    loaded = _run(repository.load_messages("01CONV"))
    assert loaded[0].metadata == metadata


def test_naive_datetime_is_rejected(repository: SQLiteConversationRepository) -> None:
    """naive 时间戳在 DB 里是有歧义的，必须拒绝而不是静默按本地时区处理。"""
    naive = datetime(2026, 9, 24, 12, 0)  # noqa: DTZ001 - 刻意构造 naive 值

    with pytest.raises(ValueError, match="timezone-aware"):
        _run(repository.create(_conversation(started_at=naive)))


# ── 删除（ADR 0029）─────────────────────────────────────


def test_delete_removes_conversation_and_its_messages(
    repository: SQLiteConversationRepository,
) -> None:
    """messages 没有外键，删除必须显式带上它们（0001_init.sql）。"""
    _run(repository.create(_conversation()))
    _run(repository.append_messages((_message("01A"), _message("01B", role=Role.ASSISTANT))))

    removed = _run(repository.delete("01CONV"))

    assert removed is True
    assert _run(repository.get("01CONV")) is None
    assert _run(repository.load_messages("01CONV")) == ()
    assert _run(repository.find_active(user_id="local", companion_id="astra")) is None


def test_delete_leaves_other_conversations_alone(repository: SQLiteConversationRepository) -> None:
    _run(repository.create(_conversation("01A")))
    _run(repository.create(_conversation("01B")))
    _run(repository.append_messages((_message("01A1", conversation_id="01A"),)))
    # turn_id 是幂等键的一部分（与 role 组合），两段会话的消息必须用不同的 turn
    _run(repository.append_messages((_message("01B1", conversation_id="01B", turn_id="01T2"),)))

    _run(repository.delete("01A"))

    assert _run(repository.get("01B")) is not None
    assert [message.id for message in _run(repository.load_messages("01B"))] == ["01B1"]


def test_delete_missing_returns_false(repository: SQLiteConversationRepository) -> None:
    assert _run(repository.delete("nope")) is False
