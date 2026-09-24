"""``Orchestrator`` 的单元测试。

覆盖：完整生命周期、桩账本、重试矩阵、失败降级、持久化、每会话串行化。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astra.application.conversation_service import ConversationService
from astra.application.orchestrator import (
    DEFAULT_ERROR_REPLIES,
    STUB_STAGES,
    Orchestrator,
    TurnConfig,
    TurnRequest,
    normalize_input,
)
from astra.application.prompt_builder import PromptBudget
from astra.domain.conversation import Role
from astra.domain.errors import (
    ErrorCode,
    InvalidInputError,
    LLMDisabledError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from astra.domain.llm import ReasoningEffort
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.persona.toml_loader import load_persona
from tests.fakes import FakeLLMProvider, llm_response

_BASE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFIG = TurnConfig(
    user_id="local",
    companion_id="astra",
    temperature=0.7,
    max_tokens=400,
    request_timeout_s=30.0,
    reasoning_effort=ReasoningEffort.NONE,
    history_budget=PromptBudget(20, 12000),
)


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def persona():
    return load_persona(_REPO_ROOT / "personas" / "astra.toml")


@pytest.fixture
def conversations(tmp_path: Path) -> ConversationService:
    db_path = tmp_path / "astra.db"
    upgrade(db_path)
    return ConversationService(
        SQLiteConversationRepository(db_path),
        user_id="local",
        companion_id="astra",
        idle_timeout=timedelta(minutes=60),
        clock=lambda: _BASE,
    )


def _orchestrator(
    conversations: ConversationService,
    persona,
    provider: FakeLLMProvider | None,
    **config_overrides: object,
) -> Orchestrator:
    return Orchestrator(
        llm=provider,
        conversations=conversations,
        persona=persona,
        config=replace(_CONFIG, **config_overrides),  # type: ignore[arg-type]
        clock=lambda: _BASE,
    )


# ── 正常路径 ────────────────────────────────────────────


def test_successful_turn(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert result.response is not None
    assert result.response.reply == "你好呀，今天过得怎么样？"
    assert result.response.emotion.primary == "happy"
    assert result.conversation_id
    assert result.turn_id
    assert result.retries == 0
    assert result.model == "fake-model"
    assert result.timings.total_ms >= 0.0
    assert result.timings.llm_ms == 1.0


def test_successful_turn_persists_both_messages(
    conversations: ConversationService, persona
) -> None:
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response()]))

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))
    history = _run(conversations.load_history(result.conversation_id))

    assert [message.role for message in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].content == "你好"
    assert history[1].metadata["status"] == "ok"


def test_stub_ledger(conversations: ConversationService, persona) -> None:
    """桩账本：M2/M3 实现任何一个空桩都会让这条断言变红，强制显式更新。"""
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response()]))

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.stubs_used == STUB_STAGES
    assert result.stubs_used == {
        "retrieve_memory",
        "apply_state_delta",
        "enqueue_memory_extraction",
    }


def test_second_turn_sees_history(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response(), llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    _run(orchestrator.run_turn(TurnRequest(text="第一句")))
    _run(orchestrator.run_turn(TurnRequest(text="第二句")))

    second_request = provider.requests[1]
    contents = [message.content for message in second_request.messages]
    assert contents == ["第一句", "你好呀，今天过得怎么样？", "第二句"]


def test_llm_request_carries_configuration(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    _run(orchestrator.run_turn(TurnRequest(text="你好")))

    request = provider.requests[0]
    assert request.temperature == 0.7
    assert request.max_tokens == 400
    assert request.reasoning_effort is ReasoningEffort.NONE
    assert request.response_format is not None
    assert request.response_format.name == "AssistantResponse"
    assert "HARD CONSTRAINTS" in request.system_prompt


def test_correlation_id_is_propagated(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好", correlation_id="01CORR")))
    history = _run(conversations.load_history(result.conversation_id))

    assert provider.requests[0].correlation_id == "01CORR"
    assert all(message.metadata["correlation_id"] == "01CORR" for message in history)


def test_correlation_id_defaults_to_turn_id(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert provider.requests[0].correlation_id == result.turn_id


def test_parsed_degradation_is_surfaced(conversations: ConversationService, persona) -> None:
    raw = json.dumps(
        {
            "reply": "好的",
            "emotion": {"primary": "开心", "intensity": 5.0},
            "action": {"name": "nod", "intensity": 0.4},
            "voice": {"style": "warm", "speed": 1.0, "pitch": 0.0},
        },
        ensure_ascii=False,
    )
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response(raw)]))

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert result.degraded is True
    assert result.response is not None
    assert result.response.emotion.primary == "neutral"
    assert result.response.emotion.intensity == 1.0


# ── 输入归一化 ──────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_input_is_rejected(text: str) -> None:
    with pytest.raises(InvalidInputError):
        normalize_input(text, max_chars=100)


def test_input_is_stripped() -> None:
    assert normalize_input("  你好  ", max_chars=100) == "你好"


def test_over_long_input_is_truncated() -> None:
    assert normalize_input("x" * 50, max_chars=10) == "x" * 10


def test_empty_input_fails_without_persisting(conversations: ConversationService, persona) -> None:
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response()]))

    result = _run(orchestrator.run_turn(TurnRequest(text="   ")))

    assert result.succeeded is False
    assert result.failure is not None
    assert result.failure.code is ErrorCode.INVALID_INPUT
    assert _run(conversations.load_history(result.conversation_id)) == ()


# ── 重试矩阵 ────────────────────────────────────────────


def test_retryable_error_retries_once_and_succeeds(
    conversations: ConversationService, persona
) -> None:
    provider = FakeLLMProvider([LLMTimeoutError("slow"), llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert result.retries == 1
    assert provider.call_count == 2


def test_non_retryable_error_does_not_retry(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([LLMUnavailableError("refused"), llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is False
    assert provider.call_count == 1


def test_retry_forces_reasoning_off(conversations: ConversationService, persona) -> None:
    """content 为空是"思考吃光 token"的签名，重试必须把思考压到 none。"""
    provider = FakeLLMProvider([llm_response(""), llm_response()])
    orchestrator = _orchestrator(
        conversations,
        persona,
        provider,
        reasoning_effort=ReasoningEffort.MEDIUM,
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert provider.requests[0].reasoning_effort is ReasoningEffort.MEDIUM
    assert provider.requests[1].reasoning_effort is ReasoningEffort.NONE


def test_retry_appends_repair_instruction(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response("这不是 JSON"), llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    _run(orchestrator.run_turn(TurnRequest(text="你好")))

    first_messages = provider.requests[0].messages
    retry_messages = provider.requests[1].messages
    assert len(retry_messages) == len(first_messages) + 1
    assert "JSON" in retry_messages[-1].content


def test_retry_exhaustion_fails(conversations: ConversationService, persona) -> None:
    provider = FakeLLMProvider([llm_response("坏输出"), llm_response("还是坏输出")])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is False
    assert result.failure is not None
    assert result.failure.code is ErrorCode.LLM_INVALID_RESPONSE
    assert provider.call_count == 2


# ── 降级 ────────────────────────────────────────────────


def test_llm_disabled_is_reported_honestly(conversations: ConversationService, persona) -> None:
    orchestrator = _orchestrator(conversations, persona, None)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is False
    assert result.failure is not None
    assert result.failure.code is ErrorCode.LLM_DISABLED
    assert result.failure.message == DEFAULT_ERROR_REPLIES[ErrorCode.LLM_DISABLED]


def test_failure_still_persists_user_message_and_honest_reply(
    conversations: ConversationService, persona
) -> None:
    """否则重连的客户端会看到一条悬空的用户消息。"""
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([LLMUnavailableError("refused")])
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))
    history = _run(conversations.load_history(result.conversation_id))

    assert [message.role for message in history] == [Role.USER, Role.ASSISTANT]
    assert history[0].content == "你好"
    assert history[1].metadata["status"] == "failed"
    assert history[1].metadata["error_code"] == "llm_unavailable"
    assert history[1].content


def test_persona_can_override_error_message(
    conversations: ConversationService, tmp_path: Path
) -> None:
    persona_file = tmp_path / "persona.toml"
    persona_file.write_text(
        """
[identity]
name = "Test"
display_name = "Test"
pronouns = "它"
self_description = "x"

[communication]
sentence_length = "short"
emoji_frequency = "low"
rhetorical_questions = "rare"
preferred_interaction = "conversational"

[errors]
llm_unavailable = "自定义降级话术"
""",
        encoding="utf-8",
    )
    persona = load_persona(persona_file)
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([LLMUnavailableError("refused")])
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.failure is not None
    assert result.failure.message == "自定义降级话术"


def test_failure_result_has_no_response(conversations: ConversationService, persona) -> None:
    orchestrator = _orchestrator(conversations, persona, None)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.response is None
    assert result.failure is not None
    assert result.reply_text == result.failure.message


def test_llm_disabled_error_is_not_retryable() -> None:
    assert LLMDisabledError("x").retryable is False
    assert LLMResponseError("x").retryable is True


# ── 串行化 ──────────────────────────────────────────────


def test_turns_in_same_conversation_are_serialized(
    conversations: ConversationService, persona
) -> None:
    """同一会话的并发 turn 不能交错——否则消息顺序会乱。"""

    async def scenario() -> list[str]:
        provider = FakeLLMProvider([llm_response() for _ in range(4)])
        orchestrator = _orchestrator(conversations, persona, provider)

        first = await orchestrator.run_turn(TurnRequest(text="一"))
        await asyncio.gather(
            orchestrator.run_turn(TurnRequest(text="二", conversation_id=first.conversation_id)),
            orchestrator.run_turn(TurnRequest(text="三", conversation_id=first.conversation_id)),
        )
        history = await conversations.load_history(first.conversation_id)
        return [message.role.value for message in history]

    roles = asyncio.run(scenario())

    # 三个 turn 共 6 条消息，必须严格 user/assistant 交替（没有交错）
    assert roles == ["user", "assistant"] * 3
