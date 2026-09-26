# 交接文档（M4 → M5）

> **⚠️ 这份文档的上游进度已经被 `HANDOFF_3.md`（M5 → Studio）取代。**
> **M5 已完成**，下一个里程碑是「Studio 应用」。
> **读文档的顺序**：`HANDOFF_3.md`（最新状态）→ 本文件（§1 工作节奏、
> §2 环境细节、§3 会故意变红的测试、§7 不要重新讨论的决定**依然全部有效**）
> → `.trae/documents/studio-app.md`（下一个里程碑的设计文档）。
> 本文件里关于「M5 要做什么」的推测（§6）已过时，只作历史参考。

> **给下一个接手这个仓库的 AI。**
> 这份文档只写**不在别处的东西**：当前状态快照、用户期望的工作节奏、
> 踩过的坑、以及 M5 开工前必须先解决的问题。
>
> 常驻规则看 [`AGENTS.md`](../../AGENTS.md)（分层、测试、依赖、输出格式）；
> 规格看 `docs/`；每个里程碑的**决策与实测数据**看 `docs/decisions/`。
> 本文件**不重复**它们，只做索引与补充。

---

## 0. 一句话现状

**M0–M4 已完成并逐个真机验收通过。** 项目现在是一个能跑、能聊、记得住、有情绪、
**能听能说、能被打断**的本地 AI 伙伴后端。M5 要做的是 **Unity + VRM 形象**——
这是第一次出现**独立的客户端工程**，验收方式与前四个里程碑完全不同
（不再是一条 curl 或一个 WS 脚本能验完的）。

```
质量门：1057 tests passed / ruff check+format 全绿 / mypy --strict 全绿（75 个源文件）
迁移：  0001_init → 0002_message_turn_index → 0003_memories_and_vectors → 0004_state
ADR：   0001 → 0023
空桩：  STUB_STAGES 已空 —— 流水线所有阶段都实现了
```

**M5 大概率不需要新迁移**：形象相关的状态（表情/动作）都是从 turn 里派生的，
不是需要持久化的领域状态。真要加，先想清楚"重启后它还需不需要存在"。

---

## 1. 用户期望的工作节奏（**最重要的一节**）

用户明确认可并期待这套节奏。**每个里程碑都这么走过一遍**，不要改成别的：

```
1. 读规格 + 找出这个里程碑的风险点
2. 【实测】把风险点用真实环境验证一遍，拿到数字
3. 【规划】写计划到 .trae/documents/m<N>-<name>.md
4. 【批准】NotifyUser 等用户批准（不要在批准前写实现代码）
5. 【实施】按计划的顺序做，每步跑一次质量门
6. 【真机验收】启动服务，按文档的验收标准实际跑一遍
7. 【报告】简洁总结 + 明确说"已知限制"与"下一步"
```

**沟通风格**：

- **中文**。代码标识符英文，注释/文档/回复全中文。
- 用户友好、信任你，常把技术决策交给你（"你来决定"）。**但重要分叉必须先问**
  —— 用 `AskUserQuestion`，**带上实测数字**，给出推荐项并说明理由。
  问对了会得到干脆的回答；问错了（比如明明有默认值却去问）会浪费他的时间。
- 用户**讨厌过度设计**。三个相似的行内代码好过一个提前抽出来的抽象。
  不要为"将来可能需要"加配置项、开关、兼容层。
- 报告要**短**。不要贴大段日志。说清楚：改了什么、验了什么、还剩什么限制。

**什么算"重要分叉"**（前四个里程碑的真实例子）：

| 里程碑 | 分叉 | 怎么问的 |
|---|---|---|
| M2 | 记忆提取内联 vs 后台 | 给了三种方案的实测延迟（+2.4s / +4.5s / 后台 3.5s），推荐内联 |
| M3 | 情绪增量从哪来 | 给了 `state_delta` 的实测代价（+2.3s/轮），推荐复用已有的 `emotion` 字段 |
| M4 | 流式 vs 整段合成 | 给了本地模型流式首 token 延迟，用户拍板**不做流式** |
| M4 | 音频二进制怎么过 WS | base64（+33% 体积）vs 独立二进制帧，用户选了**二进制帧** |

**共同点**：都用**实测数据**支撑，都给了推荐项，都在写实现之前问。

**M5 的特殊之处**：这是第一个**客户端**里程碑。用户会亲自在 Unity 里看效果，
所以"实测"的含义变了——不再是量延迟，而是**量"能不能跑起来的可行性"**
（Unity 版本、UniVRM 能不能装上、VRM 模型资源在哪、显存够不够同时开编辑器和音频模型）。
**这些必须在写计划之前问清楚，不要猜。**

---

## 2. 环境与操作细节（Windows）

```powershell
# 每个新 shell 都要先补 PATH（uv 不在 PATH 里）
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# 中文输出：PowerShell 默认不是 UTF-8，读中文日志/响应会显示乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

- **Python 3.12**（由 uv 托管）。系统里另有 3.14，**不要用**——torch/funasr 生态未适配。
- **仓库里没有 `.env`**（只有 `.env.example`）。跑服务时用环境变量内联指定，
  完整的一组（M4 验收用的就是这套）：

  ```powershell
  $env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
  $env:ASTRA_LLM_MODEL="nvidia/nemotron-3-nano-4b"
  $env:ASTRA_STT_PROVIDER="cosyvoice"; $env:ASTRA_TTS_PROVIDER="cosyvoice"
  $env:ASTRA_TTS_REFERENCE_AUDIO="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall\assets\zero_shot_prompt.wav"
  uv run astra serve     # 8000 端口，开发者控制台在 /dev
  ```

- **LM Studio 在 `localhost:1234`**。当前用的模型是 `nvidia/nemotron-3-nano-4b`
  （用户为省内存从 `qwen/qwen3.5-9b` 换过来的，已实测跑通全流程），
  embedding 是 `text-embedding-nomic-embed-text-v1.5`。
- **CosyVoice 雨落版在 `localhost:7860`**（Gradio 应用，STT + TTS 同一个进程）。
  整合包路径见上面 `ASTRA_TTS_REFERENCE_AUDIO` 的上级目录。
  **M4 之前用户要求它保持关闭**（显存紧张）——现在它是开着的，因为 M4 要用。
- **GPU 只有 8GB**。这是贯穿全局的硬约束。M4 的定案是
  **LLM 走 CPU、GPU 全给音频模型**（见 §4.6）。
- 后台跑服务用 `run_in_background`，改完代码要**先 StopCommand 再重启**。
- **Unity 侧（M5 新增）**：`docs/07` §5 推荐 **Unity 6 LTS + UniVRM + VRM 1.0**。
  **具体版本、以及旧工程的 VRM 模型资源在哪个路径，开工前必须问用户**
  ——不要自己假设，也不要去翻他的磁盘。

**质量门命令**（每次改完都跑，四项全绿才算完成）：

```powershell
uv run ruff check src tests
uv run ruff format --check src tests     # 不是 --check 就先 ruff format src tests
uv run mypy src
uv run pytest -q
```

**注意**：`mypy` 只检查 `src`，**不检查 `tests`**（所以测试里可以用 `_NODE` 这种
`str | None` 而不用 `assert`）。ruff 会检查 `tests`，包括 `S603`/`S607` 之类的
安全规则——需要豁免时写 `# noqa: S603 - 理由`。

---

## 3. 会"故意变红"的测试（别慌，那是设计）

这个仓库有几处**设计成"你实现了某个东西它就会红"**的门禁。看到它们变红时，
**不要放宽断言**，要**显式更新**它们，并想清楚新语义是什么：

| 测试 | 它在守什么 |
|---|---|
| `test_orchestrator.py::test_stub_ledger` | 流水线还有没有未实现的阶段。M3 后断言空集；**M6 加 `tool_calls` 时它还会响** |
| `test_contract/test_schema_alignment.py` | 代码 profile 与 `docs/schemas/` 的同步。加字段会让它红 |
| `test_contract/test_assistant_response_contract.py` | 剪枝表与 Payload 模型的同步 |
| `test_unit/test_architecture_rules.py` | 分层依赖（`domain`/`application`/`api` 不许 import httpx 等） |
| `tests/conftest.py::_guard_default_database` | session 结束时断言 `data/astra.db` 未被测试改写。**新集成测试必须用 `tmp_path`** |

**M5 最可能撞上的是 `test_contract/test_schema_alignment.py`**：如果为了
`avatar.expression.set` 往 Payload 里加字段/新模型，这条就会响。那是正常的，
显式更新 `docs/schemas/` 即可。

另外两条容易忘的同步项：

- 新增 `Settings` 字段 → **必须同步 `.env.example`**，否则 `extra="forbid"`
  会让 `Copy-Item .env.example .env` 之后启动直接失败（M0/M1/M2 都踩过）。
  **M5 会正面撞上这条**：`ASTRA_AVATAR_ENABLED` 在 `.env.example` 里
  现在还是注释状态，要取消注释并加进 Settings。
- 新增日志 `extra` 字段 → **必须加进 `logging_setup.EXTRA_ALLOWLIST`**，
  否则那条字段根本不会出现在日志里（白名单是 schema 闸门）。

---

## 4. 已经踩过的坑（按主题，细节在 ADR）

这些是**实测结论**，不要再凭直觉重做一遍：

### 4.1 模型行为

- `qwen3.5-9b` 默认**开思考**（15.4s/轮）；**只有 `reasoning_effort="none"` 能关掉**
  （→ 3.4s）。`enable_thinking=false`、`/no_think` 全部无效。（ADR 0011）
- `strict: true` 的 JSON Schema **不约束** `minimum`/`maximum`。实测拿到过
  `voice.pitch=200`、`memory_candidates[].importance=10`。**clamp 是必需逻辑**。
- 模型会在自由字符串字段里写中文（`emotion.primary="欢迎"`）→ 词表**必须写进
  prompt**（软约束）+ 代码归一化（硬保证）。
- `json.loads` 默认接受 `NaN`/`Infinity` → 必须 `parse_constant=` 拦掉。
- **小模型会照抄历史里最频繁的那句话**（M4 实测，新增）：早期
  `llm_provider=none` 时产生的降级回复被写进历史，堆了十几条之后，
  nemotron-4B 开始**照抄**"我现在还没有接上语言模型，所以没法回话"。
  表现极具误导性——`/health` 显示 `llm: ok`、真实调用要 19.8 秒、
  `turn.completed` 一切正常，但回复就是那句。**遇到"回复不像模型在思考"时，
  先怀疑上下文被污染，再用控制台的「新会话」按钮脱身**（ADR 0023）。
- **`nemotron-3-nano-4b` 的已知弱点**：会吐词表外的情绪
  （实测 `emotion.primary="nervous"` → 被归一化降级成 `neutral`），
  且 `memory_candidates` 基本为空——4B 在"主动提取记忆"上明显弱于 9B。

### 4.2 本机时钟与 ID

- **时钟分辨率只有 15.625ms**：3000 次 `datetime.now()` 只有 1 个唯一值。
  所以**排序一律 `ORDER BY id`**（ULID），`created_at` 只作展示。（ADR 0012）

### 4.3 每个里程碑都先量延迟再定方案

| 决策 | 实测 |
|---|---|
| M1 只发 4 字段 schema | 完整 7 字段 15.2s ↔ 裁剪 4 字段 8.5s |
| M2 候选只带 `{type, content}` | 精简 +2.4s ↔ 完整 +4.5s |
| M3 不加回 `state_delta` | +2.3s/轮 |
| M4 TTS 缓存参考音频 | 首次 3.7s ↔ 之后 3.4s（省两次往返，延迟差异可忽略） |

**方法比结论重要**：写一个 `_scratch_*.py` 直接调真实服务量三次取中位数，
量完就删。**不要猜延迟**。

### 4.4 收敛性：M3 实机验收打回来的一个错

事件规则用**加法** + 情绪用**比例靠拢**时，均衡点是 `T + d/p`。用 `docs/03` §5 的
推荐值算出来是 **1.13** → 被 clamp 到 1.0 → 连夸 8 轮就冲向满值，**状态被钉死**。
改成"规则也按比例推进"后均衡点恒小于 1。（ADR 0020 §2）

**教训**：涉及反馈回路的数值，要在纸上算均衡点，不要只看"单步变化合理"。

### 4.5 记忆检索精度的实测局限

`text-embedding-nomic-embed-text-v1.5` 对**短中文**的余弦区分度很弱：

```
相关对: 0.6378 – 0.8308   (mean 0.7064)
无关对: 0.6156 – 0.7439   (mean 0.6780)
```

两组几乎完全重叠。所以准入用**原始余弦硬闸门**（`memory_min_similarity=0.70`，
偏严的一侧），排序才用 `docs/06` §4 的加权总分。真正的解法是换一个中文更好的
embedding 模型（需新迁移 + 重建索引）。见 ADR 0018 §5。

### 4.6 M4 的实测结论（**M5 必须知道的三条**）

**① 显存：LLM 必须走 CPU，GPU 全给音频模型。**

| 配置 | 文本一轮 | TTS 一句 |
|---|---|---|
| LLM 在 GPU + TTS | 3–5s | **10–30s**（不稳定） |
| **LLM 在 CPU** + TTS | **11–21s** | **2–4s** |

两者在 8GB 里**不可能同时舒服**。M4 选了后者。**LLM 在 CPU 的代价是真实的**
（计划里估 6–10s，实测是 11–21s）——M5 开发时如果觉得聊天太慢，
唯一干净的办法是**不用语音时关掉 CosyVoice 进程**，再把 LLM 放回 GPU：
`lms load <model> --gpu off` ↔ `--gpu max`。

**② VAD 默认关闭**，因为开着会在自然停顿处截断。

实测："你好呀，今天过得怎么样？"在逗号处被判"说完了"，只送了 1.1 秒音频，
转写变成乱码。M4 的输入路径本来就是 push-to-talk（`input.audio.started/stopped`），
VAD 不需要。**要"免按键"就得做正经 VAD**（降噪 + 自适应门限），
调大 `vad_silence_ms` 治不了。

**③ TTS 的预置音色是坏的，默认走 zero-shot 音色克隆。**

`/rainfall_gen_sft` 的预置音色「开拓者」合成出来**像乱码**；
同一段文本走 `/rainfall_gen_zero_shot` 就正常。同一条管道、只有端点不同，
所以问题在预置音色本身（用户单独用整合包时也复现过）。
`ASTRA_TTS_REFERENCE_AUDIO` 一旦配置就切到 zero-shot，参考文本由
`/prompt_wav_recognition` 自动识别，**首次合成时上传+识别一次并缓存**。
留空则退回 SFT 路径。

> **这条改动不是权宜之计**：官方 CosyVoice 仓库的四种模式里同样有 `zero_shot`，
> 将来换官方实现时参考音频是同一份。

### 4.7 M4 期间真实踩到、并修掉的坑

| 坑 | 表现 | 教训 |
|---|---|---|
| `dev.html` 里 `const el` 声明了两次 | **整个页面静默失效**：按钮没反应、WS 不连，但服务端正常、控制台不报错 | JS `SyntaxError` 会杀掉整段脚本。已加 `node --check` 守门测试（无 node 则跳过） |
| VAD 关闭时 `has_speech` 被一起关掉 | 所有语音被当成"全程静音"丢弃，8 条集成测试超时才发现 | "关闭自动结束"和"有没有语音"是两件事 |
| 没有开新会话的手段 | 被污染的历史只能靠 60 分钟空闲超时脱身 | 补了 `POST /v1/conversations`（ADR 0023） |

---

## 5. 技术债与未重测的项

| 项 | 说明 |
|---|---|
| **"编造记忆"的 prompt 遵从度** | M1 实测模型在全新会话里会说"我正试着把刚才那几段对话的碎片拼起来看"。M3 之后 Layer 4/5 都真的有内容了，**这条需要重测**并回写 README |
| **情绪衰减半衰期（120 分钟）** | 纯手感值，缺数据。和 `ASTRA_STATE_EMOTION_PULL` 一起，值得用 `docs/10` §5 的固定对话集来调 |
| **词面匹配的两处局限** | no-store 短语、状态事件规则都是正则。边界已写进 README |
| **每会话串行锁** | M4 的打断已经动过它（新输入取消在跑的 turn），但**一次长 LLM 调用仍会阻塞该会话下一轮** |
| **不做流式（LLM）** | ADR 0011 / M4 都定案不做。所以 **TTS 音频是整段批发的**——M5 的 Unity 端要自己缓冲播放。见 §6.3 |
| **`voice.style` 未映射** | `docs/07` §3 的 `instruct_text` 映射没做，词表已在 `/v1/config` 分发但没人消费 |
| **`avatar_enabled` 硬编码 False** | `config.py` 里写死，`.env.example` 里的 `ASTRA_AVATAR_ENABLED` 仍是注释。**M5 的承诺时点** |
| **`Settings` 未分组进一步细化** | M4 已把 `settings.py` 拆成 5 个 mixin 包（字段名与 env 名逐字不变），当时的分组诉求已满足 |

---

## 6. M5（Avatar）开工前必须解决的几件事

`docs/08` 阶段 5 的范围：Unity project、VRM import、WebSocket client、
expression controller、action controller、lip sync、idle system。

验收：

- 文本聊天会驱动表情。
- TTS 会驱动口型。
- action 能正确播放。
- **Unity 断开不会影响 backend**。

### 6.1 先读

- `docs/07` §5–§11（Unity 客户端、Action/Expression 模型、分层动画、Lip Sync、Idle 系统）
- `docs/04` §2 的 turn 事件序列、§4 的 avatar 事件清单
- `docs/decisions/0009-external-voice-and-avatar-constraints.md`（**必读**：Unity 只复用 VRM 资源）
- `docs/decisions/0021-audio-binary-frame-protocol.md`（二进制帧契约）
- `docs/decisions/0015-persona-toml-and-code-owned-vocabulary.md`（词表为什么在 `/v1/config`）

### 6.2 **最大的缺口：avatar 事件登记了但从来没发过**

> **⚠️ 本节已过时——M5 完成时修正。**
> 实测：avatar 事件**在 M3 就已经发布了**，就在
> `application/orchestrator.py` 的 `_publish_assistant_output` 里
> （`assistant.text.completed` 之后紧跟 `avatar.expression.set` / `avatar.action.play`）。
> 下面这段分析写的时候还没实现，之后 M3 一并做了，但文档没跟着更新。
> 因此 M5 的后端改动只剩一件事：`avatar_enabled` 从硬编码 `False` 改成读 Settings。
> **不要再照着下面那段去做**。见 `docs/decisions/0024-m5-avatar-runtime.md` §1。
>
> 教训与 §4.1 的"照抄历史"同源：**这份文档里的"没有实现"结论，动手前要自己 grep 一遍**。

`EventType` 里有 `AVATAR_EXPRESSION_SET` / `AVATAR_ACTION_PLAY`，
`docs/04` §2 的事件序列里也**明确列了它们**，但是——

```
grep avatar. src/  →
  domain/events.py        定义
  cli.py                  显示分支
  api/schemas.py          avatar_enabled 字段
  api/routes/config.py    avatar_enabled 硬编码
```

**没有任何地方发布它们。** M1–M4 都没做，所以现在的实现在这一点上
**与 `docs/04` §2 不一致**。

M5 必须二选一，并把选择写进 ADR：

- **选项 A（推荐）**：后端发布。turn 成功时按状态情绪发
  `avatar.expression.set`，按 LLM 输出的 `action` 字段发 `avatar.action.play`。
  这与 `docs/04` §2 一致，且客户端不用理解状态模型。
- **选项 B**：客户端从 `state.emotion.updated` 自己推导。改动更小，
  但要改 `docs/04` §2 的序列，且 Unity 端要耦合状态语义。

**先量清楚**：LLM 输出的 `emotion` / `action` 字段现在被存到哪了、
有没有被丢弃——决定 A 的成本。

### 6.3 已知的契约约束（别踩）

- **`/v1/config` 的 key 集合不可随意改**。M5 要把 `avatar_enabled` 从硬编码
  `False` 改成读 Settings，**只改值，尽量不动 key 集合**；同时**取消
  `.env.example` 里 `ASTRA_AVATAR_ENABLED` 的注释并加进 Settings**（§3 的同步规则）。
- **词表从 `GET /v1/config` 取，不要硬编码在 Unity 里**（ADR 0015 的单一真相）。
  `actions` 是有限集合，**绝不允许 LLM 任意生成 Animator 参数名**（`docs/07` §6）。
- **二进制帧协议只有浏览器端实现**（`src/astra/api/static/dev.html`）。
  Unity 端要自己实现"文本帧 / 二进制帧"的分派：二进制帧**不是事件**，
  必须紧跟 `input.audio.started` / `tts.started` 这类说明格式的 JSON 帧。
  契约见 ADR 0021。
- **TTS 音频是整段批发，不是实时流**（ADR 0011 不做流式 + ADR 0009 后端文件级）。
  `tts.started` 先到（带 `sample_rate`、`chunk_count`、`duration_ms`），
  然后一串二进制帧一次性发完。**客户端要自己缓冲、按 `sample_rate` 播放**。
- **服务端不做重采样**：`tts.started` 里的 `sample_rate` 是权威值
  （实测 **24000 Hz / 单声道 / int16**），别假设是 16k。
- **打断**：收到 `turn.interrupted` 要**立刻停音频 + 停动画**。
  注意被中断的 turn **不会**发 `turn.completed`（`docs/10` §7 Scenario D）。
- **麦克风默认关闭**（`docs/11` §2）：只有显式进 voice mode
  （`session.voice_mode.set`）才采集，且客户端采集格式必须与
  `ASTRA_AUDIO_INPUT_SAMPLE_RATE` / `ASTRA_AUDIO_INPUT_CHANNELS` 一致，
  否则服务端回 `error.protocol`。
- **Unity 断开不能影响 backend**（`docs/08` 验收标准）。
  M1b 已经定了"断线不取消在跑的 turn"——不要为了 M5 改掉它。
- **`avatar.look_at.set` / `avatar.lipsync.start` / `avatar.lipsync.stop`**
  在 `docs/04` §4 列了，但**没登记进 `EventType`**。
  用不到就不要登记（ADR 0020 / 0022 都因同样理由移除过死登记）。
- **原始音频绝不进日志**：`SENSITIVE_KEYS` 已含 `audio`，但新增的 extra 字段
  仍要过 `EXTRA_ALLOWLIST`（`docs/11` §6 默认不记录麦克风音频）。

### 6.4 参考实现：浏览器版已经在跑了

`src/astra/api/static/dev.html` 的音频部分**就是一份可工作的参考实现**：
16 kHz 采集、`input.audio.started/stopped`、逐块播放、按 `sample_rate` 建
AudioContext、`turn.interrupted` 时立即 `stopPlayback()`。
Unity 端可以直接照它的**逻辑**翻译（不是照抄代码）。

### 6.5 建议的开工顺序

1. 读 §6.1 的五份文档
2. **问用户**（见 §6.6）——Unity 版本、VRM 模型路径、范围边界
3. **实测**：Unity 能不能装上 UniVRM、打开编辑器和音频模型共存时显存够不够、
   二进制帧在 Unity 的 WS 客户端里怎么区分
4. 拿实测数据 + §6.6 的分叉去 `AskUserQuestion`
5. 写 `.trae/documents/m5-avatar.md`，`NotifyUser` 等批准
6. 再动手

### 6.6 M5 必须先问用户的分叉

**(a) 范围边界** —— 最大的一条

`docs/08` 阶段 5 的验收里有 "TTS 会驱动口型"，但**没列麦克风采集**。
所以至少要明确：

- 选项 A：**只做"能看"**——VRM 显示 + 表情 + 动作 + 口型 + idle，
  语音输入仍走浏览器控制台。范围小，能最快看到"她站在那里"。
- 选项 B：**同时做"能说能听"**——加上麦克风采集、二进制帧收发、打断。
  这是完整的体验，但范围显著变大，而且要处理 Unity 里的音频采集格式。

**推荐 A 先做完再进 B**：口型只需要"能播 `tts.started` 给的音频"，
而麦克风采集是另一套完全不同的坑（设备权限、采样率转换、重采样）。

**(b) avatar 事件谁发** —— 见 §6.2。**推荐后端发（选项 A）**，与 `docs/04` §2 一致。

**(c) VRM 模型资源在哪**

ADR 0009 已定案"只复用 VRM 模型资源，旧脚本一律不用"。但**模型文件在哪个路径、
是什么 VRM 版本（0.x 还是 1.0）、带不带口型 blendshape**，必须问用户。
`docs/07` §5 推荐 **Unity 6 LTS + UniVRM + VRM 1.0**。

**(d) 显存与性能**

Unity 编辑器本身不怎么吃显存，但**8GB 里现在已经装着音频模型**。
要问清楚：开发 M5 时是"关掉 CosyVoice 专心做 Unity"，还是"留着它边做边听"。
这直接决定 §4.6 ① 里 LLM 该放 CPU 还是 GPU。

**(e) 表情强度从哪来**

`docs/07` §11 的 payload 是 `{emotion, intensity, duration_ms}`。
状态系统有 7 维情绪（`docs/03` §5），LLM 输出的是单个 `emotion`。
**用哪一个、intensity 怎么算、duration_ms 谁定**——这是真实分叉，
别自己拍。

---

## 7. 不要重新讨论的决定

以下是用户已拍板或已由 ADR 定案的，**不要再问、也不要"顺手改"**：

- 包名 `astra`（不是 MyFriend）；分层 `API → Application → Domain`，由测试强制执行
- 数据访问用 stdlib `sqlite3` + `asyncio.to_thread`，**不引入 ORM / aiosqlite**
- 迁移是手写 runner，文件里**禁止** `BEGIN`/`COMMIT`（runner 独占事务）
- ID 统一 ULID；排序一律 `ORDER BY id`
- `/health` HTTP 恒 200，固定 5 个组件 key（`api`/`database`/`llm`/`stt`/`tts`）。
  **只改值，不改 key 集合**
- LLM 不可用**不返 5xx**：返 `status:"failed"` + 稳定 `error.code` + 诚实中文回复
- 人格配置用 TOML（stdlib `tomllib`）；情绪/动作/音色/记忆类型**词表在代码里**，
  经 `GET /v1/config` 分发（不放进 TOML，避免两份真相）
- **形象走 Unity + VRM，但只复用模型资源，旧脚本一律不用**（ADR 0009）
- **不做 LLM 流式**；TTS 音频整段批发（ADR 0011 / M4 用户拍板）
- **音频走独立 WebSocket 二进制帧**，不塞 base64（ADR 0021）
- STT/TTS 端口是**非流式**的（`transcribe` / `synthesize`），后端做不到流式
- **TTS 失败绝不让 turn 失败**；只发 `tts.completed{status:"failed"}`
- **断线不取消在跑的 turn**（M1b 契约）
- **TTS 走 zero-shot**（配了参考音频时），预置音色是坏的（§4.6 ③）
- 记忆：`sqlite-vec` 的 vec0 表（`distance_metric=cosine`），
  `memory_embeddings.vector_blob` 是向量的事实来源
- 状态：变化**只来自事件**，没有写入端点；情绪衰减、关系不衰减
- 开发者控制台只在 `ASTRA_ENV=dev` 挂载，零构建零依赖
- **新会话只能显式开**（`POST /v1/conversations`），未知 `conversation_id` **不静默新建**（ADR 0023）

---

## 8. 关键文件速查

```
AGENTS.md                          # 常驻规则（先读这个）
README.md                          # 当前进度 + 已知限制 + 验收命令
docs/decisions/0001..0023          # 决策与实测数据（出问题先查这里）
.trae/documents/m0..m4-*.md        # 每个里程碑的实施计划（含风险清单）

src/astra/
├─ settings/                       # 5 个 mixin 包（base/llm/audio/...）；改它必须同步 .env.example
├─ application/orchestrator.py     # turn 生命周期；STUB_STAGES 在这里
├─ application/{conversation,memory,state}_service.py
├─ domain/{response,memory,state,prompt,vocabulary,events,audio}.py
├─ domain/events.py                # EventType —— avatar.* 在这里登记但**没人发**（§6.2）
├─ infrastructure/audio/           # codec（WAV 编解码）+ vad
├─ infrastructure/db/              # 仓储 + 迁移 + timestamps
├─ providers/lmstudio/             # 唯一允许 import httpx 的 LLM provider
├─ providers/cosyvoice/            # M4：Gradio 客户端 + STT/TTS adapter（纯 httpx，零新增依赖）
└─ api/static/dev.html             # 开发者控制台；音频部分是 **Unity 端的参考实现**

tests/unit/test_architecture_rules.py   # 分层门禁
tests/integration/test_dev_console.py   # 含 node --check 的 JS 语法守门
tests/integration/                      # 真实 uvicorn / TestClient + tmp_path
```

---

## 9. 最后

这个仓库的代码注释和 ADR 都写得**很密**（为什么这么做、否决了什么、实测数字），
它们是刻意留的。**改动前先读那一段的注释**——大概率你会看到"为什么不能那样改"。

有疑问时，宁可多读一个 ADR，也不要凭直觉重构。

**M5 的额外提醒**：这是第一个有**独立客户端工程**的里程碑。前四个里程碑的
"跑一遍质量门 = 完事"在这里**不成立**——后端全绿不代表 Unity 里能看到东西。
计划里必须明确写出**用户怎么亲手验收**（打开哪个场景、点哪里、应该看到什么），
并让用户来跑那一步。
