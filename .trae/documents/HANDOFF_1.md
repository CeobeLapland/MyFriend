# 交接文档（M3 → M4）

> **给下一个接手这个仓库的 AI。**
> 这份文档只写**不在别处的东西**：当前状态快照、用户期望的工作节奏、
> 踩过的坑、以及 M4 开工前必须先解决的问题。
>
> 常驻规则看 [`AGENTS.md`](../../AGENTS.md)（分层、测试、依赖、输出格式）；
> 规格看 `docs/`；每个里程碑的**决策与实测数据**看 `docs/decisions/`。
> 本文件**不重复**它们，只做索引与补充。

---

## 0. 一句话现状

**M0–M3 已完成并逐个真机验收通过。** 项目是一个能跑、能聊、记得住、有情绪的
本地 AI 伙伴后端。M4 要做的是**音频**（STT/TTS/打断）——这是第一次引入
**外部服务进程**，风险结构与前三个里程碑不同。

```
质量门：920 tests passed / ruff check+format 全绿 / mypy --strict 全绿（61 个源文件）
迁移：  0001_init → 0002_message_turn_index → 0003_memories_and_vectors → 0004_state
ADR：   0001 → 0020
空桩：  STUB_STAGES 已空 —— 流水线所有阶段都实现了
```

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

**什么算"重要分叉"**（前三个里程碑的真实例子）：

| 里程碑 | 分叉 | 怎么问的 |
|---|---|---|
| M2 | 记忆提取内联 vs 后台 | 给了三种方案的实测延迟（+2.4s / +4.5s / 后台 3.5s），推荐内联 |
| M3 | 情绪增量从哪来 | 给了 `state_delta` 的实测代价（+2.3s/轮），推荐复用已有的 `emotion` 字段 |
| M2 | 范围：是否做 consolidation | 两个选项，推荐"验收必需 + TTL" |

**共同点**：都用**实测数据**支撑，都给了推荐项，都在写实现之前问。

---

## 2. 环境与操作细节（Windows）

```powershell
# 每个新 shell 都要先补 PATH（uv 不在 PATH 里）
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# 中文输出：PowerShell 默认不是 UTF-8，读中文日志/响应会显示乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

- **Python 3.12**（由 uv 托管）。系统里另有 3.14，**不要用**——torch/funasr 生态未适配。
- **仓库里没有 `.env`**（只有 `.env.example`）。跑服务时用环境变量内联指定：
  ```powershell
  $env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
  uv run astra serve     # 8000 端口，开发者控制台在 /dev
  ```
- **LM Studio 在 `localhost:1234`**，已加载：`qwen/qwen3.5-9b`、
  `nvidia/nemotron-3-nano-4b`、`text-embedding-nomic-embed-text-v1.5`。
- **GPU 只有 8GB**。这是贯穿全局的硬约束，M4 会正面撞上它（见 §6）。
- 后台跑服务用 `run_in_background`，改完代码要**先 StopCommand 再重启**。

**质量门命令**（每次改完都跑，四项全绿才算完成）：

```powershell
uv run ruff check src tests
uv run ruff format --check src tests     # 不是 --check 就先 ruff format src tests
uv run mypy src
uv run pytest -q
```

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

另外两条容易忘的同步项：

- 新增 `Settings` 字段 → **必须同步 `.env.example`**，否则 `extra="forbid"`
  会让 `Copy-Item .env.example .env` 之后启动直接失败（M0/M1/M2 都踩过）。
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

### 4.2 本机时钟与 ID

- **时钟分辨率只有 15.625ms**：3000 次 `datetime.now()` 只有 1 个唯一值。
  所以**排序一律 `ORDER BY id`**（ULID），`created_at` 只作展示。（ADR 0012）

### 4.3 每个里程碑都先量延迟再定方案

| 决策 | 实测 |
|---|---|
| M1 只发 4 字段 schema | 完整 7 字段 15.2s ↔ 裁剪 4 字段 8.5s |
| M2 候选只带 `{type, content}` | 精简 +2.4s ↔ 完整 +4.5s |
| M3 不加回 `state_delta` | +2.3s/轮 |

**方法比结论重要**：写一个 `_scratch_*.py` 直接调 LM Studio 量三次取中位数，
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

---

## 5. 技术债与未重测的项（M4 顺手处理）

| 项 | 说明 |
|---|---|
| **`Settings` 分组** | 已 **45 个字段**，超过 `settings.py` 注释里"~30 个时再评估"的阈值。ADR 0018/0020 都把分组**排到 M3/M4** —— M4 是承诺的时点 |
| **"编造记忆"的 prompt 遵从度** | M1 实测模型在全新会话里会说"我正试着把刚才那几段对话的碎片拼起来看"。M3 之后 Layer 4/5 都真的有内容了，**这条需要重测**并回写 README |
| **情绪衰减半衰期（120 分钟）** | 纯手感值，缺数据。和 `ASTRA_STATE_EMOTION_PULL` 一起，值得用 `docs/10` §5 的固定对话集来调 |
| **词面匹配的两处局限** | no-store 短语、状态事件规则都是正则。边界已写进 README |
| **每会话串行锁** | 一次长 LLM 调用会阻塞该会话下一轮。**M4 做 interruption 必须动它** |
| **不做流式** | ADR 0011 把流式留到 P1。**M4 的 TTS 会正面撞上这条**（见 §6） |

---

## 6. M4（Audio）开工前必须解决的几件事

`docs/08` 阶段 4 的范围：麦克风采集、VAD、STT adapter、TTS adapter、
audio streaming events、interruption。

### 6.1 先读

- `docs/07_AUDIO_AND_AVATAR.md` §1–§4（管线、两个 Adapter 接口、语音策略）
- `docs/11_SECURITY_AND_PRIVACY.md` §2（麦克风默认关闭）、§6（默认不记录原始音频）
- `docs/decisions/0009-external-voice-and-avatar-constraints.md`
- `docs/third_party_voice/RAINFALL_COSYVOICE.md`

### 6.2 必须先问用户的分叉

**(a) 流式 vs 整段合成** —— 最大的范围分叉

`docs/07` §4 明确要求"不等待完整回答"（按句切分，让 TTS 尽早开始）。
但 **ADR 0011 / M1 明确"不做流式"**（留 P1）。M4 做 TTS 必然撞上：

- 选项 A：先做**整段合成**（回复生成完再合成）——范围小，但与 `docs/07` §4 的
  意图有偏差，"低等待感"这个产品目标达不到。
- 选项 B：M4 同时做 **LLM streaming**（`assistant.text.delta` 事件、
  按句切分给 TTS）——范围显著变大，会动 orchestrator 的核心。

**必须先问用户**，并带上你对本地 9B 模型流式首 token 延迟的实测。

**(b) 音频二进制怎么过 WebSocket**

现有 envelope 是纯 JSON（`docs/04` §1 的 6 个必备字段）。`voice.audio.chunk`
要传音频字节，两条路：

- base64 塞进 payload —— 简单，但 +33% 体积、JSON 序列化开销
- 单独的**二进制帧** —— 高效，但要扩协议（客户端要能区分文本帧/二进制帧）

这是协议变更，要新 ADR + 用户确认。

**(c) 8GB 显存怎么装下**

chat 模型（9B）+ embedding 模型已经很紧张，再加 TTS（CosyVoice）+ STT（SenseVoice）
**不可能同时全加载**。需要先和用户确认策略：

- 音频模型常驻 + chat 模型按需加载？
- 还是按需加载音频模型（每次切换有加载延迟）？
- 或者 M4 先只做 **TTS**（用户能听到她说话），STT 留到之后？

**实测优先**：先量 CosyVoice 的显存占用与加载时间，再谈方案。
注意用户此前**明确要求 M0–M3 期间 CosyVoice 保持关闭**（显存/内存紧张），
所以打开它之前要确认机器现在能不能承受。

**(d) interruption 的范围**

`docs/01` §4.6 要求"被打断时优先停止 TTS 和长任务，进入新的 turn"。
这会碰到**每会话串行锁**和 `turn.interrupted` 事件（`EventType` 里已登记但未实现）。

### 6.3 已知的契约约束（别踩）

- **`/health` 的 5 个 key 不可变**（ADR 0006）。M4 把 `stt`/`tts` 从
  `not_configured` 改成真值 —— **只改值，不改 key 集合**。
  `test_health_api.py::test_llm_stt_tts_are_not_configured` 依赖默认值，会需要显式更新。
- **`.env.example` 里已有注释状态的预留键**：
  `ASTRA_STT_PROVIDER` / `ASTRA_TTS_PROVIDER` / `ASTRA_TTS_BASE_URL` /
  `ASTRA_AVATAR_ENABLED` / `ASTRA_TIMEZONE`。实现后**取消注释并同步 Settings**。
- **STT 的 `emotion_hint` 只能作辅助信号，不得直接覆写状态**（`docs/07` §2 明文）。
  M3 刚建好状态系统，这里很容易顺手接上去 —— **要克制**。
- **原始音频绝不进日志**：`SENSITIVE_KEYS` 已含 `audio`，但新增的 extra 字段
  仍要过 `EXTRA_ALLOWLIST`。`docs/11` §6 默认不记录麦克风音频。
- **麦克风默认关闭**（`docs/11` §2）：只有用户显式进 voice mode 才采集。
- 事件类型：`voice.*` / `audio.*` / `stt.*` / `tts.*` 在 `docs/04` §4 已定义，
  但 `EventType` 里只登记了 `STT_FINAL` / `TTS_STARTED` / `TTS_COMPLETED`
  —— 需要按实际用到的补齐。

### 6.4 建议的开工顺序

1. 读 §6.1 的四份文档
2. **实测**：CosyVoice 的启动方式、显存占用、首次合成延迟、音频格式
   （采样率/编码/分块大小）；如果做 STT，同样量 SenseVoice
3. 拿实测数据 + §6.2 的四个分叉去 `AskUserQuestion`
4. 写 `.trae/documents/m4-audio.md`，`NotifyUser` 等批准
5. 再动手

---

## 7. 不要重新讨论的决定

以下是用户已拍板或已由 ADR 定案的，**不要再问、也不要"顺手改"**：

- 包名 `astra`（不是 MyFriend）；分层 `API → Application → Domain`，由测试强制执行
- 数据访问用 stdlib `sqlite3` + `asyncio.to_thread`，**不引入 ORM / aiosqlite**
- 迁移是手写 runner，文件里**禁止** `BEGIN`/`COMMIT`（runner 独占事务）
- ID 统一 ULID；排序一律 `ORDER BY id`
- `/health` HTTP 恒 200，固定 5 个组件 key
- LLM 不可用**不返 5xx**：返 `status:"failed"` + 稳定 `error.code` + 诚实中文回复
- 人格配置用 TOML（stdlib `tomllib`）；情绪/动作/音色/记忆类型**词表在代码里**，
  经 `GET /v1/config` 分发（不放进 TOML，避免两份真相）
- 形象走 Unity + VRM，但**只复用模型资源，旧脚本一律不用**（M5）
- 记忆：`sqlite-vec` 的 vec0 表（`distance_metric=cosine`），
  `memory_embeddings.vector_blob` 是向量的事实来源
- 状态：变化**只来自事件**，没有写入端点；情绪衰减、关系不衰减
- 开发者控制台只在 `ASTRA_ENV=dev` 挂载，零构建零依赖

---

## 8. 关键文件速查

```
AGENTS.md                          # 常驻规则（先读这个）
README.md                          # 当前进度 + 已知限制 + 验收命令
docs/decisions/0001..0020          # 决策与实测数据（出问题先查这里）
.trae/documents/m0..m3-*.md        # 每个里程碑的实施计划（含风险清单）

src/astra/
├─ settings.py                     # 45 个字段；改它必须同步 .env.example
├─ application/orchestrator.py     # turn 生命周期；STUB_STAGES 在这里
├─ application/{memory,state}_service.py
├─ domain/{response,memory,state,prompt,vocabulary,events}.py
├─ infrastructure/db/              # 仓储 + 迁移 + timestamps
└─ providers/lmstudio/             # 唯一允许 import httpx 的地方

tests/unit/test_architecture_rules.py   # 分层门禁
tests/integration/                      # 真实 uvicorn / TestClient + tmp_path
```

---

## 9. 最后

这个仓库的代码注释和 ADR 都写得**很密**（为什么这么做、否决了什么、实测数字），
它们是刻意留的。**改动前先读那一段的注释**——大概率你会看到"为什么不能那样改"。

有疑问时，宁可多读一个 ADR，也不要凭直觉重构。
