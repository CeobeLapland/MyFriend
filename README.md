# Astra

Local-first AI companion：长期记忆、稳定人格、实时语音、3D 形象。

本仓库是 **Astra** 的实现。规格文档在 [`docs/`](docs/)，Agent 执行规则见 [`AGENTS.md`](AGENTS.md)。

当前进度：**M5 完成**（长期记忆 + 情绪/关系状态 + 语音 + Unity/VRM 形象）。
`文字/语音进 → LM Studio → 结构化回复 → 文字 + 语音 + 表情/动作出`，带持久化、
实时事件流、降级、**跨重启的记忆**、**会随互动变化的情绪与关系**、
**可以打断的语音对话**，以及**站在画面里、会眨眼会开口说话**的 VRM 形象。

## 环境要求

- **Python 3.12**（由 [`uv`](https://docs.astral.sh/uv/) 托管；**不要用系统的
  3.14**，torch / funasr 等生态尚未适配）
- **LM Studio** 并加载：
  - `qwen/qwen3.5-9b`（或改 `ASTRA_LLM_MODEL`）
  - `text-embedding-nomic-embed-text-v1.5`（记忆需要，或改 `ASTRA_EMBEDDING_MODEL`）
- **CosyVoice3 雨落版整合包**（语音需要，可选）：一个 Gradio 应用，默认
  `http://127.0.0.1:7860`
- **Unity 2022.3 LTS**（3D 形象需要，可选）：工程在 `E:\UnityProject\VirtualTuber`，
  已内置 **UniVRM 0.131.0**（支持 VRM 1.0）。**不需要额外装包**
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

# 5. 启动服务（默认以"监督进程"运行：Studio 的「保存并重启」靠它，见 ADR 0026）
uv run astra serve
# → 界面 http://127.0.0.1:8000/studio    健康检查 http://127.0.0.1:8000/health
#   --no-supervisor 可退回"前台单进程"（此时 Studio 的重启按钮会如实说不可用）
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
#    ★ 配一段参考音频走 zero-shot 音色克隆（推荐，见下）
$env:ASTRA_TTS_REFERENCE_AUDIO="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall\assets\zero_shot_prompt.wav"
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

- **音色走 zero-shot 克隆**（`ASTRA_TTS_REFERENCE_AUDIO` 指向一段 ≤30 秒的参考
  音频）。留空则退回雨落版的预置音色——**实测那个预置音色「开拓者」合成出来
  像乱码**，所以强烈建议配一段。参考音频对应的文本会自动识别，不用手打。
  雨落版自带的 `assets/zero_shot_prompt.wav` 就能直接用；换成你自己的录音即可
  克隆出你自己的声音。
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

## 形象（M5）

她**站在一个 Unity 窗口里**：会按情绪换表情、按语义播动作、说话时嘴跟着动、
平时会呼吸和眨眼。形象走 **Unity 2022.3 + UniVRM 0.131.0 + VRM 1.0**
（模型 `Celandine.vrm`，自带全套表情与 `aa/ih/ou/ee/oh` 口型）。

```powershell
# 1) 起后端（形象是纯客户端，后端只多一个通告字段）
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_AVATAR_ENABLED="true"
uv run astra serve

# 2) 打开 Unity 工程，按 Play（不需要建场景、不需要拖任何组件）
#    工程：E:\UnityProject\VirtualTuber
#    （第一次打开会导入 UniVRM 并生成 Library，要等一会儿）
```

按 Play 之后：相机/灯光/角色/界面都由 **运行时代码自动建好**，
画面左上角有一个输入框——在那里打字发送，就能看到她的表情和动作；
勾上「语音模式」后（默认已勾），TTS 音频会被播出来并驱动口型。

Unity 端**自己实现**的部分（协议细节见 `docs/04` §3）：

- `GET /v1/config` 取情绪/动作**词表**（不硬编码；ADR 0015 的单一真相）。
- `WS /ws/v1/session/{session_id}` 收发。**二进制帧不是事件**，靠
  `WebSocketReceiveResult.MessageType` 与 JSON 帧区分（ADR 0021）。
- `avatar.expression.set` → 表情（8 个后端情绪 → 6 个 VRM 标准表情，映射表在
  `AstraAvatar.cs` 里，**近似项已标注**）。
- `avatar.action.play` → 11 个动作，**全部是代码程序化骨骼手势**（工程里没有任何
  动画资源，零美术管线；观感朴素，见 ADR 0024）。
- `tts.started` + 二进制帧 + `tts.completed` → 攒齐后建 `AudioClip` 播放，
  用 **30 ms RMS 包络**驱动 `Aa` 口型（整段批发，服务端不做流式）。
- `turn.interrupted` → **立刻**停音频 + 停动作 + 闭嘴（`docs/10` §7 Scenario D）。

要点：

- **Unity 断开不影响 backend**：客户端关连接**不发任何取消**，在跑的 turn 会照常跑完
  并落库（M1b 的契约）。
- **Unity 里不做麦克风采集**（M5 的范围是"只做能看"）。语音输入仍走浏览器控制台
  `/dev`；Unity 端负责"播音频 + 对口型"。
- **口型是振幅驱动的，不是音素对齐**：`docs/07` §9 允许 MVP 用 volume based。
- **音频总线是全局广播**：Unity 与 `/dev` 同时在线时**两边都会收到音频**。

> 为什么 Unity 版本不是文档原先推荐的 Unity 6、为什么动作用程序化手势、
> 为什么 `duration_ms` 不存在——见
> [ADR 0024](docs/decisions/0024-m5-avatar-runtime.md)。
>
> **动画/表演行为的完整清单**（已实现了什么、各自的触发条件、骨骼归属约定、
> 怎么加新的、Mixamo 怎么接）在 Unity 工程里的
> `Assets/Astra/ANIMATION.md`——那是客户端实现细节，刻意不放进后端仓库。

## Studio（产品界面）

浏览器打开 **http://127.0.0.1:8000/studio**

这是应用本体——日常用的那个界面。左栏切页（**对话 / 记忆 / 人格 / 设置 / 模型 / 事件 / 动画**），
中间是页内容，右栏常驻**组件状态**（5 个 health key 的实时值、耗时与原因）与
**角色状态**（7 维情绪 + 5 维关系的条形显示，以及最近一次的表情 / 动作标签）。

**阶段 1 已具备**：

- **对话**：发消息（结果走 WebSocket 实时渲染）、**新会话**、载入历史；被打断的那一轮
  打上「已被打断」标记而**不会消失**。上一轮还在跑时再发一条就会打断它（barge-in）。
- **组件状态**：每 10 秒轮询 `/health`，**串行排期 + 8 秒超时**。下游全挂时 `/health`
  单次要 ~6 秒，所以探测失败**不会**把面板闪成"挂了"——保留上次的值并标注它是多少秒前的；
  每个组件还会显示探针给出的原因（例如 `backend unreachable`）。
- **角色状态**：`GET /v1/state` + 跟着 `state.*` / `avatar.*` 事件实时更新。
- **记忆**：列表、**语义检索**（`?query=`，按相关度排序）、编辑（内容 / 类型 /
  重要度 / 置信度）、**删除**（二次确认，真实删除）、清空（二次确认且写明条数）、
  导出 JSON、**启用开关**（运行时生效，不用重启）。

**阶段 2 已具备**（配置与进程）：

- **设置页**：直接改 `.env` 里的常用项（LLM / embedding / STT / TTS 的 provider 与模型、
  地址、音色与参考音频、若干开关）。每个键都标着**来源**：`.env` / **进程环境** / 默认值——
  内联 `$env:ASTRA_*` 启动时它会如实告诉你"写文件不会改变这一项"。
  写入前会校验整份配置（写坏 = 下次启动直接失败），并留一份 `.env.bak`。
- **保存并重启**：一键写 `.env` + 重启后端（优雅关闭 → 新进程 ≈1 秒）。
  依赖 `astra serve` 的**监督进程**（ADR 0026）；`--no-supervisor` 时按钮禁用并说明原因。
- **模型页**：LM Studio 服务启动/停止、本地模型清单（类型 / 参数 / 大小 / 上下文）、
  加载（`--gpu max|off` + 上下文长度）与卸载。**显存策略是"声明值"**：LM Studio 不暴露
  "模型跑在 GPU 还是 CPU"，页面记的是 Studio 自己下发的参数并明确标注。
- **事件页**：最近 300 条 WebSocket 事件（进出都有），可暂停滚动 —— 取代 `/dev` 的原始
  事件流面板来做日常排查。

**阶段 3 已具备**（语音与动画托管）：

- **语音对话**（push-to-talk）：勾「语音模式」→ 按住说话 → 转写进气泡、回复用她的声音念出来；
  她说到一半时再按住说话会**立刻打断**（声音停、`turn.interrupted` 出现在事件页）。
  协议与 `/dev` 完全一致（16 kHz / 单声道 / PCM16、二进制帧、按 `sample_rate` 播放）。
- **诚实禁用**：`/health` 里 STT/TTS 不是 `ok` 时按钮就禁用/取消勾选，并写明
  "去设置页配好再保存并重启"——不假装能用。
- **动画页**（后端托管映射）：待机池 / 11 个动作 / 坐躺姿态三件套都能在页面里改；
  clip 名手填（Unity 侧那份清单后端不知道）、可导出 JSON、可粘贴导入。
  **后端是权威副本**（`data/astra_animation.json`），Unity 每次连上时拉取，
  工程里那份文件降级为**缓存**；后端连不上时 Unity 继续用本地缓存（不会变成"没动画"）。

**阶段 3 之后的三处修正**（[ADR 0028](docs/decisions/0028-voice-preview-and-history.md)）：

- **语音预览**：勾「识别后先填进输入框」（**默认开**）→ 按住说话只把转写填进输入框，
  改完错别字再回车发送；不勾就回到"说完直接答"（`/dev` 与 Unity 的行为没变）。
- **历史栏**：对话页左侧常驻会话列表（时间 / 条数 / 最后一句摘要，已结束的会标出来），
  点一下就翻回那一段；发消息会带上会话 id，不会串到别的会话去。
- **记忆门槛**：设置页多了「每轮最多自动记几条（0 = 不自动记）」与「每轮注入几条记忆」，
  抽取提示词也收紧了（只记用户明确说出口的长期事实，且不得为了收集记忆而提问）。
  彻底不要记忆就把「启用记忆」关掉；已有的可以在记忆页逐条删或清空。

**阶段 4 已具备**（[ADR 0029](docs/decisions/0029-multi-persona-and-conversation-deletion.md)：会话删除与多人格）：

- **删除会话**：历史栏每一项都有「删」，二次确认框里带着那一段的最后一句摘要。
  删的是**真删**（消息一起没），但**不删**从它里面记住的事——记忆在记忆页单独删。
  删掉的若是当前会话，聊天区清空、下一条消息自动开新的一段；
  她正在回话时当前会话的删除按钮是禁用的（那一轮还要往这段里落库）。
- **人格页**：列出人格目录（`ASTRA_PERSONA_PATH` 的父目录）里的每一位，标记
  「使用中 / 重启后启用 / 文件坏了」；新建、编辑（身份、性格数值、说话风格、
  行为规则、硬约束、降级话术）、删除、**切换**都在页面上做。
  **一位人格 = 一个角色**：历史、记忆、情绪状态都按它归档，换一位就是换一套。
- **切换 = 写 `.env` + 重启**（复用设置页那套监督重启）：人格在启动时装配，
  所以不做运行时热切换——页面会如实显示"重启后启用"，不假装已经生效。
- **保存会重写整份 TOML**（注释不保留，旧文件留成 `<id>.toml.bak`）；
  **删除人格只删文件**，那一位的对话与记忆仍在数据库里（用同名 id 建回来就能接着用）。

**已知限制（阶段 4 为止）**：

- **人格的编辑与切换要重启后端**（人格在装配期加载，ADR 0015/0029）；
  保存人格会重写整份 TOML、不保留原注释（旧文件有 `.bak`，页面里写明了这一点）。
- **改配置要重启后端**（`extra="forbid"` 与 `lru_cache` 的代价，ADR 0025 §2）；
  **重启会中断在跑的 turn**。
- **动画编辑要 Unity 重进 Play 才生效**：Unity 是在"连上后端"时拉一次（不做运行时热更）。
- **clip 名在 Studio 里手填**：Unity 侧的 clip 清单没有上报给后端（P1 可选）。
- **Unity 面板与 Studio 都能改映射**：面板保存会尽力上报后端；上报失败时日志会如实说明
  "本地是新的、后端还是旧的，下次连上会被覆盖"。
- **两处音频会同时播**：`/dev` 或 Unity 与 Studio 同时开着时，两边都会收到 TTS 音频
  （既有广播语义，ADR 0021/0027）。
- **不做进程强杀**：能起停 LM Studio、加载/卸载模型，但不会去杀 CosyVoice 之类的进程；
  后端自身只支持"重启"，不做守护式自愈。
- **检索结果不显示相似度分**：`GET /v1/memories?query=` 只返回条目、不返回分数，
  界面只说"按相关度排序"。
- **记忆 / 会话删除不可恢复**：没有回收站（刻意如此，都有二次确认）。
- **两个窗口**：形象在 Unity（M5），界面在浏览器。桌面壳不在当前阶段。
- 页面与 `/v1` API 一样**没有鉴权**：安全边界是默认绑定 `127.0.0.1`。Studio、`/v1/admin/*`
  与 `/v1/avatar/animation` 都**无条件挂载**（`ASTRA_ENV=prod` 也在），与只在 dev 挂载的
  `/dev` 相反——理由见 [ADR 0025](docs/decisions/0025-studio-application.md)、
  [ADR 0026](docs/decisions/0026-studio-admin-and-supervisor.md) 与
  [ADR 0027](docs/decisions/0027-studio-phase3-voice-and-animation.md)。

实现是零构建、零 npm、零新增依赖：单文件 `src/astra/api/static/studio.html` +
静态页路由 + 一组 `/v1/admin/*` 管理端点。守门测试在
`tests/integration/test_studio_console.py`（离线可用、只用 `textContent` 渲染、
脚本必须可被解析、prod 环境也在、依赖端点都存在）。

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
产品 UI（Studio，`/studio`）看的是角色。两者不冲突——Studio 做出来之后，
这个控制台依然有用（而且语音输入目前在 Studio 里还没做，阶段 1 仍走 `/dev`）。

- 零构建、零 npm、零新增依赖：单文件 HTML + 原生 JS。
- **只在 `ASTRA_ENV=dev`（默认）时挂载**。如果你把 `ASTRA_HTTP_HOST` 改成
  `0.0.0.0` 暴露到局域网，记得同时把 `ASTRA_ENV` 设成 `prod` 关掉它。
- 离线可用，不引任何 CDN。

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run astra serve` | 启动 HTTP + WebSocket 服务（默认带重启监督，`--no-supervisor` 可关） |
| `uv run astra chat` | 连上 WebSocket 聊天（REPL） |
| `uv run astra chat --once "文本"` | 单轮对话，打印完整事件序列 |
| `uv run astra migrate` | 应用数据库迁移（幂等） |
| `uv run pytest -q` | 运行全部测试 |
| `uv run ruff check .` | lint |
| `uv run ruff format --check .` | 格式检查 |
| `uv run mypy src` | 类型检查 |

## 人格配置

`personas/<id>.toml` 是「她的灵魂」——身份、性格数值、说话风格、行为规则、
降级话术。**多个人格 = 这个目录下的多个 `.toml`**，`id` 即文件名，也就是这个角色的
`companion_id`（历史 / 记忆 / 状态都按它归档）。

- 在 Studio 的「**人格**」页里新建 / 编辑 / 删除 / 切换（见 ADR 0029）；
  保存与切换后重启 `astra serve` 生效。
- 手编文件也行：加载时用的是同一套校验，写错了会**拒绝启动**并指出是哪个字段。
- `personas/astra.toml` 是随仓库分发的默认人格；**在页面上保存过一次之后，
  文件会被重写成规范形态、原注释不再保留**（旧文件留成 `.bak`）。

情绪 / 动作 / 音色风格的**白名单不在这些文件里**，它们在
`src/astra/domain/vocabulary.py`：那是与 Unity 端的协议契约，必须有单一真相，
并通过 `GET /v1/config` 分发给客户端。

## 验收（Studio · 阶段 4）

> **状态：代码与自动化门已就绪，下面三轮真机验收待做。**
> 前置：`Copy-Item .env.example .env`，`uv run astra serve`（默认带监督，
> 人格的切换靠它重启），浏览器打开 http://127.0.0.1:8000/studio。

**A. 删除会话**（不需要模型：随便是哪一段旧历史）

```text
1. 对话页 → 左栏挑一段旧会话点「删」→ 确认框里带着它的最后一句摘要 → 确认
2. 它从列表里消失；刷新页面后仍然没有
3. 删「当前正在聊」的那一段 → 聊天区清空 → 直接发一条 → 自动开新的一段（左栏出现新条目）
4. 让她回一段长话，趁她还在说 → 当前会话的「删」是禁用的（title 写明"等她说完"）
5. 记忆页里，从被删会话里抽取出的记忆**还在**（会话删除不删记忆）
```

**B. 多人格**（需要 LLM：至少能聊两句）

```text
1. 「人格」页 → 「新建…」id 填 luna、显示名填 Luna → 创建 → 列表出现 luna（未激活）
2. 选中 luna → 改 traits / behavior.rules → 「保存」→ 横幅提示重启后生效
3. 「切换为这一位」→ 确认重启 → 后端重启（遮罩），页面自动重连；
   对话页提示"现在是 luna 在说话"，左栏**没有** Astra 的历史
4. 和 Luna 聊两句、让它记一条偏好 → 记忆页里只有她的记忆
5. 切回 astra（同样重启）→ 历史、记忆、情绪状态都是原来的
6. 删除：使用中的那一位会被拒（409 提示"先切换走"）；删掉 luna → 文件消失；
   再新建同名 luna → 它的对话与记忆**回来了**（预期行为，确认框里写了）
7. 只剩一位时「删」被拒（"删了下次启动就没有身份"）
```

**C. 提示词编辑**

```text
1. 「人格」页改 astra 的 behavior.rules 或 prompt.hard_constraints（比如删掉一条反模板规则）
2. 「保存」→ 设置页点「保存并重启」或回人格页「切换为这一位」触发重启
3. 对话两轮，语气/口径能看出变化；`personas/astra.toml.bak` 里是保存前的旧文件
```

## 验收（Studio · 阶段 3）

> **状态：代码与自动化门已就绪，下面两轮真机验收待做**（语音那轮要人在麦克风前听，
> 动画那轮要在 Unity 里按 Play）。

**A. 语音**（前置：CosyVoice 雨落版在跑；LLM 按 8GB 显存约束用 `--gpu off`，见 ADR 0022）

```powershell
uv run astra serve                   # → http://127.0.0.1:8000/studio
```

```text
1. 设置页：把 STT/TTS 打开（选 cosyvoice）→ 「保存并重启」→ 右栏 stt/tts 从 not_configured 变 ok
2. 对话页勾「语音模式」→ 按住说话说一句 → 状态走"正在听… → 识别中… → 播放中"，
   气泡里出现转写内容，并且**真的听到她的声音**
3. 她说到一半时再按住说话 → 声音**立刻停**，事件页出现 turn.interrupted，新一轮正常开始
4. 不勾「语音模式」时按住说话 → 仍能识别，但**不合成语音**（纯文本路径不回归）
5. 把两个 provider 设回 none 并重启 → 按钮禁用、勾选被取消，并写明去设置页配（不假装能用）
```

**B. 动画托管**（前置：Unity 工程已备份；后端与 Unity 都在跑）

```text
1. 冷启动（后端还没有 data/astra_animation.json）：Unity 按 Play →
   Console 出现"后端还没有动画映射副本——把本地这份上报上去（首次播种）"
   → 「动画」页立刻能看到那 11 条待机池与已配的 7 个动作
2. 在动画页改一条（例如给 wave 换个 clip）→ 「保存到后端」→
   Unity 里退出 Play 再进入 → Console 显示"动画配置已应用（来自 后端）"，动作看得出差别
3. 停掉后端 → Unity 再按 Play → 动画照常（本地缓存兜底），Console 如实写"继续用本地缓存"
4. 在 Unity 面板里改一条并保存 → Console 写"动画配置已上报给后端" →
   动画页「重新载入」能看到这条改动
5. 在动画页填一个非法值（未知动作名 / 空 clip 名 / 权重 0）→ 保存被拒并指出是第几条
```

## 验收（Studio · 阶段 2）

> **状态：代码与自动化门已就绪，下面这 8 步真机验收待做。**
> 这一阶段**不再需要内联 `$env:ASTRA_*`**——配置从 `.env` 来、在页面里改（ADR 0026）。

```powershell
Copy-Item .env.example .env          # 仓库里没有 .env；Studio 也会在第一次保存时自动复制
lms server start                     # 让 /health 的 llm 有东西可探（可选，模型页也能启停）
uv run astra serve                   # 默认带监督 →  http://127.0.0.1:8000/studio
```

```text
1. 设置页：每个键显示当前值与「来源」徽标（.env / 进程环境 / 默认值），
   `.env` 路径正确，没有 unknown_keys 告警

2. 改 ASTRA_LLM_MODEL → 「保存并重启」→ 遮罩显示"后端正在重启…" → ≈1~2 秒后自己回来，
   设置页与 `/v1/config` 都显示新模型；说一句话，回复确实来自新模型

3. 填一个非法值（例如温度写 abc）→ 保存被拒、页面给出原因、`.env` 没被写坏
   （`.env.bak` 还在）

4. 模型页：列出 3 个本地模型；加载 nemotron-4B（gpu max，上下文留空）→
   「已加载」区块出现它（带 ctx 与大小）；点「卸载」→ 回到未加载

5. 显存策略换成 gpu off 再加载一次 → 页面在"上次由 Studio 下发"里标出这是**声明值**

6. 模型页点「停止服务」→ 右栏 llm 变 down、页面不卡死；点「启动服务」→ 回到 ok

7. 事件页：说一句话，能看到 turn.started / assistant.text.completed / avatar.* 依次出现
   （自己发出的帧带 → 前缀）

8. 关掉 Studio 浏览器页 → 后端不受影响；回到终端按 Ctrl+C → 父进程与后端**一起**退出，
   没有残留进程占着 8000
```

## 验收（Studio · 阶段 1）

> **状态：代码与自动化门已就绪，下面这 8 步真机验收待做。** 阶段 1 只做文本路径；
> 语音与动画在阶段 3，配置编辑已在阶段 2 落地（见上一节）。

```powershell
# 准备：LM Studio 起 REST 并加载模型（纯文本场景放 GPU 更快；CosyVoice 可以关着）
lms server start
lms load nvidia/nemotron-3-nano-4b --gpu max
# 后端（仓库没有 .env，用内联环境变量）
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_LLM_MODEL="nvidia/nemotron-3-nano-4b"
uv run astra serve                        # → http://127.0.0.1:8000/studio
```

```text
1. 打开 /studio —— 界面出来了，右栏组件状态是真实值
   （LLM=ok；STT/TTS=not_configured——不是"永远 ok"）

2. 验证"慢 health 不骗人"：先 lms server stop → 刷新页面，llm 变 down、界面不卡死；
   再 lms server start → 轮询后自己回到 ok

3. 说一句话 —— 回复出现在气泡里，右栏情绪条跟着动

4. 打断：连续发两条 —— 第一条被打上「已被打断」标记、第二条正常回

5. 点「新会话」—— 对话区清空，但记忆页里旧记忆还在

6. 记忆页搜一句话 —— 语义检索出相关条目（?query=）

7. 删掉一条记忆（二次确认后）—— 再搜不再出现

8. 关掉 Studio 页面 —— 后端不受影响（打开 /dev 还能正常对话；M1b 契约）
```

## 验收（M5）

**这一节的最后几步只能由人在 Unity 里做**——后端全绿不代表 Unity 里能看到东西。

```powershell
uv run pytest -q                          # 全绿（1185 条，含 Studio 三个阶段的守门测试）
uv run ruff check src tests ; uv run ruff format --check src tests ; uv run mypy src
```

然后：

```powershell
# 0) 后端（终端 1）
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_AVATAR_ENABLED="true"
uv run astra serve

# 1) Unity Hub → 打开 E:\UnityProject\VirtualTuber
#    → 打开 Assets/Scenes/SampleScene.unity → 按 Play
#    应该看到：Celandine 站在画面中央、面向相机、在呼吸和眨眼
#    Console 里应该有：astra: loading vrm ... / astra: ws connected

# 2) 左上角输入框输入「谢谢你，你真的太厉害了，我很喜欢你。」→ 回车
#    应该看到：表情变成开心、播一个动作
#    事件日志里出现 assistant.text.completed / avatar.expression.set / avatar.action.play

# 3) 输入「今天好累啊」→ 表情与动作应该换一个

# 4) 验证「Unity 断开不影响 backend」：
#    停止 Play → 浏览器打开 http://127.0.0.1:8000/dev → 仍能正常对话、/health 正常

# 5) 口型（需要 CosyVoice 在跑 + LLM 已挪回 CPU，见"语音（M4）"的准备）：
#    重新 Play → 从 Unity 输入框发一句话
#    应该看到：听到她的声音，嘴跟着声音开合，说完闭回去

# 6) 打断：/dev 里进语音模式 → 让 Unity 说一句 → 说到一半从 /dev 按住说话
#    应该看到：Unity 端声音立刻停、动作停、嘴闭回去
```

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

### 形象（M5）的已知限制

- **动作是代码程序化手势，不是美术动画**：这个 Unity 工程里没有任何动画资源，
  11 个动作全部用 humanoid 骨骼旋转实现。观感**明显朴素**，而且不同 VRM 的骨骼
  局部轴可能不同——手臂手势的旋转轴是按常见朝向猜的，可能需要按观感调
  `AstraActions.cs` 里对应那一行。
- **表情映射有损**：后端 8 个情绪 ↔ VRM 6 个标准表情。`excited`→`happy`、
  `sleepy`→`relaxed`、**`embarrassed`→`relaxed` 是最勉强的**，很可能"看起来不像害羞"。
  映射表只有 8 行，在 `AstraAvatar.cs` 里，可逐条调。
- **口型是振幅驱动的，不是音素对齐**：只驱动 `Aa`，所以"啊"和"嘘"看起来一样。
  真正的 viseme 需要音素时序，后端不提供。可用 `AstraConfig.LipSyncGain` 调开口幅度。
- **Unity 里不做麦克风采集**：M5 的范围是"只做能看"。语音**输入**仍走浏览器控制台
  `/dev`，Unity 端只负责"播音频 + 对口型 + 表情 + 动作"。
- **客户端源码没有版本控制**：Unity 工程 `E:\UnityProject\VirtualTuber` 不是 git 仓库，
  用户在工程内开发（`Assets/Astra/Scripts/`）。误删即永久丢失——这是本里程碑最实在的
  风险，最便宜的保险是在该工程里 `git init` + 一份 Unity `.gitignore`（见 ADR 0024 §9）。
- **界面是 IMGUI 开发界面，不是产品 UI**：一个输入框 + 状态行 + 事件日志，
  和开发者控制台一样属于工具，不是给最终用户看的界面。
- **运行时会自建整个场景**（`[RuntimeInitializeOnLoadMethod]`），所以它会在该工程
  **任意场景**进入 Play 时执行。这个工程是 Astra 专用壳，可接受。
- **模型从 `StreamingAssets` 按真实文件路径加载**：只在桌面端成立；
  将来上 Android 要改走 `UnityWebRequest`。
- **`tts.started → 首个可见口型` 有一整句的等待**：服务端整段合成完毕才开始推流
  （ADR 0011 不做流式），所以她是"想完了才开口"，不是边说边想。

## 文档

- [`docs/00_README.md`](docs/00_README.md) — 项目入口
- [`docs/04_API_AND_EVENTS.md`](docs/04_API_AND_EVENTS.md) — HTTP / WebSocket 协议
- [`docs/08_IMPLEMENTATION_PLAN.md`](docs/08_IMPLEMENTATION_PLAN.md) — 阶段划分
- [`docs/decisions/`](docs/decisions/) — 架构决策记录（ADR）
