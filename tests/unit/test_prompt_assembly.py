"""``astra.application.prompt_builder`` 的单元测试。

覆盖上下文窗口裁剪与消息组装。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astra.application.prompt_builder import (
    PromptBudget,
    build_conversation_context,
    trim_history,
)
from astra.domain.conversation import Message, Role
from astra.infrastructure.persona.toml_loader import load_persona

_REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PERSONA = _REPO_ROOT / "personas" / "astra.toml"

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def persona():
    return load_persona(SHIPPED_PERSONA)


def _message(index: int, *, role: Role = Role.USER, content: str = "内容") -> Message:
    return Message(
        id=f"01{index:04d}",
        conversation_id="01CONV",
        turn_id=f"01T{index:04d}",
        role=role,
        content=content,
        created_at=_NOW,
    )


def _history(count: int, *, content: str = "内容") -> list[Message]:
    return [
        _message(index, role=Role.USER if index % 2 == 0 else Role.ASSISTANT, content=content)
        for index in range(count)
    ]


# ── trim_history ────────────────────────────────────────


def test_empty_history_stays_empty() -> None:
    window = trim_history([], budget=PromptBudget(20, 12000))

    assert window.messages == ()
    assert window.dropped == 0


def test_history_within_budget_is_untouched() -> None:
    history = _history(5)

    window = trim_history(history, budget=PromptBudget(20, 12000))

    assert window.messages == tuple(history)
    assert window.dropped == 0


def test_message_count_limit_keeps_the_newest() -> None:
    history = _history(10)

    window = trim_history(history, budget=PromptBudget(4, 12000))

    assert [message.id for message in window.messages] == [m.id for m in history[-4:]]
    assert window.dropped == 6


def test_character_limit_drops_oldest_first() -> None:
    history = _history(5, content="x" * 100)

    window = trim_history(history, budget=PromptBudget(20, 250))

    # 250 / 100 = 2 条放得下，第 3 条会超预算
    assert len(window.messages) == 2
    assert window.dropped == 3
    assert window.messages == tuple(history[-2:])


def test_at_least_one_message_is_always_kept() -> None:
    """最新那条单独就超预算时也必须保留——丢掉全部历史比超预算更糟。"""
    history = _history(3, content="x" * 1000)

    window = trim_history(history, budget=PromptBudget(20, 50))

    assert len(window.messages) == 1
    assert window.messages[0] == history[-1]


def test_message_limit_takes_precedence_over_character_limit() -> None:
    history = _history(10, content="x")

    window = trim_history(history, budget=PromptBudget(3, 12000))

    assert len(window.messages) == 3


# ── build_conversation_context ──────────────────────────


def test_context_uses_persona_for_system_prompt(persona) -> None:
    context = build_conversation_context(
        persona,
        history=(),
        user_text="你好",
        budget=PromptBudget(20, 12000),
    )

    assert persona.identity.name in context.system_prompt
    assert "HARD CONSTRAINTS" in context.system_prompt


def test_current_user_message_is_appended_last(persona) -> None:
    context = build_conversation_context(
        persona,
        history=_history(2),
        user_text="最新一句",
        budget=PromptBudget(20, 12000),
    )

    assert context.messages[-1].role is Role.USER
    assert context.messages[-1].content == "最新一句"


def test_history_is_mapped_to_llm_messages(persona) -> None:
    history = _history(2, content="旧内容")

    context = build_conversation_context(
        persona,
        history=history,
        user_text="新内容",
        budget=PromptBudget(20, 12000),
    )

    assert [message.content for message in context.messages] == ["旧内容", "旧内容", "新内容"]
    assert [message.role for message in context.messages] == [Role.USER, Role.ASSISTANT, Role.USER]


def test_non_conversation_roles_are_filtered_out(persona) -> None:
    """system / tool 消息不是"当前对话"的一部分，不能进 messages。"""
    history = [
        _message(0, role=Role.SYSTEM, content="内部指令"),
        _message(1, role=Role.USER, content="用户消息"),
    ]

    context = build_conversation_context(
        persona,
        history=history,
        user_text="新消息",
        budget=PromptBudget(20, 12000),
    )

    assert [message.content for message in context.messages] == ["用户消息", "新消息"]


def test_current_user_message_survives_aggressive_trimming(persona) -> None:
    """当前用户消息在裁剪之后追加，因此永远不会被裁掉。"""
    context = build_conversation_context(
        persona,
        history=_history(50, content="x" * 500),
        user_text="这一句必须留下",
        budget=PromptBudget(2, 100),
    )

    assert context.messages[-1].content == "这一句必须留下"
    assert context.history_window.dropped > 0


def test_history_window_reports_dropped_count(persona) -> None:
    context = build_conversation_context(
        persona,
        history=_history(10),
        user_text="新消息",
        budget=PromptBudget(3, 12000),
    )

    assert context.history_window.dropped == 7
    assert len(context.history_window.messages) == 3


def test_system_prompt_is_stable_across_calls(persona) -> None:
    budget = PromptBudget(20, 12000)

    first = build_conversation_context(persona, history=(), user_text="a", budget=budget)
    second = build_conversation_context(persona, history=(), user_text="b", budget=budget)

    assert first.system_prompt == second.system_prompt
