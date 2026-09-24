# Astra

Local-first AI companion：长期记忆、稳定人格、实时语音、3D 形象。

本仓库是 **Astra** 的实现。规格文档在 [`docs/`](docs/)，Agent 执行规则见 [`AGENTS.md`](AGENTS.md)。

当前进度：**M4 完成**（长期记忆 + 情绪/关系状态 + 语音）。
`文字/语音进 → LM Studio → 结构化回复 → 文字 + 语音出`，带持久化、实时事件流、
降级、**跨重启的记忆**、**会随互动变化的情绪与关系**，以及**可以打断的语音对话**。

## 环境要求

- **Python 3.12**（由 [`uv`](https://docs.astral.sh/uv/) 托管；**不要用系统的
  3.14**，torch / funasr 等生态尚未适配）
- **LM Studio** 并加载：
  - `qwen/qwen3.5-9b`（或改 `ASTRA_LLM_MODEL`）
  - `text-embedding-nomic-embed-text-v1.5`（记忆需要，或改 `ASTRA_EMBEDDING_MODEL`）
- **CosyVoice3 雨落版整合包**（语音需要，可选）：一个 Gradio 应用，默认
  `http://127.0.0.1:7860`
- Windows / macOS / Linux

> **8GB 显存提示**：chat 模型与 embedding 模型同时加载可能装不下。
> 装不下时把 `ASTRA_EMBEDDING_PROVIDER=none`——记忆子系统会整体降级为
> "不检索、不写入"，**聊天不受影响**。
>
> **要用语音就必须让 LLM 让出 GPU**（M4 实测：LM Studio 的 LLM 与 CosyVoice
> 无法同时常驻 8GB 且都快——LLM 满 GPU 时 TTS 从 2–4 s/句 劣化到 10–30 s/句）：
>
> ```powershell
> lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384
> ```
>
> 代价是**纯文本聊天也慢下来**：带完整人格 prompt 的一轮实测 **11–21 秒**，
> 而且随上下文增长明显变慢（同一句话在会话第二轮从 11.3 s 涨到 21.4 s）。
> 想要更快的文本聊天，唯一干净的办法是**不用语音时关掉 CosyVoice 进程**、
> 再把 LLM 放回 GPU。详见
> [ADR 0022](docs/decisions/0022-audio-provider-boundary-and-vram.md)。

`uv` 若不在 PATH，先补上（Windows）：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

## 快速开始

```powershell
# 1. 安装依赖
uv sync

# 2. 生成本地配置
Copy-Item .env.example .env
# 编辑 .env：
#   ASTRA_LLM_PROVIDER=lmstudio
#   ASTRA_EMBEDDING_PROVIDER=lmstudio   （要用记忆就打开）

# 3. 启动 LM Studio 并加载上面两个模型（Developer → Start Server）

# 4. 初始化数据库
uv run astra migrate

# 5. 启动服务
uv run astra serve
# → http://127.0.0.1:8000/health
```

## 跟她说句话

**方式一：命令行（推荐，能看见完整事件流）**

```powershell
uv run astra chat --once "你好呀，今天陪我聊聊天吧。"
```

输出（新会话的第一轮）：

```text
[conversation.created]
[turn.started]
[conversation.message.created]
[conversation.message.created]
[assistant.text.completed] 嘿，你终于来了。今天有什么想聊聊的吗？
[avatar.expression.set] {"emotion": "happy", "intensity": 0.65}
[avatar.action.play] {"name": "nod", "intensity": 0.4, "priority": "normal"}
[turn.completed] retries=0 total=7974.245ms
```

`uv run astra chat` 不带参数会进交互 REPL，`/quit` 退出。

**方式二：HTTP 同步**

```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
$body = '{"content":"你好，今天陪我聊聊天吧。"}'
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
     -ContentType 'application/json; charset=utf-8' `
     -Body ([Text.Encoding]::UTF8.GetBytes($body))
$r.reply
$r.response.emotion
```

**方式三：投递即返回，结果走 WebSocket**

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/messages `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"content":"在吗"}'))
# → 202 {turn_id, conversation_id, status:"accepted"}（实测 77ms）
```

`POST /v1/chat/turns` **同步**跑完一轮并返回完整结构化回复
（`reply` / `emotion` / `action` / `voice`）。HTTP 状态码恒为 200：
LLM 挂掉时 `status` 为 `failed`，`reply` 会是一句诚实的中文，而不是 5xx。

`GET /v1/conversations/{id}/messages` 读历史，`GET /v1/config` 取配置与协议词表。

> **`astra chat` 与 `astra serve` 同时跑会争 SQLite**。WAL + `busy_timeout=5000`
> 让它能正常工作，但两者都在写时会偶发等待。

## 记忆（M2）

告诉 AI 一个稳定偏好，它会被提取、打分、去重、向量化并落库；**重启之后仍然记得**。

```powershell
# 1) 说一件值得长期记住的事
$b = '{"content":"我特别喜欢深夜写代码，请记住这个。"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b))

# 2) 看它记下了什么
Invoke-RestMethod http://127.0.0.1:8000/v1/memories | ConvertTo-Json -Depth 6

# 3) 重启 serve，然后在相关语境里问一句 —— 回复会用到这条记忆
#    返回体里的 memories_retrieved / memories_created 会如实告诉你它有没有在工作

# 4) 删掉它，再问一次 —— 不会再被检索到
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:8000/v1/memories/<id>"
```

用户控制面（`docs/06` §9）：

| 端点 | 作用 |
|---|---|
| `GET /v1/memories` | 列出 / 过滤；带 `?query=...` 时走**语义检索** |
| `GET/PATCH/DELETE /v1/memories/{id}` | 看 / 改 / **真删**（事实 + 向量索引一起删） |
| `POST /v1/memories/clear` | 清空全部 |
| `GET /v1/memories/export` | 导出 JSON（含归档与过期，用于备份） |
| `GET/PUT /v1/memories/settings` | 启用 / 禁用记忆（运行时生效，不用重启） |

**不要记住这件事**：在消息里说「不要记住 / 别记住 / 不用记 / 忘掉这件事」，
本轮就不会写入任何记忆（`docs/06` §9）。

> 记忆的架构取舍（为什么内联提取、为什么用余弦距离、为什么打分由代码算）
> 见 [ADR 0018](docs/decisions/0018-m2-memory-architecture.md)；
> 管理 API 与用户控制见 [ADR 0019](docs/decisions/0019-memory-management-api.md)。

## 状态（M3）

她**有情绪，也记得你们的关系**——两者都会随互动变化，并影响下一轮的措辞。

```powershell
# 看当前状态（只读）
Invoke-RestMethod http://127.0.0.1:8000/v1/state | ConvertTo-Json -Depth 4

# 夸她一句
$b = '{"content":"谢谢你，你真的太厉害了，我很喜欢你。"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b))

# 再看一次：happiness 与 affection 都涨了
Invoke-RestMethod http://127.0.0.1:8000/v1/state | ConvertTo-Json -Depth 4
```

**状态变化只来自事件**，没有写入端点（`docs/03` §5）：

- **情绪**（7 维）由两条推导：用户消息的事件规则（被称赞 / 被批评 / 久别重逢），
  以及模型**已经产出**的 `emotion`——后者定义"目标水平"，状态按比例靠拢过去。
- **关系**（5 维）只由事件累积，**不衰减**。
- **情绪会衰减**：按半衰期（默认 120 分钟）回到基线。衰减是**惰性**的（读时计算），
  不需要后台任务。

> 为什么不把 `state_delta` 加回 schema、为什么必须"比例靠拢"而不是加法、
> 为什么关系不衰减——见 [ADR 0020](docs/decisions/0020-m3-state-model.md)。

## 语音（M4）

她能**听你说，也能说给你听**，而且**可以被打断**。

```powershell
# 0) 让 LLM 让出 GPU（8GB 硬约束，见上）
lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384

# 1) 确认 CosyVoice 雨落版在跑，并打开 STT/TTS
$env:ASTRA_STT_PROVIDER="cosyvoice"; $env:ASTRA_TTS_PROVIDER="cosyvoice"
uv run astra serve

# 2) /health 的 stt / tts 应变成 ok（key 集合仍是 api/database/llm/stt/tts）
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4

# 3) 打开 http://127.0.0.1:8000/dev → 勾上「语音模式」→ 按住「按住说话」说一句
#    期望事件序列：
#      audio.input.started → audio.input.stopped → stt.final
#      → turn.started → … → assistant.text.completed → tts.started
#      → （二进制音频帧）→ tts.completed → turn.completed
#    并且真的能听到她的声音

# 4) 她说到一半时再按住说话 → turn.interrupted + 声音立刻停 + 新的 turn 开始
```

音频**走独立的 WebSocket 二进制帧**，不塞 base64：

```text
客户端 → 服务端
  {"type":"input.audio.started","version":1,
   "payload":{"encoding":"pcm16","sample_rate":16000,"channels":1}}
  <原始 int16 PCM 字节>          ← 可多帧
  {"type":"input.audio.stopped","version":1,"payload":{}}
服务端 → 客户端
  tts.started {encoding, sample_rate, channels, text, chunk_count, duration_ms}
  <int16 PCM 字节>
  tts.completed {turn_id, status, chunk_count, duration_ms}
```

要点：

- **voice mode 是显式开关**（`session.voice_mode.set`）。麦克风默认关闭
  （`docs/11` §2）；没开 voice mode 时语音输入照常可用，但**不会**合成语音。
- **服务端不做重采样**：声明的格式必须与 `ASTRA_AUDIO_INPUT_SAMPLE_RATE` /
  `ASTRA_AUDIO_INPUT_CHANNELS` 一致，否则回 `error.protocol`。
- **TTS 挂了不影响文本**：只发一条 `tts.completed{status:"failed"}`，
  回复照常送达（`docs/10` §4）。
- **打断**：新输入会取消正在跑的 turn 并发 `turn.interrupted`；客户端收到后要
  **立即停止播放**已收到的音频（服务端只能停止*继续发送*）。
- **断线不打断**：客户端关掉页面时，正在跑的 turn 会照常跑完并落库。

> 为什么端口是非流式的、为什么用纯 httpx 调 Gradio、为什么 LLM 要挪到 CPU——
> 见 [ADR 0022](docs/decisions/0022-audio-provider-boundary-and-vram.md)；
> 二进制帧协议见 [ADR 0021](docs/decisions/0021-audio-binary-frame-protocol.md)。

## 开发者控制台

浏览器打开 **http://127.0.0.1:8000/dev**

左边是对话（可在 WebSocket / 同步两种模式间切换，语音模式下还有「按住说话」，
以及**「新会话」按钮**），右边是**原始事件流**。顶部是 health 实时状态
（每 5 秒刷新），底部可展开看运行配置、协议词表、**记忆面板**
（列表 / 单条删除 / 清空 / 启用开关）与**状态面板**（7 维情绪 + 5 维关系的条形显示）。

> **「新会话」是 M4 追加的**：在此之前新会话只能靠 60 分钟空闲超时切分，
> 用户没有任何显式手段脱身。旧会话只被置为 `ended`，历史与记忆都还在。
> 见 [ADR 0023](docs/decisions/0023-explicit-new-conversation.md)。

**这是开发工具，不是产品 UI**（见 [ADR 0017](docs/decisions/0017-developer-console.md)）。
它看的是协议与运行状态——原始事件、health、stub、重试、耗时、记忆、状态；
产品 UI 看的是角色。两者不冲突，控制台在产品 UI 做出来之后依然有用。

- 零构建、零 npm、零新增依赖：单文件 HTML + 原生 JS。
- **只在 `ASTRA_ENV=dev`（默认）时挂载**。如果你把 `ASTRA_HTTP_HOST` 改成
  `0.0.0.0` 暴露到局域网，记得同时把 `ASTRA_ENV` 设成 `prod` 关掉它。
- 离线可用，不引任何 CDN。

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run astra serve` | 启动 HTTP + WebSocket 服务 |
| `uv run astra chat` | 连上 WebSocket 聊天（REPL） |
| `uv run astra chat --once "文本"` | 单轮对话，打印完整事件序列 |
| `uv run astra migrate` | 应用数据库迁移（幂等） |
| `uv run pytest -q` | 运行全部测试 |
| `uv run ruff check .` | lint |
| `uv run ruff format --check .` | 格式检查 |
| `uv run mypy src` | 类型检查 |

## 人格配置

`personas/astra.toml` 是「她的灵魂」——身份、性格数值、说话风格、行为规则、
降级话术。改完重启 `astra serve` 生效。

情绪 / 动作 / 音色风格的**白名单不在这个文件里**，它们在
`src/astra/domain/vocabulary.py`：那是与 Unity 端的协议契约，必须有单一真相，
并通过 `GET /v1/config` 分发给客户端。

## 验收（M4）

```powershell
uv run pytest -q                          # 全绿（含音频编解码 / VAD / 二进制帧 / 打断）
uv run ruff check . ; uv run ruff format --check . ; uv run mypy src

# 前置：CosyVoice 雨落版在跑；LM Studio 的 LLM 已 --gpu off
$env:ASTRA_STT_PROVIDER="cosyvoice" ; $env:ASTRA_TTS_PROVIDER="cosyvoice"
uv run astra serve

# 端到端 5 步：
#   0. GET /health → stt/tts 从 not_configured 变成 ok（key 集合仍是那 5 个）
#   1. 打开 /dev → 勾「语音模式」→ 按住说话："你好呀，今天过得怎么样？"
#      → stt.final 出现转写；听到她的声音；tts.completed 的 chunk_count 与实收帧数一致
#   2. 她说到一半时再按住说话 → turn.interrupted，声音立刻停，新 turn 开始
#   3. 全程键盘输入照常可用（文本路径不回归）
#   4. 关掉 CosyVoice 再试 → /health 的 tts=down，但文本回复照常送达
```

## 验收（M3）

```powershell
uv run pytest -q                          # 全绿（含状态的性质测试：不饱和、会衰减、并发不丢更新）
uv run ruff check . ; uv run ruff format --check . ; uv run mypy src

# 端到端 4 步：
#   0. GET /v1/state → 记下初始 happiness
#   1. 说"谢谢你，你真的太厉害了，我很喜欢你。"
#   2. GET /v1/state → happiness 与 affection 都上升
#   3. 看 /dev 的原始事件流：state.emotion.updated（带新值与增量）与 avatar.expression.set
#   4. 隔一段时间再问一次 → 情绪已向 baseline 回落（惰性衰减）
```

## 验收（M2）

```powershell
uv run pytest -q                          # 全绿（含记忆的重启持久化与删除验收）
uv run ruff check . ; uv run ruff format --check . ; uv run mypy src

# 端到端 5 步（需 LM Studio 同时加载 chat 与 embedding 模型）：
#   $env:ASTRA_LLM_PROVIDER="lmstudio" ; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
#   1. 说"我特别喜欢深夜写代码，请记住这个。"
#   2. Ctrl+C 关掉 serve
#   3. 重启 serve
#   4. 新会话里问"我一般什么时候写代码效率最高？"
#      → 返回体 memories_retrieved >= 1，且回复用到了这条记忆
#   5. DELETE /v1/memories/<id> 后再问 → memories_retrieved == 0
# 附加：
#   "这件事不要记住" → /v1/memories 不新增，日志出现 memory.suppressed
#   PUT /v1/memories/settings {"enabled": false} → 之后既不检索也不写入
```

## 验收（M1）

```powershell
uv run pytest -q                          # 全绿
uv run ruff check . ; uv run ruff format --check . ; uv run mypy src

# 正常路径：200 + status=ok + 结构化回复
# 负向路径：关掉 LM Studio 后重跑
#   /health        → llm=down, overall=degraded, HTTP 仍 200
#   /v1/chat/turns → 200 + status=failed + failure.code=llm_unavailable + 非空中文 reply
#   GET 历史        → 用户消息与诚实回复都在
# WebSocket：uv run astra chat --once "你好呀" 应依次打印
#   turn.started → conversation.message.created ×2 → assistant.text.completed
#   → avatar.expression.set → avatar.action.play → turn.completed
# 202：POST /v1/chat/messages 应在 <100ms 返回（实测 77ms）
```

## 验收（M0）

```powershell
uv run astra migrate
uv run astra migrate   # 再跑一次，应幂等成功

Remove-Item .\data\astra.db* -Force -ErrorAction SilentlyContinue   # 删库前先停服务
```

> `Remove-Item .\data\astra.db*` 的通配是必须的 —— WAL 模式会额外产生
> `astra.db-wal` / `astra.db-shm`。删库前请先停掉服务，否则 Windows 上文件被占用
> 会删除失败。

## 项目结构

```
personas/astra.toml     # 人格配置
src/astra/
├─ main.py              # create_app() 工厂 + uvicorn 入口（唯一的依赖装配点）
├─ settings/            # pydantic-settings 配置（按域拆成 5 个 mixin，字段名扁平不变）
├─ logging_setup.py     # 结构化日志 + 敏感字段脱敏
├─ cli.py               # serve / migrate
├─ api/                 # FastAPI 路由层 + HTTP 契约模型
├─ domain/              # 纯数据与 Protocol（不依赖任何框架）
├─ application/         # 用例编排：orchestrator / conversation / prompt / parser / memory
├─ infrastructure/      # SQLite 仓储与迁移、人格 TOML 加载、音频编解码与 VAD
└─ providers/
   ├─ lmstudio/         # 唯一允许 import httpx 的地方（chat + embeddings）
   └─ cosyvoice/        # 语音识别 / 合成（Gradio 后端，纯 httpx）
```

分层依赖方向为 `API -> Application -> Domain`，`Infrastructure` 实现 `Domain`
中定义的端口。`src/astra/domain/**` 禁止 import `fastapi` / `sqlite3` /
`infrastructure`；`domain`、`application`、`api` 都禁止 import HTTP 客户端——
由 `tests/unit/test_architecture_rules.py` 强制执行。

## 已知限制

- **不做流式输出**（留到 P1）。一轮回复耗时约 7–12 秒，期间客户端只能等。
  语音也一样：TTS 是**整段合成**后才开始推流（后端接口是文件级的，
  做不到"首包即播"，见 ADR 0022）。
- **8GB 下 LLM 与 TTS 不能兼得**。要让语音可用就必须让 LLM 走 CPU，
  代价是**所有**对话都变慢：带完整人格 prompt 的一轮实测 11–21 秒
  （会话第一轮 11.3 s、第二轮 21.4 s），一轮语音端到端约 20 秒。
  而且这条是**环境配置，代码检测不到**——
  忘记 `lms load ... --gpu off` 时 TTS 会慢到 10–30 s/句，`/health` 却一切正常。
- **VAD 是占位实现，而且默认关闭**：固定阈值的 RMS 能量检测 + 静音超时
  （800 ms）。没有降噪、没有自适应噪声门限。真机验收实测它在**自然停顿**处就会
  误判"说完了"（"你好呀，今天过得怎么样？"在逗号处被截断，转写变成乱码），
  所以默认不开——M4 的输入路径本来就是 push-to-talk。想做"免按键"时再打开
  （`ASTRA_VAD_ENABLED=true`），届时它需要的是正经 VAD 而不是调大阈值。
- **音频的 WebSocket 总线是全局广播**（不按 `session_id` 过滤），
  多客户端同时在线时都会收到音频；且队满时"淘汰最旧"可能丢掉音频中间帧
  （客户端可用 `tts.started.chunk_count` 自检）。见 ADR 0021。
- **记忆让每轮多约 2.4 秒**。候选是随回复内联产出的（ADR 0018 的实测数据），
  换来的是"落库即持久"——否则说完立刻关程序会丢记忆。关掉
  `ASTRA_EMBEDDING_PROVIDER` 或 `PUT /v1/memories/settings {"enabled": false}`
  即可把这 2.4 秒拿回来。
- **同一会话同时只有一个 turn**：新输入会**打断**正在跑的那一轮
  （`turn.interrupted`），而不是排队。这是 barge-in 的语义——但客户端如果想
  "排队发送"，必须等 `turn.completed` 之后再发。
- **流水线已无空桩**（`stubs_used` 恒为空）。「桩账本」测试保留着，
  M6 加 `tool_calls` 时如果又留了桩会立刻变红。
- **状态的事件规则是词面匹配**（与 no-store 同类局限）：说「你真是个小天才」
  这类变体不会被识别成称赞。好在状态增量只是**小 nudge**，漏判的代价远低于
  写错记忆——漏判就是"情绪没动"，不会出现错误内容。
- **情绪衰减半衰期（120 分钟）是手感值**，缺乏数据支撑。它和
  `ASTRA_STATE_EMOTION_PULL` 都值得用 `docs/10` §5 的固定对话集来调。
- **no-store 是短语匹配，不是语义理解**。说「不要记住 / 别记住 / 不用记 /
  忘掉这件事 / 不要记录」会生效，但「帮我留个印象」这类变体会漏判。
  漏判时可以事后删：`DELETE /v1/memories/{id}`。
- **记忆检索的精度受 embedding 模型限制**（实测数据）。用
  `text-embedding-nomic-embed-text-v1.5` 对**短中文文本**做余弦：

  ```text
  相关对: 0.6378 – 0.8308  (mean 0.7064)
  无关对: 0.6156 – 0.7439  (mean 0.6780)
  ```

  两组几乎完全重叠——没有哪个阈值能干净切开。所以准入闸门
  （`ASTRA_MEMORY_MIN_SIMILARITY`，默认 0.70）取偏严的一侧：
  **宁可漏掉一条相关记忆，也不要把不相干的记忆塞进 prompt**。
  想调松就调低它，想更严就调高。真正的解法是换一个中文区分度更好的
  embedding 模型（需要新迁移 + 重建索引），留待后续里程碑。
- **记忆提取依赖模型自觉**：候选是模型判断"值不值得记"的结果。实测它偶尔会
  记下不够稳定的内容，也可能漏掉值得记的。`importance` / `confidence` 打分与
  去重阈值能过滤一部分，但不是保证——真正的调参需要
  `docs/10_TESTING_AND_ACCEPTANCE.md` §5 的固定对话集。
- **embedding 维度写死在迁移里**（`float[768]`）。换 embedding 模型且维度变化时，
  需要新开一个迁移并重建索引；装配期会校验并在不一致时**启动即失败**。
- **人格规则并不能 100% 约束模型**：实测在全新会话里，模型仍可能说出
  "我正试着把刚才那几段对话的碎片拼起来看"这类**编造记忆**的话，尽管 prompt 里
  明确写了"不要编造记忆"。M2 引入真实记忆后这条需要重新评估（尚未量化）。
- **小模型会照抄历史里最频繁的那句话**。实测踩到过：早期 `llm_provider=none` 时
  产生的降级回复被写进历史，堆了十几条之后，nemotron-4B 开始**照抄**
  "我现在还没有接上语言模型，所以没法回话"。表现极具误导性——`/health` 显示
  `llm: ok`、真实调用要 19.8 秒、`turn.completed` 一切正常，但回复就是那句。
  遇到"回复明显不像模型在思考"时，先用控制台的**「新会话」**按钮换一段干净的
  上下文（旧会话与记忆都保留）。
- **小模型会吐词表外的值**：换用 `nvidia/nemotron-3-nano-4b` 时实测出现过
  `emotion.primary="nervous"`，被归一化降级成 `neutral`（设计如此）。
  它的 `memory_candidates` 也基本为空——4B 在"主动提取记忆"这件事上明显弱于 9B。
- 尚未接 Unity / 3D 形象（M5）。

## 文档

- [`docs/00_README.md`](docs/00_README.md) — 项目入口
- [`docs/04_API_AND_EVENTS.md`](docs/04_API_AND_EVENTS.md) — HTTP / WebSocket 协议
- [`docs/08_IMPLEMENTATION_PLAN.md`](docs/08_IMPLEMENTATION_PLAN.md) — 阶段划分
- [`docs/decisions/`](docs/decisions/) — 架构决策记录（ADR）
