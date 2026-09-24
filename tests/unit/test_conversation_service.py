"""``ConversationService`` 的单元测试。

覆盖 ``resolve_or_create`` 的四个分支与 ``append_turn`` 的幂等性。
用真实 SQLite 仓储 + 可注入时钟，不引入假仓储。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.application.conversation_service import ConversationService
from astra.domain.conversation import Conversation, ConversationStatus, Role
from astra.domain.errors import ConversationNotFoundError
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.migrations import upgrade

_BASE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_IDLE = timedelta(minutes=60)


class FakeClock:
    """可推进的时钟，用来模拟空闲超时。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(_BASE)


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteConversationRepository:
    db_path = tmp_path / "astra.db"
    upgrade(db_path)
    return SQLiteConversationRepository(db_path)


@pytest.fixture
def service(repository: SQLiteConversationRepository, clock: FakeClock) -> ConversationService:
    return ConversationService(
        repository,
        user_id="local",
        companion_id="astra",
        idle_timeout=_IDLE,
        clock=clock,
    )


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def _say(
    service: ConversationService,
    conversation_id: str,
    *,
    turn_id: str,
    text: str = "你好",
    reply: str = "你好呀",
) -> None:
    await service.append_turn(
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_text=text,
        reply_text=reply,
        reply_metadata={},
        correlation_id=turn_id,
    )


# ── 新建 ────────────────────────────────────────────────


def test_creates_conversation_when_none_exists(service: ConversationService) -> None:
    resolved = _run(service.resolve_or_create(None))

    assert resolved.created is True
    assert resolved.conversation.status is ConversationStatus.ACTIVE
    assert resolved.conversation.user_id == "local"
    assert resolved.conversation.companion_id == "astra"
    assert resolved.conversation.started_at == _BASE


def test_reuses_active_conversation(service: ConversationService) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))

    second = _run(service.resolve_or_create(None))

    assert second.created is False
    assert second.conversation.id == first.conversation.id


def test_reuses_explicitly_requested_conversation(service: ConversationService) -> None:
    created = _run(service.resolve_or_create(None))

    resolved = _run(service.resolve_or_create(created.conversation.id))

    assert resolved.created is False
    assert resolved.conversation.id == created.conversation.id


# ── 空闲切分 ────────────────────────────────────────────


def test_idle_conversation_is_ended_and_replaced(
    service: ConversationService, clock: FakeClock
) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))

    clock.advance(_IDLE + timedelta(minutes=1))
    second = _run(service.resolve_or_create(None))

    assert second.created is True
    assert second.conversation.id != first.conversation.id


def test_ended_conversation_is_marked_ended(
    service: ConversationService, clock: FakeClock, repository: SQLiteConversationRepository
) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))

    clock.advance(_IDLE + timedelta(minutes=1))
    _run(service.resolve_or_create(None))

    old = _run(repository.get(first.conversation.id))
    assert old is not None
    assert old.status is ConversationStatus.ENDED
    assert old.ended_at == clock.now


def test_conversation_exactly_at_idle_boundary_is_replaced(
    service: ConversationService, clock: FakeClock
) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))

    clock.advance(_IDLE)
    second = _run(service.resolve_or_create(None))

    assert second.conversation.id != first.conversation.id


def test_just_below_idle_boundary_is_reused(service: ConversationService, clock: FakeClock) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))

    clock.advance(_IDLE - timedelta(seconds=1))

    assert _run(service.resolve_or_create(None)).conversation.id == first.conversation.id


def test_empty_conversation_idles_from_started_at(
    service: ConversationService, clock: FakeClock
) -> None:
    """没有消息的会话以 started_at 作为空闲基准。"""
    first = _run(service.resolve_or_create(None))

    clock.advance(_IDLE + timedelta(minutes=1))

    assert _run(service.resolve_or_create(None)).conversation.id != first.conversation.id


# ── 拒绝分支 ────────────────────────────────────────────


def test_unknown_conversation_id_is_rejected(service: ConversationService) -> None:
    """不静默新建——客户端传未知 id 是 bug 或被删的数据。"""
    with pytest.raises(ConversationNotFoundError, match="not found"):
        _run(service.resolve_or_create("01MISSING"))


def test_ended_conversation_id_is_rejected(service: ConversationService, clock: FakeClock) -> None:
    first = _run(service.resolve_or_create(None))
    _run(_say(service, first.conversation.id, turn_id="01T1"))
    clock.advance(_IDLE + timedelta(minutes=1))
    _run(service.resolve_or_create(None))

    with pytest.raises(ConversationNotFoundError, match="not active"):
        _run(service.resolve_or_create(first.conversation.id))


def test_conversation_of_another_user_is_rejected(
    repository: SQLiteConversationRepository, clock: FakeClock
) -> None:
    _run(
        repository.create(
            Conversation(
                id="01OTHER",
                user_id="someone-else",
                companion_id="astra",
                started_at=_BASE,
            )
        )
    )
    service = ConversationService(
        repository, user_id="local", companion_id="astra", idle_timeout=_IDLE, clock=clock
    )

    with pytest.raises(ConversationNotFoundError, match="another user"):
        _run(service.resolve_or_create("01OTHER"))


# ── 持久化 ──────────────────────────────────────────────


def test_append_turn_writes_both_messages_in_order(service: ConversationService) -> None:
    created = _run(service.resolve_or_create(None))

    _run(_say(service, created.conversation.id, turn_id="01T1", text="你好", reply="你好呀"))
    history = _run(service.load_history(created.conversation.id))

    assert [message.role for message in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].content == "你好"
    assert history[1].content == "你好呀"
    assert history[0].id < history[1].id
    assert history[0].turn_id == history[1].turn_id == "01T1"


def test_append_turn_records_correlation_id(service: ConversationService) -> None:
    created = _run(service.resolve_or_create(None))

    _run(_say(service, created.conversation.id, turn_id="01T1"))
    history = _run(service.load_history(created.conversation.id))

    assert all(message.metadata["correlation_id"] == "01T1" for message in history)


def test_append_turn_carries_reply_metadata(service: ConversationService) -> None:
    created = _run(service.resolve_or_create(None))

    _run(
        service.append_turn(
            conversation_id=created.conversation.id,
            turn_id="01T1",
            user_text="你好",
            reply_text="模型连不上了",
            reply_metadata={"status": "failed", "error_code": "llm_unavailable"},
            correlation_id="01T1",
        )
    )
    history = _run(service.load_history(created.conversation.id))

    assert history[1].metadata["status"] == "failed"
    assert history[1].metadata["error_code"] == "llm_unavailable"


def test_append_turn_is_idempotent(service: ConversationService) -> None:
    """docs/04 §6：同一 turn 重复持久化不能产生重复消息。"""
    created = _run(service.resolve_or_create(None))

    _run(_say(service, created.conversation.id, turn_id="01T1"))
    _run(_say(service, created.conversation.id, turn_id="01T1"))

    assert len(_run(service.load_history(created.conversation.id))) == 2


def test_load_history_is_empty_for_new_conversation(service: ConversationService) -> None:
    created = _run(service.resolve_or_create(None))

    assert _run(service.load_history(created.conversation.id)) == ()
