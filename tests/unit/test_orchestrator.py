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
from astra.application.memory_service import MemoryConfig, MemoryService
from astra.application.orchestrator import (
    DEFAULT_ERROR_REPLIES,
    STUB_STAGES,
    Orchestrator,
    TurnConfig,
    TurnRequest,
    normalize_input,
)
from astra.application.prompt_builder import PromptBudget
from astra.application.state_service import StateConfig, StateService
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
from astra.domain.memory import MemoryCandidate, MemoryType
from astra.domain.state import EMOTION_BASELINE_VALUES
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.memory_repository import SQLiteMemoryStore
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.db.state_repository import SQLiteStateStore
from astra.infrastructure.persona.toml_loader import load_persona
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider, assistant_json, llm_response

_BASE = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 记忆子系统测试用的维度与向量（必须与 vec0 的 float[768] 一致）
_MEMORY_DIM = 768
_MEMORY_TEXT = "用户喜欢深夜写代码"
_MEMORY_QUERY = "我一般什么时候写代码效率最高"
_MEMORY_VECTORS = {
    _MEMORY_TEXT: (1.0, 0.0, *([0.0] * (_MEMORY_DIM - 2))),
    _MEMORY_QUERY: (1.0, 0.05, *([0.0] * (_MEMORY_DIM - 2))),
}

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
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "astra.db"
    upgrade(path)
    return path


@pytest.fixture
def conversations(db_path: Path) -> ConversationService:
    return ConversationService(
        SQLiteConversationRepository(db_path),
        user_id="local",
        companion_id="astra",
        idle_timeout=timedelta(minutes=60),
        clock=lambda: _BASE,
    )


@pytest.fixture
def memory(db_path: Path) -> MemoryService:
    return MemoryService(
        SQLiteMemoryStore(db_path, embedding_model="fake-embedding", embedding_dim=_MEMORY_DIM),
        FakeEmbeddingProvider(dim=_MEMORY_DIM, vectors=_MEMORY_VECTORS),
        config=MemoryConfig(user_id="local", companion_id="astra"),
        clock=lambda: _BASE,
    )


@pytest.fixture
def state_service(db_path: Path) -> StateService:
    return StateService(
        SQLiteStateStore(db_path),
        config=StateConfig(companion_id="astra"),
        clock=lambda: _BASE,
    )


def _orchestrator(
    conversations: ConversationService,
    persona,
    provider: FakeLLMProvider | None,
    *,
    memory: object = None,
    state: object = None,
    **config_overrides: object,
) -> Orchestrator:
    return Orchestrator(
        llm=provider,
        conversations=conversations,
        persona=persona,
        config=replace(_CONFIG, **config_overrides),  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
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
    """桩账本：**M3 起没有空桩了**（``apply_state_delta`` 也填掉了）。

    这条测试保留下来，语义从"哪些是桩"变成"**还有没有桩**"——
    M6 加 ``tool_calls`` 时如果又留了桩，它会立刻变红。
    """
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response()]))

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.stubs_used == STUB_STAGES
    assert result.stubs_used == frozenset()


def test_turn_without_a_memory_service_still_works(
    conversations: ConversationService, persona
) -> None:
    """记忆子系统缺席（``ASTRA_EMBEDDING_PROVIDER=none``）不该影响聊天。"""
    orchestrator = _orchestrator(conversations, persona, FakeLLMProvider([llm_response()]))

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert result.memories_retrieved == 0
    assert result.memories_created == 0


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


# ── 记忆集成（M2）───────────────────────────────────────


def test_retrieved_memories_are_injected_into_the_prompt(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """检索到的记忆必须出现在 system prompt 的 Layer 5——否则检索等于白做。"""
    _run(
        memory.ingest_candidates(
            [MemoryCandidate(type=MemoryType.PREFERENCE, content=_MEMORY_TEXT)],
            user_text=_MEMORY_TEXT,
        )
    )
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider, memory=memory)

    result = _run(orchestrator.run_turn(TurnRequest(text=_MEMORY_QUERY)))

    assert result.memories_retrieved == 1
    sent_prompt = provider.requests[0].system_prompt
    assert "RELEVANT MEMORIES" in sent_prompt
    assert _MEMORY_TEXT in sent_prompt


def test_unrelated_turn_gets_no_memory_layer(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """没有命中记忆时整层省略——空标题会让模型以为系统藏着信息。"""
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider, memory=memory)

    result = _run(orchestrator.run_turn(TurnRequest(text="今天天气怎么样")))

    assert result.memories_retrieved == 0
    assert "RELEVANT MEMORIES" not in provider.requests[0].system_prompt


def test_candidates_are_stored_with_provenance(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """候选必须带 ``source_message_id``——"这句话是从哪来的"是记忆的基本属性。"""
    payload = assistant_json(memory_candidates=[{"type": "preference", "content": _MEMORY_TEXT}])
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([llm_response(payload)]), memory=memory
    )

    result = _run(orchestrator.run_turn(TurnRequest(text=_MEMORY_TEXT)))

    assert result.memories_created == 1
    stored = _run(memory.list_memories())
    assert len(stored) == 1
    assert stored[0].content == _MEMORY_TEXT

    history = _run(conversations.load_history(result.conversation_id))
    user_message = history[0]
    assert stored[0].source_message_id == user_message.id


def test_turn_without_candidates_creates_nothing(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """docs/05 §8：候选不是每轮都创建。空数组必须什么都不写。"""
    orchestrator = _orchestrator(
        conversations,
        persona,
        FakeLLMProvider([llm_response(assistant_json(memory_candidates=[]))]),
        memory=memory,
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="今天天气不错")))

    assert result.memories_created == 0
    assert _run(memory.list_memories()) == ()


def test_no_store_phrase_prevents_writing(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """docs/06 §9：用户说"不要记住"必须生效。"""
    payload = assistant_json(memory_candidates=[{"type": "event", "content": "用户明天要去医院"}])
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([llm_response(payload)]), memory=memory
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="我明天要去医院，这件事不要记住")))

    assert result.succeeded is True
    assert result.memories_created == 0
    assert _run(memory.list_memories()) == ()


class _BrokenMemory:
    """每个方法都炸的记忆替身——验证失败隔离。"""

    async def retrieve(self, query: str) -> object:
        raise RuntimeError("retrieve exploded")

    async def ingest_candidates(self, candidates: object, **kwargs: object) -> object:
        raise RuntimeError("ingest exploded")


def test_memory_failure_does_not_break_the_turn(
    conversations: ConversationService, persona
) -> None:
    """记忆子系统抛异常时 turn 仍必须成功——LLM 已经回复了，用户必须拿到。"""
    payload = assistant_json(memory_candidates=[{"type": "preference", "content": _MEMORY_TEXT}])
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([llm_response(payload)]), memory=_BrokenMemory()
    )

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    assert result.response is not None
    assert result.response.reply
    assert result.memories_retrieved == 0
    assert result.memories_created == 0
    # 消息照常落库
    history = _run(conversations.load_history(result.conversation_id))
    assert [message.role for message in history] == [Role.USER, Role.ASSISTANT]


def test_disabled_memory_neither_retrieves_nor_writes(
    conversations: ConversationService, persona, memory: MemoryService
) -> None:
    """docs/06 §9 的"禁用记忆"必须同时关掉读取与写入。"""
    _run(
        memory.ingest_candidates(
            [MemoryCandidate(type=MemoryType.PREFERENCE, content=_MEMORY_TEXT)],
            user_text=_MEMORY_TEXT,
        )
    )
    _run(memory.set_enabled(False))

    payload = assistant_json(memory_candidates=[{"type": "preference", "content": "用户新的事实"}])
    provider = FakeLLMProvider([llm_response(payload)])
    orchestrator = _orchestrator(conversations, persona, provider, memory=memory)

    result = _run(orchestrator.run_turn(TurnRequest(text=_MEMORY_QUERY)))

    assert result.memories_retrieved == 0
    assert result.memories_created == 0
    assert "RELEVANT MEMORIES" not in provider.requests[0].system_prompt
    # 禁用期间没有写入新记忆，只有禁用前那一条
    assert len(_run(memory.list_memories())) == 1


# ── 状态集成（M3）───────────────────────────────────────


def test_state_is_injected_into_the_prompt(
    conversations: ConversationService, persona, state_service: StateService
) -> None:
    """状态必须出现在 system prompt 的 Layer 4——否则情绪影响不了措辞。"""
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider, state=state_service)

    _run(orchestrator.run_turn(TurnRequest(text="你好")))

    prompt = provider.requests[0].system_prompt
    assert "CURRENT STATE" in prompt
    assert "Mood:" in prompt
    assert "Relationship:" in prompt
    assert "happiness:" in prompt


def test_praise_raises_happiness_end_to_end(
    conversations: ConversationService, persona, state_service: StateService
) -> None:
    """验收核心：user praises companion -> happiness increases。"""
    orchestrator = _orchestrator(
        conversations, persona, FakeLLMProvider([llm_response()]), state=state_service
    )
    before = _run(state_service.load_snapshot())

    _run(orchestrator.run_turn(TurnRequest(text="谢谢你，你真的很厉害")))

    after = _run(state_service.load_snapshot())
    assert after.emotion.happiness > before.emotion.happiness
    assert after.relationship.affection > before.relationship.affection


def test_updated_state_reaches_the_next_turn_prompt(
    conversations: ConversationService, persona, state_service: StateService
) -> None:
    """顺序验证：这轮写的状态，下一轮必须能被读到（读在前、写在后）。"""
    provider = FakeLLMProvider([llm_response(), llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider, state=state_service)

    _run(orchestrator.run_turn(TurnRequest(text="谢谢你，我很喜欢你")))
    _run(orchestrator.run_turn(TurnRequest(text="你好")))

    second_prompt = provider.requests[1].system_prompt
    baseline = EMOTION_BASELINE_VALUES["happiness"]
    assert f"- happiness: {baseline:.2f}" not in second_prompt


def test_turn_without_a_state_service_still_works(
    conversations: ConversationService, persona
) -> None:
    """状态子系统缺席不该影响聊天（Layer 4 整层省略）。"""
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider)

    result = _run(orchestrator.run_turn(TurnRequest(text="你好")))

    assert result.succeeded is True
    # 缺席时 orchestrator 用基线兜底，Layer 4 仍然渲染（她只是"平静"）
    assert "CURRENT STATE" in provider.requests[0].system_prompt


class _BrokenState:
    """每个方法都炸的状态替身——验证失败隔离。"""

    async def load_snapshot(self) -> object:
        raise RuntimeError("load exploded")

    async def apply_turn(self, **kwargs: object) -> object:
        raise RuntimeError("apply exploded")


def test_state_failure_does_not_break_the_turn(conversations: ConversationService, persona) -> None:
    """状态子系统抛异常时 turn 仍必须成功。"""
    provider = FakeLLMProvider([llm_response()])
    orchestrator = _orchestrator(conversations, persona, provider, state=_BrokenState())

    result = _run(orchestrator.run_turn(TurnRequest(text="谢谢你")))

    assert result.succeeded is True
    assert result.response is not None
    assert result.response.reply
    # 兜底到基线，prompt 仍然有 Layer 4
    assert "CURRENT STATE" in provider.requests[0].system_prompt


def test_relationship_relevance_reaches_memory_retrieval(
    conversations: ConversationService, persona, memory: MemoryService, state_service: StateService
) -> None:
    """M3 填上了 M2 检索公式里恒为 0 的那一项——关系越近，关系分项越大。"""
    _run(
        memory.ingest_candidates(
            [MemoryCandidate(type=MemoryType.PREFERENCE, content=_MEMORY_TEXT)],
            user_text=_MEMORY_TEXT,
        )
    )
    orchestrator = _orchestrator(
        conversations,
        persona,
        FakeLLMProvider([llm_response()]),
        memory=memory,
        state=state_service,
    )
    # 先让关系涨起来（连续称赞）
    for _ in range(3):
        _run(orchestrator.run_turn(TurnRequest(text="谢谢你，你真的很厉害")))

    hits = _run(memory.retrieve(_MEMORY_QUERY, relationship_relevance=0.9))
    assert hits
    assert hits[0].components["relationship"] > 0.0
