# 10 Testing and Acceptance

## 1. Test Pyramid

```text
        E2E / manual UX
       /              \
  contract tests     integration
      /                    \
           unit tests
```

## 2. Unit Test Areas

### Persona

- prompt assembly is deterministic for same state.
- hidden state is not leaked unless explicitly requested.
- personality config merges correctly.

### Memory

- candidate filtering.
- importance scoring.
- deduplication.
- retrieval ranking.
- archive/delete behavior.

### State

- values always remain in [0,1].
- deltas are bounded.
- decay works.
- event-to-state transitions are deterministic.

### Tools

- schema validation rejects invalid arguments.
- blocked tools cannot execute.
- confirmation-required tools stop before execution.

## 3. Contract Tests

### LLM

Given a fixed mocked provider response, ensure:

```text
JSON parses
schema validates
unknown actions are normalized
state delta is clamped
```

### WebSocket

Ensure all event envelopes contain:

- event_id
- type
- version
- occurred_at
- correlation_id
- payload

### Unity

Mock events such as:

```text
avatar.expression.set
avatar.action.play
avatar.lipsync.start
```

Verify the client maps them to expected internal commands.

## 4. Integration Tests

### Restart persistence

```text
send message
shutdown backend
restart backend
query history
=> message exists
```

### Memory persistence

```text
create memory
restart
retrieve memory
=> same memory available
```

### Provider failure

```text
LM Studio unavailable
=> health marks degraded
=> API does not crash
=> user gets a clear recoverable error
```

### TTS failure

```text
TTS unavailable
=> text response still arrives
=> avatar may lip-sync disabled
```

### Unity failure

```text
Unity disconnected
=> AI text chat remains usable
```

## 5. Behavioral Evaluation

建立一组固定对话集，例如 50~100 条 turn：

- ordinary chat
- technical discussion
- emotional conversation
- memory recall
- conflicting memories
- tool use
- refusal of unsafe action
- interruption
- long context

比较版本时关注：

- persona consistency
- factual accuracy
- memory precision
- inappropriate memory creation rate
- tool correctness
- latency

## 6. Latency Metrics

记录：

```text
STT endpoint latency
LLM TTFT
LLM total generation time
TTS first-audio latency
TTS total synthesis time
turn total latency
```

不要只看平均值；至少记录 p50 / p95。

## 7. MVP 验收场景

### Scenario A — text chat

用户输入：

```text
你好，今天陪我聊聊天吧。
```

Expected：

- 文本响应。
- 正确人格风格。
- 生成合法 structured response。
- 无错误日志。

### Scenario B — memory

Turn 1：

```text
我最近在做一个本地 AI 伙伴项目，请记住这个。
```

Turn 2：

```text
下次提醒我继续做这个项目。
```

Expected：

- memory exists after restart。
- relevant retrieval appears in context。

### Scenario C — avatar

User asks a playful question。

Expected：

- text response
- emotion event
- action event
- Unity receives valid events

### Scenario D — interruption

TTS speaking while user starts talking。

Expected：

- TTS stops quickly。
- new turn starts。
- no duplicated final response。

## 8. Quality Gates

每个 milestone 不仅要“能跑”，还需要：

- automated tests pass
- no critical unresolved exception
- documented configuration
- failure mode tested
- rollback/restart behavior verified
