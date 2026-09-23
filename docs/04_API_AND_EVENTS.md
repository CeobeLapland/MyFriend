# 04 API and Event Protocol

## 1. 原则

HTTP 负责 request/response；WebSocket 负责实时事件流。

所有事件带：

```json
{
  "event_id": "01J...",
  "type": "conversation.message.created",
  "version": 1,
  "occurred_at": "2026-09-23T13:00:00+09:00",
  "correlation_id": "01J...",
  "payload": {}
}
```

## 2. HTTP API

### GET /health

检查：

- API
- Database
- LLM provider reachability
- STT provider availability
- TTS provider availability

### GET /v1/config

返回非敏感运行配置，例如：

```json
{
  "companion_id": "astra",
  "llm_provider": "lmstudio",
  "stt_provider": "sensevoice",
  "tts_provider": "cosyvoice",
  "avatar_enabled": true
}
```

### POST /v1/chat/messages

请求：

```json
{
  "conversation_id": "...",
  "input_type": "text",
  "content": "你好呀"
}
```

返回：

```json
{
  "turn_id": "...",
  "status": "accepted"
}
```

实际结果通过 WebSocket 推送。

### GET /v1/memories

支持：

- type
- min_importance
- created_after
- query
- archived

### DELETE /v1/memories/{id}

必须真正删除事实数据和向量索引。

## 3. WebSocket

连接：

```text
/ws/v1/session/{session_id}
```

客户端发送：

```json
{
  "type": "input.user_text",
  "version": 1,
  "payload": {
    "text": "你今天怎么样？"
  }
}
```

服务器可能推送：

```text
turn.started
memory.retrieval.completed
assistant.text.delta
assistant.text.completed
assistant.state.updated
voice.synthesis.started
voice.audio.chunk
voice.synthesis.completed
avatar.expression.set
avatar.action.play
turn.completed
```

## 4. 推荐事件类型

### Conversation

- `conversation.created`
- `conversation.message.created`
- `turn.started`
- `turn.completed`
- `turn.interrupted`

### Memory

- `memory.candidate.created`
- `memory.created`
- `memory.updated`
- `memory.archived`
- `memory.deleted`
- `memory.retrieved`

### State

- `state.emotion.updated`
- `state.relationship.updated`
- `state.activity.updated`

### Audio

- `audio.input.started`
- `audio.input.stopped`
- `stt.partial`
- `stt.final`
- `tts.started`
- `tts.chunk`
- `tts.completed`

### Avatar

- `avatar.expression.set`
- `avatar.action.play`
- `avatar.look_at.set`
- `avatar.lipsync.start`
- `avatar.lipsync.stop`

### Tool

- `tool.proposed`
- `tool.confirmation.required`
- `tool.started`
- `tool.completed`
- `tool.failed`

## 5. Assistant Response Contract

推荐统一结构：

```json
{
  "reply": "当然可以呀。",
  "emotion": {
    "primary": "happy",
    "intensity": 0.72
  },
  "action": {
    "name": "nod",
    "intensity": 0.45
  },
  "voice": {
    "style": "warm",
    "speed": 1.0,
    "pitch": 0.0
  },
  "memory_candidates": [],
  "state_delta": {
    "happiness": 0.02,
    "energy": 0.00,
    "curiosity": 0.01
  },
  "tool_calls": []
}
```

注意：

- LLM 输出必须通过 schema validation。
- 未知 emotion/action 直接降级为 `neutral` / `idle`。
- state_delta 必须经过允许范围校验。
- tool_calls 必须经过 Tool Registry 校验。

## 6. 事件幂等性

每个 command/event 必须有 `event_id`。

数据库写入和外部执行需要避免同一 event 重复执行。

尤其是：

- 删除记忆。
- 创建任务。
- 发送通知。
- 文件写入。
- 外部 API 调用。
