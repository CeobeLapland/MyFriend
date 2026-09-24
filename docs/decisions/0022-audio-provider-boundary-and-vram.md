# ADR 0022 — 音频 provider 边界与 8 GB 显存策略

## Decision

### 1. `STTProvider` / `TTSProvider` 端口按**非流式**的真实能力设计

`docs/07` §3 写的接口是 `synthesize_stream -> AsyncIterator[bytes]`，
目标是"首包即播"。但首选后端（CosyVoice3 雨落版）是 Gradio 应用，
接口是**文件级**的：它返回一个生成完毕的 `.wav`，做不到流式。

所以端口是：

```python
class STTProvider(Protocol):
    async def transcribe(audio: bytes, sample_rate: int,
                         language_hint: str | None = None) -> Transcript: ...

class TTSProvider(Protocol):
    async def synthesize(request: TTSRequest) -> AudioArtifact: ...
```

**不引入 `supports_streaming` 之类的能力开关**：M4 没有任何消费者，
留一个没人读的标志就是死代码。将来真接入流式后端时**扩端口**，而不是提前留位。

### 2. Gradio 后端用**纯 httpx** 调，不引入 `gradio_client`

实测确认那条路只要三步：

```text
POST /gradio_api/call/{api_name}   {"data":[...]}   → {"event_id": "..."}
GET  /gradio_api/call/{api_name}/{event_id}         → SSE，complete 事件里是结果
GET  <结果里的 url>                                  → 文件字节
POST /gradio_api/upload                             → 服务端路径（STT 需要，它收文件）
```

三十行 httpx 就够，而 `gradio_client` 会拖进 `huggingface_hub` / `aiofiles` /
`websockets` 一串与本项目无关的传递依赖。**M4 无新增运行时依赖。**

额外好处是**天然 provider-agnostic**：音频的输入输出都是"HTTP 上传/下载字节"，
不依赖"输出目录在本地磁盘上"。将来换成第三方云 API（用户已明确可能这么做），
只需新写一个 provider 包。

### 3. 8 GB 显存下 **LLM 走 CPU**，GPU 整个让给音频模型

这是 M4 最硬的约束，全部有实测数据：

| 配置 | 显存占用 | TTS 每句 |
|---|---|---|
| TTS 单独 | 4.25 GB | 2.2 / 3.3 / 4.0 s |
| LLM 满 GPU（16k ctx）+ TTS | 7.6 GB | **24.9 / 30.3 / 60.1 s** |
| LLM 满 GPU（8k ctx）+ TTS | 7.8 GB | 10.4 / 11.6 / 29.4 s |
| LLM 部分 offload（0.4）+ TTS | 6.1 GB | 4.3 / 6.7 / 7.3 s（LLM 掉到 9.2 s） |
| LLM 全 CPU + TTS | — | 2–4 s（LLM 11–21 s） |

结论：**LLM 与 TTS 无法同时常驻 GPU 且都快**，而且显存越紧 TTS 越慢
（越跑越慢，是压力累积）。**全 CPU 严格优于部分 offload**——TTS 满速。

```powershell
lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384
```

**代价（真机验收实测，比早期估计更重）**：LLM 走 CPU 后，带完整人格 prompt 的
一轮是 **11–21 秒**，而且**随上下文增长明显变慢**（实测同一句话在会话第二轮
从 11.3 s 涨到 21.4 s）。早先测到的"6–10 s"用的是极简 prompt，不代表真实负载。

即便如此全 CPU 仍是最优解：LLM 满 GPU 时一轮语音是 `3–5 s + 10–30 s`，
比 `11–21 s + 2–4 s` 更慢**且不稳定**。用户已明确"慢没关系"。

> 想要更快的文本聊天，唯一干净的办法是**在不用语音时关掉 CosyVoice 进程**，
> 再把 LLM 放回 GPU。代码侧不做这种模式切换（见下方第 4 条）。

### 4. 模型生命周期归**环境配置**，代码不碰 LM Studio 专有 API

LM Studio 有 `POST /api/v1/models/unload`（实测可用），所以"turn 内把 LLM
换出去给 TTS 腾显存"技术上是可行的。**M4 不这么做**：

- 那会耦合 LM Studio 的专有 API，与 ADR 0001 的 provider-agnostic 原则冲突；
- 每轮多一次加载（实测 ~3 s）且要处理换回时机，收益与复杂度不成比例。

### 5. TTS 放在 turn 内、`turn.completed` **之前**

`docs/04` §3 的既定序列要求 `turn.completed` 收尾。所以语音事件是 turn 的一部分，
不是事后的旁支。由 `TurnRequest.synthesize_audio` 触发——只有**显式开了
voice mode** 的连接才合成；HTTP 调试端点恒为 `False`（不必白等 2–4 秒）。

### 6. `voice.style` 未映射到 instruct

`AssistantResponse.voice.style` 已经免费产出（`warm` / `gentle` / ...），但
`/rainfall_gen_instruct` 的 `instruct_text` 期待的是"湖南话"这类**中文指令**，
英文风格词的效果没有实测过。M4 只把 `voice.speed` 映射到 `speed` 参数，
**style 字段照常发给客户端（Unity 端可能用得上），但不喂给 TTS**。

### 7. STT 的 `emotion_hint` 只记录、不使用

`docs/07` §2 明文："STT 的 emotion hint 只能作为辅助信号，不得直接覆写 AI 状态"。
M3 刚建好状态系统，这里很容易顺手接上去——所以：

- `Transcript.emotion_hint` 只是个普通字段，**没有任何自动接入状态系统的通路**；
- 调用点（`Orchestrator`）刻意不读它；
- 它出现在 `stt.final` 事件里（对客户端透明），但日志里只记
  `has_hint` 布尔值，不记内容。

### 8. VAD **默认关闭**

`docs/08` 阶段 4 列了 VAD，所以它实现了、也有单测——但**默认不开**。
这是 M4 真机验收打回来的一处默认值。

实测：把 CosyVoice 合成的"你好呀，今天过得怎么样？"当麦克风输入推给服务端，
VAD 在**逗号处的自然停顿**就判定了"说完了"，只送了 1.1 秒音频给 STT，
转写结果变成 `一や。` 这种乱码。

既然 M4 的输入路径本来就是 push-to-talk（客户端一定会发
`input.audio.stopped`），开着 VAD 只会**主动截断**正常对话。所以默认关闭，
需要"免按键"时再打开——届时它需要的是正经 VAD（降噪 + 自适应门限），
而不是把 `vad_silence_ms` 调大。

**关闭只关掉"自动结束"，不能把"有没有语音"一起关掉**：`has_speech` 是调用方
判断"要不要送去识别"的依据。第一版实现里 `feed()` 在关闭时直接 `return False`，
导致所有语音都被当成"全程静音"丢掉——集成测试（8 条）全部超时才发现。

## Reason

**为什么接受"端口与文档不符"**：ADR 0009 已经定案——接口必须按真实能力设计，
不能假装能流式。写一个永远只 yield 一次的 `AsyncIterator` 是自欺。

**为什么 TTS 失败不让 turn 失败**：`docs/10` §4「TTS failure」要求
"TTS unavailable → text response still arrives"。所以 `_synthesize_speech`
捕获 `AstraError`，只发一条 `tts.completed{status:"failed", code}`。

## Consequences

- `/health` 的 `stt` / `tts` 从 `not_configured` 变成真值——**只改值，不改
  key 集合**（ADR 0006）。探针打 `GET /gradio_api/info`，不触发任何模型推理
  （真跑一次 TTS 要 2–30 s，`/health` 是要被频繁调用的）。
- `ASTRA_TTS_BASE_URL` 的默认值是雨落版的地址；输出目录用系统临时目录并在
  下载后删除——**不把机器专属的绝对路径写进配置**。
- **已知局限：显存策略是环境配置，代码无法检测**。忘记 `--gpu off` 时 TTS 会
  慢到 10–30 s/句，而 `/health` 不会报错（LM Studio 的 OpenAI 兼容接口不暴露
  "模型在 GPU 还是 CPU"）。写进 README 的"已知限制"。
- 音频格式统一为 **int16 PCM**：TTS 后端实际返回 32-bit float WAV（实测
  `audioformat=3`，stdlib `wave` 读不了），转换在
  `infrastructure/audio/codec.py` 里做，上层永远只见 int16。
