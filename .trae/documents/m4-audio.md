# M4 Audio — 实施计划

## Context

M0–M3 已完成并逐个真机验收通过。当前质量门：**920 tests passed / ruff check+format 全绿 /
mypy --strict 全绿（61 个源文件）**；迁移到 `0004`，ADR 到 `0020`，`STUB_STAGES` 为空集。

现在实现 **M4「音频」**——`docs/08_IMPLEMENTATION_PLAN.md` 阶段 4：
麦克风采集、VAD、STT adapter、TTS adapter、audio streaming events、interruption。

**验收标准（文档原文）**：

```text
speak -> transcript -> response -> audio
```

同时键盘输入仍然可用。

**这是第一次引入外部服务进程**（CosyVoice3 雨落版，一个 Gradio 应用，
`http://127.0.0.1:7860`），风险结构与前三个里程碑不同。

**已由用户拍板**（本轮，均基于下方实测数据）：

1. **显存策略：LLM 走 CPU，GPU 全给音频模型。** 用户明确"慢没关系，本地跑不嫌慢"，
   并指出**后续可能改成调用第三方 API** —— 所以 STT/TTS 端口必须 provider-agnostic。
2. **不做 LLM 流式**，整段合成（与 ADR 0011 一致）。
3. **音频走独立二进制帧**（不是 base64）—— 协议变更，需要新 ADR。
4. **M4 顺带做 Settings 分组**（ADR 0018/0020 承诺的时点）。

---

## 一、已实测确认的环境事实

| # | 事实 | 影响 |
|---|---|---|
| 1 | **TTS 延迟随显存余量剧烈变化**：单独跑 2.2 / 3.3 / 4.0 s/句；LLM 满 GPU(16k ctx) 共存时 **24.9 / 30.3 / 60.1 s**；LLM 8k ctx 时 10.4 / 11.6 / 29.4 s；LLM 部分 offload 时 4.3 / 6.7 / 7.3 s | LLM 必须让出 GPU，否则 TTS 不可用且越跑越慢 |
| 2 | **LLM 全 CPU：6.2 / 10.2 s/轮**（~50 token 回复）。部分 offload(0.4)：LLM 9.2 s 且 TTS 仍 4.3–7.3 s | **全 CPU 严格优于部分 offload**：TTS 满速、LLM 还更快 |
| 3 | **CosyVoice TTS 输出 24kHz / 单声道 / 32-bit float WAV**（`audioformat=3`） | 必须转 int16 PCM 再传输，否则体积翻倍且客户端要处理浮点 |
| 4 | **SenseVoice STT**：首次 4.9 s（含模型加载），之后 **0.19–0.33 s**，占 ~1 GB | STT 便宜，不是瓶颈 |
| 5 | **Gradio call API 可用纯 httpx 调**：`POST /gradio_api/call/{name}` → `event_id`；`GET .../{event_id}` → SSE `complete`；结果里的 `url` 经 `GET` 可下载（实测 200 / `audio/wav` / 242000 bytes） | **不引入 `gradio_client`**（它拖一堆传递依赖）。adapter 纯 HTTP，天然 provider-agnostic |
| 6 | `nemotron-3-nano-4b` 能跑通完整流水线：合法 JSON、5 字段 profile、归一化生效、状态/记忆子系统都触发，3–5 s/轮 | 换模型无需代码改动；只是 `memory_candidates` 多为空（4B 弱），用户已接受 |
| 7 | GPU 8188 MiB；TTS 4.25 GB + STT ~1 GB + LLM 2.6 GB ≈ 7.8 GB | 三者不能同时常驻 GPU |
| 8 | LM Studio 有 `POST /api/v1/models/unload`（实测可用）与 `/load` | 将来若要"turn 内换出"有路；**M4 不用**，只记进 ADR |
| 9 | `Settings` 用**普通 mixin 基类**拆模块可行：字段被正确收集、`ASTRA_*` 环境变量照常填充、`extra="forbid"` 仍生效 | Settings 分组可以**零 env 契约变更**地做 |

---

## 二、技术选型结论

### 2.1 显存与进程策略（**运行环境配置，不改代码**）

```powershell
# LM Studio：LLM 全走 CPU，把 GPU 整个让给音频模型
lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384
```

- **代码侧不做模型生命周期管理**：那是环境配置，且会耦合 LM Studio 专有 API
  （与 ADR 0001 的 provider-agnostic 原则冲突）。写进 README 与 `.env.example` 注释。
- CosyVoice 雨落版由用户手动启动，常驻 GPU。
- 后果诚实记录：纯文本聊天从 3–5 s 变成 6–10 s。这是 8 GB 的硬约束，不是实现缺陷。

### 2.2 领域端口（改 `domain/ports.py`）

```python
class STTProvider(Protocol):
    async def transcribe(self, audio: bytes, sample_rate: int,
                         language_hint: str | None = None) -> Transcript: ...
    async def health(self) -> ComponentHealth: ...

class TTSProvider(Protocol):
    async def synthesize(self, request: TTSRequest) -> AudioArtifact: ...
    async def health(self) -> ComponentHealth: ...
```

- **按 ADR 0009：TTS 端口是"非流式"的**（`synthesize -> AudioArtifact`），
  不是 `docs/07` §3 写的 `AsyncIterator[bytes]`。文档接口是理想，后端做不到，
  接口必须按真实能力设计。
- 保留一个 `supports_streaming: bool` 能力标记（第三方 API 将来可置 True），
  **M4 不消费它**——只作为"将来换后端不用改端口形状"的锚点。
- `Transcript` / `TTSRequest` / `AudioArtifact` 是 `domain/audio.py` 里的 frozen dataclass。
  `AudioArtifact.pcm` 一律是 **int16 PCM**（采样率写在字段里），
  这样上层永远不碰 float32 / WAV 容器。
- 端口方法声明 `async def`（对调用方的契约），实现内部可以是同步 HTTP。

### 2.3 `providers/cosyvoice/`（新，第一个非 LLM 的 provider）

```
client.py    # httpx 封装 Gradio call API：POST → event_id → GET SSE → url → GET bytes
stt.py       # /prompt_wav_recognition（SenseVoice）
tts.py       # /rainfall_gen_sft（预训练音色，无需参考音频）
```

- **纯 httpx，无新依赖**（实测见 §一 #5）。
- STT 需要**文件**输入 → adapter 内把 int16 PCM 包成 WAV（`infrastructure/audio/codec.py`）。
- TTS 返回 float32 WAV → adapter 内转 int16 PCM 后放进 `AudioArtifact`。
- `voice.speed` 映射到 `speed` 参数。**`voice.style` M4 不消费**（`/rainfall_gen_instruct`
  的 `instruct_text` 期待中文指令如"湖南话"，英文风格词效果未知）——记进 ADR 的"未做"。
- 失败抛 `STTUnavailableError` / `TTSUnavailableError`（新增到 `domain/errors.py`），
  **绝不让 turn 失败**：TTS 挂了仍要返回文本（`docs/10` §4「TTS failure」）。

### 2.4 音频编解码与 VAD（新 `infrastructure/audio/`）

```
codec.py   # float32 WAV → int16 PCM；int16 PCM → WAV 容器；纯 stdlib
vad.py     # 能量阈值 VAD：纯状态机，不碰 I/O，可单测
```

- **不用 `audioop`**：3.13 已移除，虽然 3.12 还在，但新代码不该依赖它。
  用 `array` + `struct` 手工打包。
- **VAD 是"占位实现"**：固定阈值的 RMS 能量检测 + 静音超时自动结束。
  没有降噪、没有自适应噪声门限。`docs/07` §1 只要求管线里有这一环，
  M4 把它做成可关闭的（客户端 push-to-talk 优先）。**不要假装它是正经 VAD。**

### 2.5 二进制帧协议（新 ADR 0021）

**客户端 → 服务端**：

```
JSON  {"type":"input.audio.started","version":1,
       "payload":{"format":"pcm16","sample_rate":16000,"channels":1,
                  "conversation_id":null}}
BIN   <原始 int16 PCM 字节>            ← 可多帧
JSON  {"type":"input.audio.stopped","version":1,"payload":{}}
```

**服务端 → 客户端**：

```
JSON  tts.started   {format, sample_rate, channels, text, chunk_count}
BIN   <int16 PCM 字节>                 ← 可多帧
JSON  tts.completed {duration_ms}
```

- **二进制帧本身不是事件**（没有信封）。它必须紧跟一条说明格式的 JSON 事件。
  没有上下文时收到二进制帧 → 回 `error.protocol`，**不关连接**（沿用既有约定）。
- 接收侧用 `websocket.receive()`，按 `"text"` / `"bytes"` 分派
  （现在的 `receive_text()` 只认文本帧）。
- **事件登记变更**（`domain/events.py`）：新增 `AUDIO_INPUT_STARTED`、
  `AUDIO_INPUT_STOPPED`、`TTS_CHUNK`（保留为"一段音频已推送"的语义占位）、
  `STT_PARTIAL` 登记但 **M4 不发布**（不做流式识别）。
  `docs/04` §4 的 `audio.input.*` / `tts.*` 名字照抄，不自造。
- **总线要能带二进制**：`EventBus` 的队列元素从 `EventEnvelope` 扩成
  `EventEnvelope | BinaryFrame`，新增 `publish_binary()`；`ConnectionManager._send_loop`
  按类型分派 `send_json` / `send_bytes`。去重逻辑只作用于事件（二进制帧没有 `event_id`）。
- **已知局限**：总线是**全局广播**（不按 `session_id` 过滤），
  所以同一时刻多个客户端都会收到音频。单用户本地场景（ADR 0002）可接受，记进 ADR。
- **已知局限**：队满时"淘汰最旧"可能丢掉音频中间帧 → 该段音频不完整。
  本地回环 + 128 深度队列下概率极低；客户端可用 `chunk_count` 自检。记进 ADR 与 README。

### 2.6 输入侧：采集 → VAD → STT → turn

- 采集在**客户端**（浏览器/Unity），服务端只收字节。这与 `docs/07` §1 的管线一致。
- `input.audio.stopped`（push-to-talk 松开）**优先**触发转写；
  VAD 静音超时作为"忘了松手"的兜底。
- 采样率统一 **16 kHz 单声道 int16**（SenseVoice 的输入惯例）。
  服务端按 `input.audio.started` 声明的格式接收，格式不支持 → `error.protocol`。
- 上限：单次utterance 超过 `audio_max_utterance_seconds` 直接截断并记日志（防呆，
  避免客户端 bug 把内存吃满）。
- **`emotion_hint` 只记录、不使用**（`docs/07` §2 明文："STT 的 emotion hint 只能作为
  辅助信号，不得直接覆写 AI 状态"）。M3 刚建好状态系统，这里**要克制**。
  连"记进日志"都要过 `EXTRA_ALLOWLIST` 才看得见。

### 2.7 输出侧：TTS 放在 **turn 内**（改 `application/orchestrator.py`）

M4 之后的完整事件序列：

```text
turn.started
→ conversation.message.created（user）
→ conversation.message.created（assistant）
→ assistant.text.completed
→ avatar.expression.set
→ avatar.action.play
→ tts.started
→ （二进制音频帧，不占事件序列）
→ tts.completed
→ state.emotion.updated          # 仅在该轮情绪确实变化时出现
→ turn.completed
```

- **为什么放 turn 内而不是 turn 后**：`turn.completed` 必须是最后一个（`docs/04` §3
  的既定序列）；放 turn 后会让 TTS 事件排在收尾事件之后，客户端的"这轮结束了"判断会乱。
- 触发条件：`TurnRequest.synthesize_audio=True`。由 WS 层根据会话的
  **voice mode** 决定；HTTP `/v1/chat/turns` 恒为 `False`（调试端点不需要合成）。
- **voice mode 是显式的客户端帧**：`{"type":"session.voice_mode.set","payload":{"enabled":true}}`。
  理由：`docs/11` §2 要求"只有用户显式进 voice mode 才采集"，
  而"该不该合成"是同一个开关的两面。不做"收到音频就自动开"的隐式推断。
- **失败隔离**：TTS 失败只发 `tts.completed{status:"failed", code}`（或直接不发起），
  turn 仍然 `status=ok`——与记忆/状态子系统同一降级模式（ADR 0014）。

### 2.8 interruption（改 `api/routes/chat_ws.py` + `orchestrator.py`）

**必须重构 WS 接收循环**：现在是 `await orchestrator.run_turn(...)` **内联**，
turn 跑的时候根本收不到新帧，barge-in 无从谈起。

```python
# 每个连接一份会话状态
@dataclass
class _SessionState:
    turn_task: asyncio.Task[TurnResult] | None
    voice_mode: bool
    audio: _AudioBuffer | None      # 格式 + 累积的 PCM + VAD 状态
```

- 收到新的**触发输入**（`input.user_text` 或 `input.audio.stopped`）时：
  若 `turn_task` 未完成 → `task.cancel()`，等它退出，再起新 turn。
- **`turn.interrupted` 由 Orchestrator 发布**：`run_turn` 的
  `except asyncio.CancelledError` 分支发事件（带 `turn_id` / `conversation_id`）后
  **重新 raise**，让 task 真正进入 cancelled 状态。
- 二进制音频帧与 `input.audio.started` **不触发**中断（否则第一帧就自我打断）。
- 这**会动每会话串行锁**（HANDOFF §5 点名 M4 必须处理）：取消的 turn 释放锁后，
  新 turn 才能进入。取消不是瞬时的，但 `asyncio.Lock` 的释放与获取是有序的。
- 客户端责任（`docs/10` §7 Scenario D）：收到 `turn.interrupted` 后**立即停止播放**
  已收到的音频。服务端只能停止**继续发送**，发出去的声音收不回来。

### 2.9 健康检查与配置（**契约边界，别踩**）

- **`/health` 的 5 个 key 不可变**（ADR 0006）。`stt` / `tts` 从 `not_configured`
  变成真值 —— **只改值，不改 key 集合**。
  `test_health_api.py::test_llm_stt_tts_are_not_configured` 依赖默认值，
  **显式更新**它（不是放宽断言）。
- 新增 `CosyVoiceHealthProbe`（`providers/cosyvoice/health.py`），
  探测方式是 `GET /gradio_api/info` 是否 200（比试合成便宜得多）。
- `GET /v1/config` 的 `stt_provider` / `tts_provider` 变成真实值
  （`none` → `cosyvoice`）。**key 集合不变**。
- `.env.example` 里预留的注释键（`ASTRA_STT_PROVIDER` / `ASTRA_TTS_PROVIDER` /
  `ASTRA_TTS_BASE_URL`）**取消注释并同步 Settings**。
  `ASTRA_AVATAR_ENABLED` / `ASTRA_TIMEZONE` 仍保持注释（那是 M5）。

### 2.10 Settings 分组（新 `settings/` 包）

**方案（已实测可行，见 §一 #9）：用普通 mixin 基类按域拆模块，字段名与 env 契约完全不变。**

```
settings/
├─ __init__.py     # Settings(LLMMixin, MemoryMixin, StateMixin, AudioMixin, ... , BaseSettings)
├─ base.py         # 运行环境 / 日志 / HTTP / 数据
├─ llm.py          # LLM + Embedding
├─ memory.py       # Memory + 检索权重（含 sum=1.0 validator）
├─ state.py        # State
└─ audio.py        # Audio（M4 新增）
```

- **为什么不嵌套 `BaseSettings` 子模型**：`settings.py` 顶部注释已警告
  "嵌套 BaseSettings 子模型的 env 填充顺序与 `default_factory` 交互存在歧义"。
  mixin 方案保持**单个** `BaseSettings`、**扁平字段名**、`ASTRA_LLM_MODEL` 这类
  env 名一个都不变，零迁移成本。
- `get_settings()` 的 `lru_cache` 与 `Settings(...)` 直接构造的用法都不变。
- 读取点**不改**（`settings.llm_model` 照旧）——这是纯组织性重构，
  不改任何调用方。**这正是它与"嵌套重命名"的关键区别。**

### 2.11 开发者控制台（改 `api/static/dev.html`）

加「语音」面板：voice mode 开关、按住说话按钮、麦克风 → 16 kHz int16 PCM →
WS 二进制帧、收到的音频用 `AudioContext` 播放、`turn.interrupted` 时立刻停播。

**保持零构建、零 npm、零新增 Python 依赖**（ADR 0017）。用 `AudioWorklet` 或
`ScriptProcessorNode` 做重采样——纯浏览器 API，不引任何 CDN。

这是 M4 真机验收的"嘴和耳朵"。

### 2.12 测试策略

沿用既有约定：**不引入 `pytest-asyncio`**（async 用 `asyncio.run()`）；
集成测试用 `tmp_path`（`conftest.py::_guard_default_database` 会断言默认库未被改写）。

```
tests/unit/    test_audio_codec（float32↔int16 / WAV 容器 / 边界与奇数长度）
               test_vad（状态机：静音不触发 / 语音中不触发 / 静音超时触发）
               test_cosyvoice_provider（用 httpx MockTransport 假 Gradio，含 SSE 解析）
               test_event_bus_binary（二进制帧与事件保持顺序 / 去重不作用于二进制）
               test_orchestrator_audio（TTS 失败不影响 turn / synthesize_audio=False 不发起）
tests/contract/ test_binary_frame_protocol（帧顺序：started 必须先于二进制帧）
                test_event_envelope_contract（既有，新事件类型仍满足 6 字段）
tests/integration/ test_ws_audio（TestClient：音频→转写→回复→音频，全链路打桩）
                   test_ws_interruption（turn 跑到一半发新输入 → turn.interrupted + 新 turn）
                   test_health_audio（stt/tts 真值，5 个 key 不变）
```

- **契约测试要有"故意会红"的意识**：新增事件类型会让
  `test_schema_alignment` / `test_event_envelope_contract` 需要显式更新。
- **不测真实 CosyVoice**：集成测试全部打桩（真实服务在真机验收里跑）。
  这是刻意的——CI 不该依赖一个需要 GPU 的外部进程。

---

## 三、目标文件树

```
src/astra/
├─ settings/
│  ├─ __init__.py                 # 新：Settings 组合 + get_settings()
│  ├─ base.py                     # 新：env / log / http / data
│  ├─ llm.py                      # 新：LLM + Embedding mixin
│  ├─ memory.py                   # 新：Memory + 检索权重 mixin
│  ├─ state.py                    # 新：State mixin
│  └─ audio.py                    # 新：Audio mixin（STT/TTS/VAD/格式）
├─ settings.py                    # 删：内容拆进 settings/ 包
├─ logging_setup.py               # 改：EXTRA_ALLOWLIST += 音频相关字段
├─ domain/
│  ├─ audio.py                    # 新：Transcript / TTSRequest / AudioArtifact / AudioFormat
│  ├─ ports.py                    # 改：+STTProvider / TTSProvider
│  ├─ errors.py                   # 改：+STTUnavailableError / TTSUnavailableError / AudioFormatError
│  ├─ events.py                   # 改：+音频事件；+BinaryFrame；+新 ClientFrameType
│  └─ health.py                   # 改：+STT_COMPONENT / TTS_COMPONENT 常量（若缺）
├─ infrastructure/
│  ├─ audio/
│  │  ├─ codec.py                 # 新：float32 WAV ↔ int16 PCM / PCM → WAV 容器
│  │  └─ vad.py                   # 新：能量阈值 VAD 状态机
│  └─ db/                         # 不变（M4 无新表）
├─ providers/cosyvoice/
│  ├─ __init__.py                 # 新
│  ├─ client.py                   # 新：Gradio call API 封装（httpx）
│  ├─ stt.py                      # 新：CosyVoiceSTTProvider
│  ├─ tts.py                      # 新：CosyVoiceTTSProvider
│  └─ health.py                   # 新：CosyVoiceHealthProbe
├─ application/
│  ├─ event_bus.py                # 改：队列支持 BinaryFrame + publish_binary
│  ├─ orchestrator.py             # 改：+TTS 阶段 / synthesize_audio / CancelledError → turn.interrupted
│  └─ (其余不变)
└─ api/
   ├─ connection_manager.py       # 改：send_loop 按类型分派 send_json / send_bytes
   ├─ app.py                      # 改：装配 STT/TTS provider + 探针
   ├─ routes/chat_ws.py           # 改：receive() 分派 / 会话状态 / voice mode / interruption
   ├─ routes/config.py            # 改：stt_provider / tts_provider 真值
   └─ static/dev.html             # 改：+语音面板

docs/decisions/0021-audio-binary-frame-protocol.md   # 新
docs/decisions/0022-audio-provider-boundary-and-vram.md  # 新
```

---

## 四、实施顺序

前置（Windows）：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

| # | 步骤 | 验证 |
|---|---|---|
| 1 | Settings 拆包（mixin，字段名不变） | `pytest tests/unit/test_settings.py -q` + 全量 `pytest -q` |
| 2 | `domain/audio.py` + `domain/errors.py` + `domain/ports.py` | `mypy src` + `pytest tests/unit/test_architecture_rules.py -q` |
| 3 | `infrastructure/audio/codec.py` + tests | `pytest tests/unit/test_audio_codec.py -q` |
| 4 | `infrastructure/audio/vad.py` + tests | `pytest tests/unit/test_vad.py -q` |
| 5 | `providers/cosyvoice/{client,stt,tts}.py` + tests（MockTransport） | `pytest tests/unit/test_cosyvoice_provider.py -q` |
| 6 | `domain/events.py` 音频事件 + `BinaryFrame`；`event_bus.py` 支持二进制 | `pytest tests/contract tests/unit/test_event_bus_binary.py -q` |
| 7 | `connection_manager.py` 分派 + tests | `pytest tests/unit/test_connection_manager.py -q` |
| 8 | `orchestrator.py` TTS 阶段 + `synthesize_audio` + 失败隔离 | `pytest tests/unit/test_orchestrator_audio.py -q` |
| 9 | `chat_ws.py` 接收循环重构（receive 分派 / 会话状态 / voice mode / 音频缓冲） | `pytest tests/integration/test_ws_audio.py -q` |
| 10 | interruption（取消在跑 turn + `turn.interrupted`） | `pytest tests/integration/test_ws_interruption.py -q` |
| 11 | `app.py` 装配 + `providers/cosyvoice/health.py` + `/health` + `/v1/config` | `pytest tests/integration -q` |
| 12 | `.env.example` 同步（取消预留键注释 + 新增字段） | `Copy-Item .env.example .env` 后能启动 |
| 13 | `dev.html` 语音面板 | 手工 |
| 14 | ADR 0021 / 0022 + `docs/04` §3/§4 对齐 + `docs/08` 阶段 4 落点 + README | 人工检查 |
| 15 | 全量质量门 | `ruff check` / `ruff format --check` / `mypy src` / `pytest -q` |
| 16 | 真机端到端验收 | 见 §六 |

---

## 五、依赖变更

**无新增运行时依赖。** Gradio call API 用现有 `httpx` 直接调（实测见 §一 #5），
音频编解码用 stdlib。刻意避开 `gradio_client`——它拖进 `huggingface_hub` /
`websockets` / `aiofiles` 等一串与本项目无关的传递依赖，而我们需要的能力
（POST 拿 event_id、GET SSE、GET 文件）用 httpx 三十行就能覆盖。

---

## 六、验收标准与命令

### 自动化

```
1. uv run pytest -q                              全绿（M3 920 条不回归 + 新增）
2. ruff check / ruff format --check / mypy src    全绿
3. 契约测试：二进制帧必须先有 started；/health 的 5 个 key 集合不变
4. TTS 失败时 turn 仍 status=ok 且文本正常（失败隔离测试）
5. interruption：turn 进行中发新输入 → 收到 turn.interrupted → 新 turn 正常完成
6. 集成测试全部用 tmp_path，不碰 data/astra.db
```

### 真机端到端（验收 5 步）

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 0) 先让 LLM 让出 GPU（8GB 硬约束）
lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader   # 应只剩音频模型

$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_STT_PROVIDER="cosyvoice"; $env:ASTRA_TTS_PROVIDER="cosyvoice"
uv run astra serve

# 1) /health 的 stt / tts 应变成 ok（key 集合仍是 api/database/llm/stt/tts）
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4

# 2) 打开 http://127.0.0.1:8000/dev → 语音面板 → 开 voice mode
# 3) 按住说话："你好呀，今天过得怎么样？"
#    期望：stt.final → turn.started → assistant.text.completed → tts.started
#          → 听到她的声音 → tts.completed → turn.completed
# 4) 她说话说到一半时再按住说话 → 期望：turn.interrupted + 声音立刻停 + 新 turn 开始
# 5) 全程用键盘也能发消息（文本路径不回归）
```

### 延迟记录（`docs/10` §6 要求，至少 p50 / p95）

记录并写进 ADR：STT 延迟、LLM 总时长、TTS 合成时长、turn 总时长。

---

## 七、新增 ADR

| 编号 | 结论 |
|---|---|
| **0021** | **音频二进制帧协议**：(1) 音频走独立 WS 二进制帧，不塞 base64；(2) 二进制帧不是事件、必须紧跟说明格式的 `*.started` 事件；(3) 无上下文的二进制帧 → `error.protocol` 且不关连接；(4) `EventBus` 队列扩成 `事件 \| BinaryFrame`，去重只作用于事件；(5) 已知局限：总线是全局广播（单用户本地假设）、队满淘汰可能丢音频中间帧；(6) `tts.chunk` 不发布（音频在二进制帧里，事件层不需要它） |
| **0022** | **音频 provider 边界与显存策略**：(1) `STTProvider` / `TTSProvider` 端口按**非流式**真实能力设计（ADR 0009），保留 `supports_streaming` 能力标记但不消费；(2) Gradio 后端用纯 httpx 调，**不引入 gradio_client**；(3) 8GB 下 LLM 与 TTS 无法兼得——**LLM 走 CPU**，实测全 CPU 严格优于部分 offload；(4) 模型生命周期归环境配置，代码不碰 LM Studio 专有 API；(5) TTS 放在 turn 内、`turn.completed` 之前；(6) `voice.style` 未映射到 instruct；(7) STT 的 `emotion_hint` 只记录不使用 |

---

## 八、风险点

1. **WS 接收循环重构是本里程碑最大的结构性改动**——现在 `run_turn` 是内联 await，
   改成 task 后，"同一连接的消息保序"这个既有不变量要重新保证。
   缓解：同一连接内仍按顺序处理帧；跨连接顺序仍由 Orchestrator 的每会话锁保证。
2. **interruption 与每会话锁的交互**：取消不是瞬时的。若新 turn 先于旧 turn 完成
   unwind 就抢锁，可能出现两个 turn 交错。
   缓解：取消后 `await` 旧 task 退出，再起新 turn；并有集成测试锁住。
3. **"不重复的最终回复"**（`docs/10` §7 Scenario D）：被取消的 turn 若已经落了
   消息事件，客户端会看到"半截回复 + 新回复"。
   缓解：TTS 排在持久化之后，取消发生在 TTS 阶段时消息已经写好了——
   这是**正确**的（用户确实收到了那句话）。ADR 里写清楚这个语义。
4. **二进制帧与事件的顺序**：二者在同一队列里，顺序天然保持。
   但"淘汰最旧"可能只丢二进制帧不丢事件 → 音频残缺而事件完整。
   缓解：客户端可用 `tts.started.chunk_count` 自检；本地回环下概率极低。记进 README。
5. **显存策略是环境配置，不是代码保证**——用户忘了 `--gpu off` 时，
   TTS 会慢到 10–30 s 而**代码不会报错**。
   缓解：README 与 `.env.example` 显著位置写明；`/health` 无法检测这一点
   （LM Studio 不暴露"模型在 GPU 还是 CPU"给 OpenAI 兼容接口）。
   诚实记进 README 的"已知限制"。
6. **VAD 是占位实现**，固定阈值在嘈杂环境会误判。
   缓解：可关闭，客户端 push-to-talk 优先。README 标注。
7. **`/health` 的 5 个 key 不可变**（ADR 0006）——只改 `stt`/`tts` 的值。
   `test_health_api` 需要**显式更新**，不是放宽断言。
8. **Settings 拆包必须零行为变更**：字段名、默认值、validator、env 名一个都不能动。
   缓解：拆完后先跑全量 `pytest -q`（920 条不回归），再动别的。
9. **新增 Settings 字段必须同步 `.env.example`**，否则 `Copy-Item .env.example .env`
   后 `extra="forbid"` 会让启动直接失败（M0/M1/M2 都踩过）。
10. **新日志 `extra` 字段必须进 `EXTRA_ALLOWLIST`**，否则日志里看不到（M0 的 schema 闸门）。
11. **原始音频绝不进日志**（`docs/11` §6）：`SENSITIVE_KEYS` 已含 `audio`，
    新增字段仍要过白名单。日志只记**长度/时长**，不记内容。
12. **麦克风默认关闭**（`docs/11` §2）：只有显式 voice mode 才采集。
13. **新增集成测试必须用 `tmp_path`**，否则 `_guard_default_database` 会红。
14. **`docs/01` §2 把"语音打断"标为 P2**，而 `docs/08` 阶段 4 把 interruption
    列为 M4 范围——两处文档冲突。
    缓解：按 `docs/08` + `docs/10` §7 Scenario D 做（用户已知悉并选择 M4 做），
    ADR 记录这个取舍。

---

## 九、待用户确认的点

1. **TTS 预训练音色用 `开拓者`**（雨落版目前唯一列出的 SFT 音色）。
   若你想用别的音色（zero-shot 克隆需要一段 ≤30 s 参考音频），告诉我。
2. **voice mode 做成显式开关**（客户端发 `session.voice_mode.set`），
   而不是"收到音频就自动开"。理由是 `docs/11` §2 要求显式。是否认可？
3. **`docs/01` §2 把语音打断标为 P2**，本计划按 `docs/08` 在 M4 做（含在验收里）。
   如果希望缩小 M4，可以把 interruption 挪到后面——但那会让 `docs/10` §7 Scenario D 缺失。
4. **接受"纯文本聊天从 3–5 s 变成 6–10 s"**作为 LLM 走 CPU 的代价。
   （你已说"慢没关系"，这里再确认一次，因为它影响的是**所有**对话，不只语音。）
