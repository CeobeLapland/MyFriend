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
  "conversation_id": "...",
  "status": "accepted"
}
```

实际结果通过 WebSocket 推送。

> `conversation_id` 是响应体里的补充字段：请求里带了它而响应不给回来，客户端
> 就无从续话。见 `decisions/0016-m1-protocol-additions.md`。

### POST /v1/chat/turns

**同步**跑完整个 turn，直接返回完整结构化响应。用于调试、脚本化验收，以及
不需要实时事件流的客户端。

请求：

```json
{
  "content": "你好，今天陪我聊聊天吧。",
  "conversation_id": null,
  "input_type": "text"
}
```

返回（HTTP 状态码**恒为 200**）：

```json
{
  "turn_id": "...",
  "conversation_id": "...",
  "status": "ok",
  "reply": "好啊，正好我也刚想找个话题聊聊。",
  "response": {
    "reply": "...",
    "emotion": {"primary": "happy", "intensity": 0.72},
    "action": {"name": "nod", "intensity": 0.65},
    "voice": {"style": "warm", "speed": 1.1, "pitch": -0.2}
  },
  "failure": null,
  "model": "qwen/qwen3.5-9b",
  "retries": 0,
  "degraded": false,
  "timings": {"total_ms": 8900.0, "llm_ms": 8888.0}
}
```

LLM 不可用时同样是 200，但 `status` 为 `failed`、`failure.code` 给出稳定错误码、
`reply` 是一句诚实的中文：

```json
{
  "status": "failed",
  "reply": "我这边的模型服务连不上了。你确认一下 LM Studio 在运行，然后再说一次好吗？",
  "failure": {"code": "llm_unavailable", "message": "...", "recoverable": true}
}
```

### GET /v1/config

返回非敏感运行配置与**协议词表**：

```json
{
  "companion_id": "astra",
  "user_id": "local",
  "llm_provider": "lmstudio",
  "llm_model": "qwen/qwen3.5-9b",
  "stt_provider": "none",
  "tts_provider": "none",
  "avatar_enabled": false,
  "emotions": ["angry", "embarrassed", "excited", "happy", "neutral", "sad", "sleepy", "surprised"],
  "actions": ["idle", "laugh", "look_at_user", "look_away", "nod", "shake_head", "shy", "stretch", "think", "wave", "yawn"],
  "voice_styles": ["calm", "cheerful", "excited", "gentle", "neutral", "playful", "serious", "warm"]
}
```

词表必须从这里取——它是与 Unity 端的协议契约，代码侧有单一真相
（`src/astra/domain/vocabulary.py`，见 `decisions/0015`）。

### GET /v1/conversations/{conversation_id}/messages

按 **id 升序**返回该会话的全部消息。未知会话返回 **404**。

```json
{
  "conversation_id": "...",
  "messages": [
    {"id": "01J...", "conversation_id": "...", "turn_id": "...", "role": "user",
     "content": "你好", "created_at": "2026-09-24T03:45:50+00:00", "metadata": {}}
  ]
}
```

> 排序用 `id` 而不是 `created_at`：本机时钟分辨率只有 15.625ms，同一 turn 的
> 两条消息时间戳完全相同。见 `decisions/0012-id-and-identity-model.md`。

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
- `turn.failed`
- `turn.interrupted`

> `turn.failed` 是独立事件而不是 `turn.completed{status:"failed"}`：客户端普遍用
> `turn.completed` 收尾 UI，让它收到失败状态会误导。命名与 `tool.failed` 对齐。

### Protocol

- `error.protocol` —— 客户端发来非法帧时回一条，**连接不关闭**（一个手滑不该掉会话）

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
