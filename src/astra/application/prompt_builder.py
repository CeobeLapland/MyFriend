"""把人格 + 记忆 + 历史组装成一次 LLM 调用的上下文。

**不在这里拼模型参数**（temperature / max_tokens / reasoning_effort）——那些来自
``Settings``，由 Orchestrator 填进 ``LLMRequest``。本模块只负责"说什么"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from astra.domain.conversation import Message, Role
from astra.domain.llm import LLMMessage
from astra.domain.memory import Memory
from astra.domain.persona import Persona
from astra.domain.prompt import build_system_prompt
from astra.domain.state import StateSnapshot

#: 只把这两种角色送进 LLM。``messages`` 表还允许 system / tool（M6），
#: 它们不是"当前对话"的一部分。
_CONVERSATION_ROLES = frozenset({Role.USER, Role.ASSISTANT})

#: 降级轮次在消息 metadata 里的标记（``Orchestrator._persist_failure`` 写的）。
_FAILED_STATUS = "failed"


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """上下文窗口预算。"""

    max_history_messages: int
    max_history_chars: int
    #: 记忆层（Layer 5）的字符预算。0 表示不注入记忆。
    memory_max_chars: int = 0


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """裁剪结果。``dropped`` 用于可观测性（``event="llm.history.trimmed"``）。"""

    messages: tuple[Message, ...]
    dropped: int


@dataclass(frozen=True, slots=True)
class ConversationContext:
    system_prompt: str
    messages: tuple[LLMMessage, ...]
    history_window: HistoryWindow


def trim_history(messages: Sequence[Message], *, budget: PromptBudget) -> HistoryWindow:
    """从**最旧**开始按**整条消息**丢弃，直到满足预算。

    - 至少保留一条消息：即使最新那条单独就超出字符预算，也保留它——
      丢掉全部历史比超出预算更糟。
    - 按**字符**而非 token 计预算：本地没有 qwen 的 tokenizer，``tiktoken``
      对 qwen 是错的词表；用错的 tokenizer 做预算比用字符更不诚实。
    """
    kept: list[Message] = []
    total_chars = 0

    for message in reversed(messages):
        if len(kept) >= budget.max_history_messages:
            break
        if kept and total_chars + len(message.content) > budget.max_history_chars:
            break
        kept.append(message)
        total_chars += len(message.content)

    kept.reverse()
    return HistoryWindow(messages=tuple(kept), dropped=len(messages) - len(kept))


def conversation_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
    """挑出该喂给 LLM 的历史，并**整轮剔除降级轮次**。

    降级回复（"我这边的模型服务连不上了…"）是**故障的产物，不是对话内容**。
    把它留在上下文里有两个坏处，都有实测或明确的机理：

    1. **小模型会照抄**。README「已知限制」记录过一个真实故障：早期
       ``llm_provider=none`` 产生的降级回复堆了十几条之后，nemotron-4B 开始
       把"我现在还没有接上语言模型"当成自己的口头禅——因为它是上下文里出现
       最频繁的 assistant 句子。
    2. **内容经常是错的**。文案是按**当时的** ``ErrorCode`` 生成的，而它被写死在
       ``Orchestrator.DEFAULT_ERROR_REPLIES`` 里指向 LM Studio（"你确认一下
       LM Studio 在运行"）。换到第三方 LLM 之后，这句话既过时又误导。

    **用户那一半也一起丢**：一次故障不该在上下文里留下"我问了但没人应"的悬空
    提问——用户真在意就会再说一遍，而重复的提问比缺失的提问更干扰模型。
    数据库里的记录**一条不少**（``docs/04`` 的持久化契约不变），这里只影响
    送给 LLM 的那一份。
    """
    failed_turns = {
        message.turn_id
        for message in messages
        if message.role is Role.ASSISTANT
        # 早期（M1 之前）的消息没有 status：缺省视为正常，不作剔除
        and message.metadata.get("status") == _FAILED_STATUS
    }

    return tuple(
        message
        for message in messages
        if message.role in _CONVERSATION_ROLES and message.turn_id not in failed_turns
    )


def build_conversation_context(
    persona: Persona,
    *,
    history: Sequence[Message],
    user_text: str,
    budget: PromptBudget,
    state: StateSnapshot | None = None,
    memories: Sequence[Memory] = (),
) -> ConversationContext:
    """组装 system prompt 与消息列表。

    当前用户消息**在裁剪之后**追加，因此永远不会被裁掉。

    ``memories`` 由 ``MemoryService`` 检索并按总分排好序——这里不重排，否则
    "同一组输入产出逐字节相同的 prompt" 这条（`docs/10` §2）会依赖两份排序逻辑。
    ``state`` 是 ``StateService`` 读到的快照（已含惰性衰减）。
    """
    window = trim_history(conversation_messages(history), budget=budget)

    messages = [
        LLMMessage(role=message.role, content=message.content) for message in window.messages
    ]
    messages.append(LLMMessage(role=Role.USER, content=user_text))

    return ConversationContext(
        system_prompt=build_system_prompt(
            persona,
            state=state,
            memories=memories,
            memory_max_chars=budget.memory_max_chars or None,
        ),
        messages=tuple(messages),
        history_window=window,
    )
