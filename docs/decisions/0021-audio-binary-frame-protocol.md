# ADR 0021 — 音频二进制帧协议

## Decision

### 1. 音频走独立的 WebSocket **二进制帧**，不塞 base64

`docs/04` §1 的信封是纯 JSON（6 个必备字段）。音频字节如果 base64 塞进
`payload`，体积 +33%，而且每次都要 JSON 序列化一大坨字符串。

代价是客户端必须能区分文本帧与二进制帧——但那本来就是 WebSocket 的原生能力
（浏览器侧只是设一下 `binaryType = "arraybuffer"`），没有引入新的复杂度。

### 2. 二进制帧**不是事件**，它必须紧跟一条说明格式的 JSON 帧

```text
客户端 → 服务端
  JSON  {"type":"input.audio.started","version":1,
         "payload":{"encoding":"pcm16","sample_rate":16000,"channels":1,
                    "conversation_id":null}}
  BIN   <原始 int16 PCM 字节>            ← 可多帧
  JSON  {"type":"input.audio.stopped","version":1,"payload":{}}

服务端 → 客户端
  JSON  tts.started   {encoding, sample_rate, channels, text, chunk_count, duration_ms}
  BIN   <int16 PCM 字节>                 ← 可多帧
  JSON  tts.completed {turn_id, status, chunk_count, duration_ms}
```

二进制帧没有 `event_id` / `type` / `version` 信封，因此：

- **不进幂等去重**（`docs/04` §6 的幂等性针对的是事件，去重靠 `event_id`）；
- **没有上下文时收到二进制帧 → `error.protocol`，且不关连接**（沿用既有约定）。

### 3. 事件的顺序由"共用同一个队列"保证

`EventBus` 的队列元素从 `EventEnvelope` 扩成 `EventEnvelope | BinaryFrame`
（`ServerFrame`），新增 `EventBus.publish_binary()`。分成两条通道就保证不了
`tts.started` → 音频帧 → `tts.completed` 的顺序。

### 4. 所有出站帧统一走总线，**包括 `error.protocol`**

M4 之前协议错误是直接 `websocket.send_json` 的，而那会与 `ConnectionManager`
的 sender task **并发写同一个 socket**。音频帧一多，两个协程在 `await` 点交错
就可能把帧写坏。统一走总线后，每条连接只有一个发送者。

副作用：`error.protocol` 现在是**广播**的（总线本来就是全局广播，
不按 `session_id` 过滤）。单用户本地场景（ADR 0002）可接受。

### 5. 不发布 `tts.chunk`

`docs/04` §4 列了 `tts.chunk`，但音频已经在二进制帧里了，事件层不需要它。
`HANDOFF` §6.3 的要求是"按实际用到的补齐"——注册一个永远不发布的类型
就是死登记（ADR 0020 已经因为同样的理由移除过 `assistant.state.updated`）。

## Reason

**为什么不按 `session_id` 过滤总线**：总线从 M1b 起就是全局广播，改成定向投递
要动 `ConnectionManager` 的订阅模型与所有事件生产方。这是单用户本地应用，
收益不足以支撑那次重构。记进"已知局限"。

**为什么 `tts.started` 排在合成之后**：它携带 `encoding` / `sample_rate` /
`channels`，而客户端**必须先知道格式才能解释紧随其后的二进制帧**。
格式只有合成完才知道（provider 可能换采样率），所以事件只能排在合成之后。

## Consequences

- **已知局限：队满时"淘汰最旧"可能丢掉音频中间帧**，让一段音频不完整而事件
  依然完整。本地回环 + 128 深度队列下概率极低；客户端可用
  `tts.started.chunk_count` 与实收帧数自检（开发者控制台就做了这个检查）。
- **已知局限：总线是全局广播**，多客户端同时在线时都会收到音频。
- **`error.protocol` 的 `code` 不再只有 `invalid_frame`**：音频后端失败会带
  `stt_unavailable` / `tts_unavailable` 这类稳定错误码（来自 `ErrorCode`），
  客户端可以据此分支。消息文案只给"几条校验错误"，不泄漏 pydantic 的原始结构。
- 客户端责任：收到 `turn.interrupted` 后**立即停止播放**已收到的音频。
  服务端只能停止*继续发送*，推出去的声音收不回来（`docs/10` §7 Scenario D）。
- 新增契约测试：帧顺序（`started` 必须先于二进制帧）、无上下文的二进制帧、
  超长帧、不支持的格式；`test_event_bus.py` 里锁住"事件与二进制帧共队列保序"。
