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

#: 只把这两种角色送进 LLM。``messages`` 表还允许 system / tool（M6），
#: 它们不是"当前对话"的一部分。
_CONVERSATION_ROLES = frozenset({Role.USER, Role.ASSISTANT})


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


def build_conversation_context(
    persona: Persona,
    *,
    history: Sequence[Message],
    user_text: str,
    budget: PromptBudget,
    memories: Sequence[Memory] = (),
) -> ConversationContext:
    """组装 system prompt 与消息列表。

    当前用户消息**在裁剪之后**追加，因此永远不会被裁掉。

    ``memories`` 由 ``MemoryService`` 检索并按总分排好序——这里不重排，否则
    "同一组输入产出逐字节相同的 prompt" 这条（`docs/10` §2）会依赖两份排序逻辑。
    """
    window = trim_history(
        [message for message in history if message.role in _CONVERSATION_ROLES],
        budget=budget,
    )

    messages = [
        LLMMessage(role=message.role, content=message.content) for message in window.messages
    ]
    messages.append(LLMMessage(role=Role.USER, content=user_text))

    return ConversationContext(
        system_prompt=build_system_prompt(
            persona,
            memories=memories,
            memory_max_chars=budget.memory_max_chars or None,
        ),
        messages=tuple(messages),
        history_window=window,
    )
