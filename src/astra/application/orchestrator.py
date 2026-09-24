"""一次 turn 的生命周期（`docs/02_ARCHITECTURE.md` §2.2）。

流水线与当前落点：

===========================  ==============================================
步骤                         状态
===========================  ==============================================
input_received               实现
normalize_input              实现（strip + 长度上限 + 空输入拒绝）
retrieve_memory              M2 实现（检索结果填进 prompt 的 Layer 5）
load_state                   M3 实现（快照填进 prompt 的 Layer 4）
build_context                实现
ask_llm                      实现（按 ADR 0011 的重试矩阵，最多重试 1 次）
validate_response            实现（``response_parser``）
apply_state_delta            M3 实现（事件规则 + 情绪靠拢 → 落库 + 发事件）
persist_conversation         实现（一个事务写 user + assistant）
enqueue_memory_extraction    M2 实现（候选打分 / 去重 / 落库）
dispatch_events              M1b 接入事件总线
===========================  ==============================================

**所有阶段都已实现，``STUB_STAGES`` 现在是空集。** 桩账本测试保留下来，
语义从"哪些是桩"变成"**还有没有桩**"——M6 加 ``tool_calls`` 时它还会响。

**M3 的状态顺序不能错**：快照必须在 ``ask_llm`` **之前**读（Layer 4 要用），
状态写回必须在 ``validate_response`` **之后**（要用到 ``emotion``）。
写反了会"用上一轮的状态回答这一轮"，而且很难看出来。这与 `docs/02` §2.2 的
流水线顺序一致。

**三个子系统的失败都不影响 turn**（记忆、状态、以及将来的工具）：各自的异常都在
自己的边界内被吞掉并记日志。LLM 已经成功，用户必须拿到回复（ADR 0014）。

**降级策略**（ADR 0014）：LLM 不可用**不抛 5xx**，而是返回一个
``TurnResult.failure`` + 由代码拥有的诚实回复，并且**仍然把用户消息持久化**，
否则重连的客户端会看到一条悬空的用户消息。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from astra.application.assistant_schema import build_response_format
from astra.application.conversation_service import ConversationService, ResolvedConversation
from astra.application.event_bus import EventBus
from astra.application.memory_service import MemoryService
from astra.application.prompt_builder import (
    ConversationContext,
    PromptBudget,
    build_conversation_context,
)
from astra.application.response_parser import ParseOutcome, parse_assistant_response
from astra.application.state_service import StateService
from astra.domain.conversation import Role
from astra.domain.errors import (
    AstraError,
    ErrorCode,
    InvalidInputError,
    LLMDisabledError,
    LLMResponseError,
)
from astra.domain.events import EventType, make_event
from astra.domain.ids import new_id
from astra.domain.llm import LLMMessage, LLMRequest, LLMResponse, ReasoningEffort
from astra.domain.memory import Memory
from astra.domain.persona import Persona
from astra.domain.ports import LLMProvider
from astra.domain.response import AssistantResponse
from astra.domain.state import StateSnapshot, baseline_emotion, baseline_relationship

_logger = logging.getLogger(__name__)

#: 首次调用 + 最多重试 1 次
MAX_ATTEMPTS = 2

#: 仍然刻意留空的阶段。**M3 起是空集**——所有流水线阶段都已实现。
#: 保留这个常量与「桩账本」测试，是为了 M6 加 ``tool_calls`` 时它还会响。
STUB_STAGES: frozenset[str] = frozenset()

#: 由**代码**拥有的诚实降级文案。
#: 不能问 LLM——LLM 正是挂掉的那个。人格文件可用同名错误码覆盖（ADR 0014）。
DEFAULT_ERROR_REPLIES: dict[ErrorCode, str] = {
    ErrorCode.LLM_UNAVAILABLE: (
        "我这边的模型服务连不上了。你确认一下 LM Studio 在运行，然后再说一次好吗？"
    ),
    ErrorCode.LLM_TIMEOUT: "我这边等模型等太久了，可能卡住了。你再说一次试试？",
    ErrorCode.LLM_DISABLED: "我现在还没有接上语言模型，所以没法回话。",
    ErrorCode.LLM_INVALID_RESPONSE: "模型这次给出的回复我没读懂，能再说一次吗？",
    ErrorCode.INVALID_INPUT: "我没看到你说什么，再说一次？",
    ErrorCode.CONVERSATION_NOT_FOUND: "这段对话已经不在了，我们重新开一段吧。",
    ErrorCode.INTERNAL_ERROR: "我这边出了点问题，没能处理这次消息。",
}

_REPAIR_INSTRUCTION = (
    "上一次的输出不是合法的 JSON。请只输出一个符合要求的 JSON 对象，"
    "不要包含任何解释、代码块标记或其他文字。"
)


@dataclass(frozen=True, slots=True)
class TurnConfig:
    """一次 turn 的模型与预算参数（来自 ``Settings``）。"""

    user_id: str
    companion_id: str
    temperature: float
    max_tokens: int
    request_timeout_s: float
    reasoning_effort: ReasoningEffort
    history_budget: PromptBudget
    input_max_chars: int = 4000


@dataclass(frozen=True, slots=True)
class TurnRequest:
    text: str
    conversation_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    #: 由调用方预先分配（``POST /v1/chat/messages`` 要在 202 里先返回它）。
    #: 为 ``None`` 时由 Orchestrator 生成。
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnFailure:
    code: ErrorCode
    message: str
    recoverable: bool


@dataclass(frozen=True, slots=True)
class TurnTimings:
    total_ms: float
    llm_ms: float | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    conversation_id: str
    response: AssistantResponse | None
    failure: TurnFailure | None
    stubs_used: frozenset[str]
    timings: TurnTimings
    model: str | None = None
    retries: int = 0
    degraded: bool = False
    #: M2 可观测性：本轮注入 prompt 的记忆条数 / 新建的记忆条数。
    #: 新建数不含"合并到既有记忆"的那些（那是更新，不是新建）。
    memories_retrieved: int = 0
    memories_created: int = 0

    @property
    def succeeded(self) -> bool:
        return self.failure is None

    @property
    def reply_text(self) -> str:
        """无论成功还是降级，都有一句可以展示给用户的话。"""
        if self.response is not None:
            return self.response.reply
        return self.failure.message if self.failure is not None else ""


class Orchestrator:
    """turn 生命周期的唯一入口。"""

    def __init__(
        self,
        *,
        llm: LLMProvider | None,
        conversations: ConversationService,
        persona: Persona,
        config: TurnConfig,
        bus: EventBus | None = None,
        memory: MemoryService | None = None,
        state: StateService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm = llm
        self._conversations = conversations
        self._persona = persona
        self._config = config
        #: 为 ``None`` 时记忆子系统整体缺席（不检索、不写入），turn 照常
        self._memory = memory
        #: 为 ``None`` 时状态子系统整体缺席（Layer 4 省略、状态不更新），turn 照常
        self._state = state
        #: 没有订阅者时 publish 是空操作，所以默认自带一个 bus 不会产生副作用
        self._bus = bus if bus is not None else EventBus()
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))
        #: 会话解析阶段的串行化，避免两个并发请求同时新建会话
        self._resolve_lock = asyncio.Lock()
        #: 每会话一把锁，保证消息顺序。M1 的会话数量由空闲切分限制在个位数；
        #: 真需要淘汰时再引入 LRU（M4 重新评估）。
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    async def run_turn(self, request: TurnRequest) -> TurnResult:
        """执行一次完整 turn。任何受控失败都体现在 ``TurnResult.failure`` 里。"""
        started = time.perf_counter()
        turn_id = request.turn_id or new_id()

        try:
            async with self._resolve_lock:
                resolved = await self._conversations.resolve_or_create(request.conversation_id)
        except AstraError as exc:
            # 显式给了未知 / 已结束的 conversation_id：不静默新建，如实报告
            return self._failed_result(
                turn_id,
                request.conversation_id or "",
                exc,
                started,
                retries=0,
                correlation_id=request.correlation_id or turn_id,
            )

        async with self._lock_for(resolved.conversation.id):
            return await self._execute(request, resolved, turn_id=turn_id, started=started)

    # ── 流水线 ──────────────────────────────────────────

    async def _execute(
        self,
        request: TurnRequest,
        resolved: ResolvedConversation,
        *,
        turn_id: str,
        started: float,
    ) -> TurnResult:
        correlation_id = request.correlation_id or turn_id
        conversation_id = resolved.conversation.id

        if resolved.created:
            self._publish(
                EventType.CONVERSATION_CREATED,
                correlation_id,
                {"conversation_id": conversation_id, "companion_id": self._config.companion_id},
            )

        self._publish(
            EventType.TURN_STARTED,
            correlation_id,
            {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "session_id": request.session_id,
            },
        )

        try:
            user_text = normalize_input(request.text, max_chars=self._config.input_max_chars)
        except InvalidInputError as exc:
            # 空输入不落库——没有值得保存的内容
            return self._failed_result(
                turn_id, conversation_id, exc, started, retries=0, correlation_id=correlation_id
            )

        try:
            snapshot = await self._load_state()
            memories = await self._retrieve_memory(
                user_text, relationship_relevance=snapshot.relationship.relevance()
            )
            context = await self._build_context(
                conversation_id, user_text, state=snapshot, memories=memories
            )
            llm_response, outcome, retries = await self._ask_and_parse(context, correlation_id)
        except AstraError as exc:
            return await self._persist_failure(
                conversation_id, turn_id, correlation_id, user_text, exc, started
            )

        user_message, assistant_message = await self._conversations.append_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            user_text=user_text,
            reply_text=outcome.response.reply,
            reply_metadata={"status": "ok", "model": llm_response.model},
            correlation_id=correlation_id,
        )

        self._publish_message(correlation_id, user_message.id, conversation_id, turn_id, Role.USER)
        self._publish_message(
            correlation_id, assistant_message.id, conversation_id, turn_id, Role.ASSISTANT
        )
        self._publish_assistant_output(correlation_id, turn_id, outcome.response)

        # 状态与记忆都排在事件之后：回复要尽快到达用户，落库是事后记账。
        # 放在 append_turn 之后是为了让候选拿到 user_message.id 作为 provenance。
        await self._apply_state_delta(outcome, user_text=user_text, correlation_id=correlation_id)
        created = await self._enqueue_memory_extraction(
            outcome, user_text=user_text, source_message_id=user_message.id
        )

        total_ms = _elapsed_ms(started)
        self._publish(
            EventType.TURN_COMPLETED,
            correlation_id,
            {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "status": "ok",
                "retries": retries,
                "degraded": outcome.degraded,
                "total_ms": total_ms,
            },
        )

        return TurnResult(
            turn_id=turn_id,
            conversation_id=conversation_id,
            response=outcome.response,
            failure=None,
            stubs_used=STUB_STAGES,
            timings=TurnTimings(total_ms=total_ms, llm_ms=llm_response.latency_ms),
            model=llm_response.model,
            retries=retries,
            degraded=outcome.degraded,
            memories_retrieved=len(memories),
            memories_created=created,
        )

    async def _build_context(
        self,
        conversation_id: str,
        user_text: str,
        *,
        state: StateSnapshot | None = None,
        memories: Sequence[Memory] = (),
    ) -> ConversationContext:
        history = await self._conversations.load_history(conversation_id)

        context = build_conversation_context(
            self._persona,
            history=history,
            user_text=user_text,
            budget=self._config.history_budget,
            state=state,
            memories=memories,
        )

        if context.history_window.dropped:
            _logger.info(
                "trimmed conversation history",
                extra={
                    "event": "llm.history.trimmed",
                    "detail": {"dropped_messages": context.history_window.dropped},
                },
            )

        return context

    async def _ask_and_parse(
        self, context: ConversationContext, correlation_id: str
    ) -> tuple[LLMResponse, ParseOutcome, int]:
        """调用 provider 并解析结果。按 ADR 0011 最多重试 1 次。"""
        if self._llm is None:
            raise LLMDisabledError("no llm provider is configured")

        base_request = self._build_llm_request(context, correlation_id)

        for attempt in range(MAX_ATTEMPTS):
            request = base_request if attempt == 0 else self._repair_request(base_request)
            try:
                response = await self._llm.generate(request)
                outcome = parse_assistant_response(response.text)
            except AstraError as exc:
                if not exc.retryable or attempt == MAX_ATTEMPTS - 1:
                    raise
                _logger.warning(
                    "llm call failed, retrying once",
                    extra={"event": "llm.retry", "detail": {"code": exc.code.value}},
                )
                continue
            return response, outcome, attempt

        # 循环只在 return 或 raise 处退出；这里只为类型检查兜底
        msg = "llm retry loop exited without a result"
        raise LLMResponseError(msg)

    def _build_llm_request(self, context: ConversationContext, correlation_id: str) -> LLMRequest:
        return LLMRequest(
            system_prompt=context.system_prompt,
            messages=context.messages,
            response_format=build_response_format(),
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            timeout_s=self._config.request_timeout_s,
            reasoning_effort=self._config.reasoning_effort,
            correlation_id=correlation_id,
        )

    def _repair_request(self, base: LLMRequest) -> LLMRequest:
        """重试时追加一条修复指令，并**强制关掉思考**。

        实测 ``content`` 为空 + ``finish_reason=length`` 是"思考吃光 token"的签名，
        所以重试一定要把 ``reasoning_effort`` 压到 ``none``。
        """
        return replace(
            base,
            messages=(*base.messages, LLMMessage(role=Role.USER, content=_REPAIR_INSTRUCTION)),
            reasoning_effort=ReasoningEffort.NONE,
        )

    # ── 状态（M3）───────────────────────────────────────

    async def _load_state(self) -> StateSnapshot:
        """读当前状态快照（含惰性衰减），填进 prompt 的 Layer 4。

        **失败返回基线**：``StateService`` 内部已经吞掉了存储异常，这里的兜底是
        为了它将来新增代码路径时也不会漏。状态缺席只是"她现在是平静的"。
        """
        if self._state is None:
            return _baseline_snapshot(self._clock())

        try:
            return await self._state.load_snapshot()
        except Exception:
            _logger.warning(
                "state load failed; continuing without state",
                extra={"event": "state.failed", "detail": {"stage": "orchestrator.load"}},
            )
            return _baseline_snapshot(self._clock())

    async def _apply_state_delta(
        self, outcome: ParseOutcome, *, user_text: str, correlation_id: str
    ) -> None:
        """按本轮的用户消息与已表达情绪更新状态。

        **方法名保留 ``apply_state_delta``**（对齐 `docs/02` §2.2 的流水线命名），
        但它**不消费 LLM 的 ``state_delta`` 字段**——那个字段没有加回 schema
        （实测 +2.3s/轮，ADR 0020）。增量来自用户侧事件规则与已经免费拿到的
        ``emotion``。命名与实现的这点差异记在 ADR 0020 里。

        ``correlation_id`` 传的是**该 turn 的** id：ADR 0012 要求一个 turn 的每个
        事件共用它。

        **失败只记日志**：``StateService`` 已经返回 ``None`` 表示失败，
        状态缺席不该让 turn 失败。
        """
        if self._state is None:
            return

        try:
            await self._state.apply_turn(
                user_text=user_text,
                emotion=outcome.response.emotion,
                correlation_id=correlation_id,
            )
        except Exception:
            _logger.warning(
                "state update failed; turn is unaffected",
                extra={"event": "state.failed", "detail": {"stage": "orchestrator.apply"}},
            )

    # ── 记忆（M2）───────────────────────────────────────

    async def _retrieve_memory(
        self, user_text: str, *, relationship_relevance: float = 0.0
    ) -> tuple[Memory, ...]:
        """检索与用户输入相关的记忆，填进 prompt 的 Layer 5。

        ``relationship_relevance`` 由调用方从关系状态算出（`docs/06` §4 的第五项）。

        **任何失败都返回空元组**——记忆缺席只是"这次没想起来"，而抛出去会让
        整轮对话失败。``MemoryService`` 内部已经吞掉了 embedding / 存储异常，
        这里的兜底是为了它将来新增代码路径时也不会漏。
        """
        if self._memory is None:
            return ()

        try:
            hits = await self._memory.retrieve(
                user_text, relationship_relevance=relationship_relevance
            )
        except Exception:
            # 记忆失败绝不能让 turn 失败：LLM 已经成功，用户必须拿到回复
            _logger.warning(
                "memory retrieval failed; continuing without memories",
                extra={"event": "memory.failed", "detail": {"stage": "orchestrator.retrieve"}},
            )
            return ()

        return tuple(hit.memory for hit in hits)

    async def _enqueue_memory_extraction(
        self, outcome: ParseOutcome, *, user_text: str, source_message_id: str
    ) -> int:
        """把本轮提取出的候选落库（打分 / 去重 / 向量化）。

        **方法名保留 ``enqueue_``**（对齐 `docs/02` §2.2 的流水线命名），但 M2 是
        **在 turn 内 await 完成**、不投递后台队列——这是"内联·精简候选"的直接推论：
        候选随回复一起产出，落库即持久，用户"说完就关程序"也不会丢（ADR 0018）。

        返回新建的记忆条数。**失败只记日志并返回 0**，turn 照常成功。
        """
        if self._memory is None:
            return 0

        candidates = outcome.response.memory_candidates
        if not candidates:
            return 0

        try:
            created = await self._memory.ingest_candidates(
                candidates, user_text=user_text, source_message_id=source_message_id
            )
        except Exception:
            # 同上：记忆落库失败只是"这次没记住"，turn 仍然成功
            _logger.warning(
                "memory extraction failed; turn is unaffected",
                extra={"event": "memory.failed", "detail": {"stage": "orchestrator.ingest"}},
            )
            return 0

        return len(created)

    # ── 失败处理 ────────────────────────────────────────

    async def _persist_failure(
        self,
        conversation_id: str,
        turn_id: str,
        correlation_id: str,
        user_text: str,
        error: AstraError,
        started: float,
    ) -> TurnResult:
        """降级：写入诚实回复并保留用户消息。"""
        reply = self._honest_reply(error.code)

        await self._conversations.append_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            user_text=user_text,
            reply_text=reply,
            reply_metadata={"status": "failed", "error_code": error.code.value},
            correlation_id=correlation_id,
        )

        _logger.warning(
            "turn failed",
            extra={
                "event": "turn.failed",
                "detail": {"code": error.code.value, "error": error.message},
            },
        )

        return self._failed_result(
            turn_id, conversation_id, error, started, retries=0, correlation_id=correlation_id
        )

    def _failed_result(
        self,
        turn_id: str,
        conversation_id: str,
        error: AstraError,
        started: float,
        *,
        retries: int,
        correlation_id: str | None = None,
    ) -> TurnResult:
        reply = self._honest_reply(error.code)

        self._publish(
            EventType.TURN_FAILED,
            correlation_id or turn_id,
            {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "code": error.code.value,
                "message": error.message,
                "reply": reply,
                "recoverable": error.retryable or error.code is ErrorCode.LLM_UNAVAILABLE,
            },
        )

        return TurnResult(
            turn_id=turn_id,
            conversation_id=conversation_id,
            response=None,
            failure=TurnFailure(
                code=error.code,
                message=reply,
                recoverable=error.retryable or error.code is ErrorCode.LLM_UNAVAILABLE,
            ),
            stubs_used=STUB_STAGES,
            timings=TurnTimings(total_ms=_elapsed_ms(started)),
            retries=retries,
        )

    def _honest_reply(self, code: ErrorCode) -> str:
        default = DEFAULT_ERROR_REPLIES.get(code, DEFAULT_ERROR_REPLIES[ErrorCode.INTERNAL_ERROR])
        return self._persona.error_message(code.value, default=default)

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[conversation_id] = lock
        return lock

    # ── 事件发布 ────────────────────────────────────────

    def _publish(self, event_type: EventType, correlation_id: str, payload: dict[str, Any]) -> None:
        self._bus.publish(make_event(event_type, correlation_id=correlation_id, payload=payload))

    def _publish_message(
        self,
        correlation_id: str,
        message_id: str,
        conversation_id: str,
        turn_id: str,
        role: Role,
    ) -> None:
        self._publish(
            EventType.CONVERSATION_MESSAGE_CREATED,
            correlation_id,
            {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "role": role.value,
            },
        )

    def _publish_assistant_output(
        self, correlation_id: str, turn_id: str, response: AssistantResponse
    ) -> None:
        """把回复正文与**归一化后**的 emotion / action 发给客户端。

        emotion / action 必须是归一化后的值——原始 LLM 字符串绝不能流到 Unity
        （`docs/04_API_AND_EVENTS.md` §5 明文要求）。
        """
        self._publish(
            EventType.ASSISTANT_TEXT_COMPLETED,
            correlation_id,
            {
                "turn_id": turn_id,
                "reply": response.reply,
                "emotion": {
                    "primary": response.emotion.primary,
                    "intensity": response.emotion.intensity,
                },
                "action": {
                    "name": response.action.name,
                    "intensity": response.action.intensity,
                },
                "voice": {
                    "style": response.voice.style,
                    "speed": response.voice.speed,
                    "pitch": response.voice.pitch,
                },
            },
        )
        self._publish(
            EventType.AVATAR_EXPRESSION_SET,
            correlation_id,
            {
                "turn_id": turn_id,
                "emotion": response.emotion.primary,
                "intensity": response.emotion.intensity,
            },
        )
        self._publish(
            EventType.AVATAR_ACTION_PLAY,
            correlation_id,
            {
                "turn_id": turn_id,
                "name": response.action.name,
                "intensity": response.action.intensity,
                "priority": "normal",
            },
        )


def _baseline_snapshot(now: datetime) -> StateSnapshot:
    """状态子系统缺席 / 失败时用的快照。

    用**基线**而不是"空状态"：Layer 4 渲染出来的是"她现在是平静的"，
    而不是"她的情绪是未定义"。
    """
    return StateSnapshot(
        emotion=baseline_emotion(),
        relationship=baseline_relationship(),
        emotion_updated_at=now,
        relationship_updated_at=now,
    )


def normalize_input(text: str, *, max_chars: int) -> str:
    """归一化用户输入。

    Raises:
        InvalidInputError: 去掉首尾空白后为空。
    """
    normalized = text.strip()
    if not normalized:
        raise InvalidInputError("input text is empty")

    if len(normalized) > max_chars:
        _logger.warning(
            "truncated over-long input",
            extra={"event": "turn.input.truncated", "detail": {"limit": max_chars}},
        )
        normalized = normalized[:max_chars]

    return normalized


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
