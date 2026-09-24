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

`stt_provider` / `tts_provider` 的值来自 `ASTRA_STT_PROVIDER` / `ASTRA_TTS_PROVIDER`
（M4 起；之前是写死的 `"none"`）。**key 集合不变**——客户端从 M0 起就能写死解析。

### POST /v1/conversations

**结束当前会话并立即开一段新的**（M4 追加，见 `decisions/0023`）。

请求体为空。返回 **201**：

```json
{"conversation_id": "01J...", "started_at": "2026-09-24T13:00:00+00:00"}
```

- 旧会话只被置为 `ended`，**不删除任何数据**——历史、记忆、状态都还在，
  `GET /v1/conversations/{id}/messages` 照常可读。
- 没有它的话，一段被写坏的历史只能靠 60 分钟空闲超时才能脱身。

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

M4 起还支持三种帧（音频字节本身走二进制帧，见 §3.2）：

```json
{"type": "input.audio.started", "version": 1,
 "payload": {"encoding": "pcm16", "sample_rate": 16000, "channels": 1,
             "conversation_id": null}}
{"type": "input.audio.stopped", "version": 1, "payload": {}}
{"type": "session.voice_mode.set", "version": 1, "payload": {"enabled": true}}
```

`session.voice_mode.set` 是**显式开关**：`docs/11` §2 要求只有用户显式进 voice
mode 才采集麦克风，而"这一轮该不该把回复合成成语音"是同一个开关的另一面。

服务器可能推送：

```text
conversation.created
turn.started
conversation.message.created
assistant.text.delta
assistant.text.completed
state.emotion.updated
state.relationship.updated
memory.retrieval.completed
voice.synthesis.started
voice.audio.chunk
voice.synthesis.completed
avatar.expression.set
avatar.action.play
turn.completed
turn.failed
error.protocol
```

一轮完整对话的实际序列（M1b 起，新会话的第一轮会多一条 `conversation.created` 打头；
M3 起多一条 `state.emotion.updated`，排在回复相关事件**之后**——状态与记忆都是
事后记账，不该拖慢回复）：

```text
turn.started
→ conversation.message.created（user）
→ conversation.message.created（assistant）
→ assistant.text.completed
→ avatar.expression.set
→ avatar.action.play
→ state.emotion.updated          # 仅在该轮情绪确实变化时出现
→ turn.completed
```

> `assistant.state.updated` 是 M1 时的泛化名，**已由上面两个具体名取代**
> （它从未被发布过）。见 `decisions/0020-m3-state-model.md`。

`error.protocol` 只在客户端发来非法帧时出现，且**不会关闭连接**。

> M1 不做流式，所以没有 `assistant.text.delta`——它在 P1 加流式时启用。
> 当前 `assistant.text.completed` 一次给出完整 `reply`。

### 3.1 语音模式下的序列（M4 起）

客户端显式开启 voice mode（`session.voice_mode.set {enabled:true}`）之后，
turn 中间会插入语音事件：

```text
turn.started
→ conversation.message.created（user）
→ conversation.message.created（assistant）
→ assistant.text.completed
→ avatar.expression.set
→ avatar.action.play
→ tts.started
→ （若干二进制音频帧，不占事件序列）
→ tts.completed
→ state.emotion.updated
→ turn.completed
```

**`tts.started` 携带格式**（`encoding` / `sample_rate` / `channels` /
`chunk_count` / `duration_ms`）：客户端必须先知道格式才能解释紧随其后的
二进制帧，所以它排在**合成之后、推流之前**。`tts.completed` 的 `chunk_count`
可用于自检有没有丢帧。

### 3.2 音频二进制帧（M4 起）

音频**不走 JSON payload**，而是独立的 WebSocket 二进制帧（见
`decisions/0021-audio-binary-frame-protocol.md`）。

**客户端 → 服务端**：

```text
JSON  {"type":"input.audio.started","version":1,
       "payload":{"encoding":"pcm16","sample_rate":16000,"channels":1,
                  "conversation_id":null}}
BIN   <原始 int16 PCM 字节>            ← 可多帧
JSON  {"type":"input.audio.stopped","version":1,"payload":{}}
```

**服务端 → 客户端**：`tts.started` 之后跟着若干二进制帧，然后 `tts.completed`。

约束：

- 二进制帧**不是事件**，没有信封，也不进幂等去重；
- **没有上下文时收到二进制帧 → `error.protocol`**（只报一次），且不关连接；
- 服务端**不做重采样**：`input.audio.started` 声明的格式与
  `ASTRA_AUDIO_INPUT_SAMPLE_RATE` / `ASTRA_AUDIO_INPUT_CHANNELS` 不符会被拒绝；
- 客户端在收到 `turn.interrupted` 后必须**立即停止播放**已收到的音频——
  服务端只能停止*继续发送*。

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

- `audio.input.started` —— M4 实现
- `audio.input.stopped` —— M4 实现
- `stt.partial` —— **未实现**（M4 不做流式识别，也不发布它）
- `stt.final` —— M4 实现
- `tts.started` —— M4 实现
- `tts.chunk` —— **未实现**（音频在二进制帧里，事件层不需要它，见 ADR 0021）
- `tts.completed` —— M4 实现

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
