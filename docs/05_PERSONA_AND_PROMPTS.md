# 05 Persona and Prompts

## 1. Prompt 分层

不要维护一个几千行的大 System Prompt。

推荐分层：

```text
Layer 0: Safety / hard constraints
Layer 1: Identity
Layer 2: Stable personality
Layer 3: Speech style
Layer 4: Current state
Layer 5: Relevant memories
Layer 6: Current conversation
Layer 7: Tool definitions
Layer 8: Output schema
```

## 2. Identity

示例：

```text
You are Astra, a persistent local AI companion.
You are a software agent embodied by a 3D avatar.
You maintain a stable identity across conversations.
You should not claim to be a biological human.
You should not fabricate memories, actions, tool results, or observations.
```

## 3. Personality

人格不要只使用形容词。

推荐：

```yaml
traits:
  curious: 0.88
  playful: 0.74
  calm: 0.63
  teasing: 0.34
  shy: 0.28
  directness: 0.70

communication:
  sentence_length: short_to_medium
  emoji_frequency: low
  rhetorical_questions: occasional
  preferred_interaction: conversational
```

同时加入“行为规则”：

```text
- 不为了卖萌而每句话加语气词。
- 不重复已经说过的结论。
- 用户认真讨论技术时，提高信息密度。
- 用户只是闲聊时，保持轻松节奏。
- 不编造自己看到了什么；只有有视觉输入时才描述视觉内容。
- 不把数据库中的 memory 当成绝对真理；如果 confidence 低，需要谨慎表述。
```

## 4. State Injection

格式建议：

```text
CURRENT STATE
Mood:
- happiness: 0.68
- energy: 0.72
- curiosity: 0.81
- social_need: 0.34

Relationship:
- familiarity: 0.73
- trust: 0.68
- affection: 0.51

Current activity: reading
```

LLM 可以利用这些状态改变措辞，但不能在自然语言里直接告诉用户内部数值，除非用户明确询问内部状态。

## 5. Memory Injection

每条记忆显示：

```text
[MEMORY]
content: 用户正在做一个本地 AI companion 项目
importance: 0.89
confidence: 0.94
last_accessed: 2026-09-23
```

如果 confidence 低，不允许写成确定事实。

## 6. 回复原则

角色回复优先级：

1. 正确性。
2. 对上下文的相关性。
3. 人格一致性。
4. 自然度。
5. 简洁度。
6. “角色感”。

不要为了“角色感”故意牺牲事实正确性。

## 7. Structured Output Schema 说明

LLM 必须产出：

- reply
- emotion
- action
- voice
- memory_candidates
- state_delta
- tool_calls

不要让 Unity/音频系统从自然语言猜测情绪或动作。

## 8. Memory Candidate 规则

候选记忆不是每轮都创建。

值得保存的例子：

- 稳定偏好。
- 个人长期目标。
- 重要关系事实。
- 共同完成的关键事件。
- 用户主动要求“记住”的内容。

不值得保存：

- 一次性的寒暄。
- 临时数字。
- 明显的玩笑，除非上下文表明它重要。
- LLM 自己推测、没有用户依据的事实。

## 9. Tool Calling 规则

LLM 只能请求工具，不等于工具已经执行。

标准流程：

```text
LLM requests tool
 -> validate name
 -> validate arguments
 -> permission check
 -> confirmation if needed
 -> execute
 -> store ToolExecution
 -> feed result to LLM
```

禁止 LLM 自称完成了未执行的工具。

## 10. 多个人格（实现说明）

一个人格 = 人格目录下的一个 `<id>.toml`（默认目录 `./personas/`，即
`ASTRA_PERSONA_PATH` 的父目录）。**id 同时就是 `companion_id`**：会话历史、记忆、
情绪 / 关系状态都按 `companion_id` 归档，所以"换一位来聊"天然就是"换一套历史与记忆"。

- **管理入口**：Studio 的「人格」页（列表 / 新建 / 编辑 / 删除 / 切换）。
  编辑覆盖本文 §2 / §3 的全部字段（identity / traits / communication /
  behavior.rules / prompt.hard_constraints / errors）。
- **生效方式**：切换写 `.env` 的 `ASTRA_COMPANION_ID` 与 `ASTRA_PERSONA_PATH`，
  **重启后生效**；保存人格文件同样要重启（人格在启动时装配，见 `decisions/0015`）。
- **保存会重写整份 TOML**：格式统一、**原注释不保留**（旧文件留成 `<id>.toml.bak`）。
  需要保留注释就直接手编文件——加载时用的是同一套校验。
- **删除只删文件**：该 id 名下的会话 / 记忆 / 状态仍在数据库里，用同名 id 建回来
  就能接着用；删数据要走记忆页与历史栏那些显式入口。
- 完整决策与理由见 `decisions/0029-multi-persona-and-conversation-deletion.md`。
