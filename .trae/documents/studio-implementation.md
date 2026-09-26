# Studio 应用 — 实施计划（阶段 1：Web UI 骨架）

## Context

M0–M5 已完成并逐个真机验收通过。当前质量门：**1058 tests passed / ruff check+format 全绿 /
mypy --strict 全绿（75 个源文件）**；迁移到 `0004`，ADR 到 `0024`，`STUB_STAGES` 已空。

现在开始 **Studio 应用**（设计输入：`.trae/documents/studio-app.md`）。
配套 ADR：[`docs/decisions/0025-studio-application.md`](../../docs/decisions/0025-studio-application.md)
（三条选型决定 + 语音排期 + 挂载规则 + 显存状态口径）。**本文件是阶段 1 的执行依据。**

**本轮用户已拍板**（AskUserQuestion，均按推荐项采纳）：

| # | 决定点 | 结论 |
|---|---|---|
| 1 | UI 形态 | **Web UI 先行**（`GET /studio`，浏览器打开；将来可选桌面壳，A→C 两步走） |
| 2 | 配置读写 | **写 `.env` + 一键重启后端**（实测重启 ≈1~2s）；不做运行时热更新 |
| 3 | 动画映射 | **后端托管**，Unity 改为从后端拉；放**阶段 3** 实施 |
| 4 | 语音 | **要，先文本后语音**（阶段 3 移植 `/dev` 的音频逻辑） |
| — | 默认项（用户未反对即生效） | 桌面宠物留 P2；进程管理先"状态显示 + 重启后端"，不做强杀 |

**阶段 1 范围** = 设计文档 P0 #1–#4：**对话 / 组件状态 / 角色状态 / 记忆管理**，
装进一个产品级网页。**不做**：配置编辑（阶段 2）、语音（阶段 3）、动画页（阶段 3）、
原始事件流面板（P1，可在阶段 2 顺手补）。

---

## 一、本轮实测（写计划前的真实数据）

| # | 事实 | 影响 |
|---|---|---|
| 1 | `lms` CLI 能力完整：`load --gpu off\|max\|0~1 --identifier --ttl -y`、`unload`、`ps --json`、`ls`、`server start\|stop\|status`；本机 3 个模型（nemotron-4B / qwen3.5-9B / nomic-embed） | 阶段 2 的"模型下拉 / 显存策略 / 启动 LM Studio"全部有实现路径 |
| 2 | **`lms ps --json` 与 REST `/api/v0/models` 都不暴露 GPU/CPU 归属**；可得的是加载状态（`state` / `contextLength` / `sizeBytes`） | 显存状态设计为"加载实测 + 归属声明"（ADR 0025 §6）；studio-app.md §10#6 关闭 |
| 3 | `lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384` 实测 **5.07s**（4B、warm cache），`unload` 干净 | 阶段 2 切显存策略要有进度反馈；本轮结束时机器状态已还原（无模型加载、REST 已停） |
| 4 | **后端启动到可服务 ≈ 1~2 秒**：uvicorn 的 `Application startup complete` 与 `Started server process` 只差 8ms，耗时都在 uv / 环境装配 | "一键重启"方案成立的直接依据（ADR 0025 §2） |
| 5 | **下游全挂时一次 `/health` ≈ 6 秒**：llm / stt / tts 三个探测各 ~2s（`ConnectTimeout`）、串行累加 | ⚠️ 新坑：状态栏轮询与"等后端就绪"的探测都必须容忍（§2.5 的对策） |
| 6 | `lms server start` 可拉起 LM Studio REST：`/v1/models`（OpenAI 兼容）与 `/api/v0/models`（原生）都通；`stop` 后干净关闭 | 验收步骤必须包含 `lms server start`（否则 llm 恒 down） |
| 7 | 仓库**没有 `.env`**（只有 `.env.example`）；缺省值下后端可直接启动（本轮实测） | 阶段 1 不依赖 `.env`；阶段 2 的写配置从"复制 `.env.example`"开始 |
| 8 | 静态页挂法：`routes/dev.py` 用 `FileResponse` 单文件、`include_in_schema=False`；`/dev` 仅 dev 挂载；守门测试在 `tests/integration/test_dev_console.py`（7 类断言，含 node --check） | 照这个模式加 `/studio`；测试模式可整体复用 |
| 9 | `dev.html` 35.6 KB 单文件（对话 / WS / 记忆 / health / 音频全在里面）；health 用 `setInterval(5s)` + 裸 `fetch`（无超时控制） | 产品 UI 的"最大可用单文件"参照；health 轮询是**要改进**的点（§2.5） |

---

## 二、阶段 1 技术设计

### 2.1 形态与挂载

- 新文件 `src/astra/api/static/studio.html`：**单文件、零构建、原生 JS**（沿用 ADR 0017 的做法）。
- 新路由 `src/astra/api/routes/studio.py`：`GET /studio` → `FileResponse`，`include_in_schema=False`。
- **无条件挂载**（不受 `ASTRA_ENV` 门控；理由见 ADR 0025 §5）。**`/dev` 不动。**
- 阶段 1 后端**零新增业务端点**：对话 / 状态 / 记忆 / health 的接口全部已存在。

### 2.2 布局（按设计文档 §5.3 的草图落地）

```text
┌────────────────────────────────────────────┬──────────────────┐
│  对话区（气泡 + 时间 + 情绪/动作小标签）      │  组件状态         │
│                                            │  ● 后端 ok        │
│  [新会话]                                   │  ● LLM ok         │
│                                            │  ● STT not_conf   │
│  [输入框                      ] [发送]       │  ● TTS ok         │
│                                            │  ● Unity —        │
├────────────────────────────────────────────┤──────────────────┤
│  侧栏切页：对话 · 记忆                       │  角色状态         │
│  （配置 / 动画 / 日志 在后续阶段加入）        │  7 维情绪条       │
│                                            │  5 维关系条       │
└────────────────────────────────────────────┴──────────────────┘
```

- 主区 = 对话（默认页）；侧栏两个页签：**对话 / 记忆**。
  未实现的页（配置 / 动画 / 日志）**先不放占位**——不显示比"点进去是空的"诚实。
- 右栏常驻：**组件状态**（5 个 health key 的实时值 + error 文案）+
  **角色状态**（7 维情绪 / 5 维关系条 + 最近的动作 / 表情标签）。

### 2.3 功能 → 数据来源（全部是既有接口）

| 功能 | 端点 / 事件 |
|---|---|
| 发消息（202 + 结果走 WS） | `POST /v1/chat/messages` + WS `turn.*` / `assistant.text.completed` |
| 开新会话 | `POST /v1/conversations`（ADR 0023：新会话只能显式开） |
| 历史加载 | `GET /v1/conversations/{id}/messages`（未知会话 404，不要静默当空） |
| 打断提示 | WS `turn.interrupted`（被中断的 turn **不会**发 `turn.completed`） |
| 组件状态 | `GET /health`（轮询，策略见 §2.5） |
| 角色状态 | `GET /v1/state` + WS `state.emotion.updated` / `state.relationship.updated` |
| 当前动作 / 表情 | WS `avatar.expression.set` / `avatar.action.play`（渲染成人类可读小标签） |
| 记忆列表 / 搜索 | `GET /v1/memories`（`?query=` 语义检索） |
| 记忆编辑 / 删除 / 清空 / 导出 / 开关 | `PATCH`/`DELETE /v1/memories/{id}`、`POST /v1/memories/clear`、`GET /v1/memories/export`、`PUT /v1/memories/settings` |

### 2.4 复用 `dev.html` 的哪些逻辑（照逻辑，不照抄）

- **会话 id 的获取**（dev.html 已解决"当前会话是哪个"）：从 WS `conversation.created` /
  `turn.started` 的 payload、`POST /v1/chat/messages` 的 202 响应、`POST /v1/conversations`
  的响应三处学习，存一个 `conversationId` 变量。
- WS 连接与重连、事件分派、发送 / 接收 / 渲染、记忆面板的全套交互、health 渲染。
- 历史加载：`GET /v1/conversations/{conversationId}/messages`（**该接口是否有分页参数
  在实施时确认**；没有就只取最近 N 条，避免一次拉全量）。

**不照抄**：样式与布局（Studio 是产品级，重做视觉）；原始事件流面板（阶段 1 不做）。
安全约定沿用（测试会守）：渲染一律 `textContent`（模型输出不可信）、绝不引外部资源。

### 2.5 ⚠️ health 轮询策略（针对实测的"慢 health"）

实测：下游全挂时 `/health` 单次 ≈ 6s（三个探测各 ~2s、串行；只配 LLM 时约 2s）。
`dev.html` 的 `setInterval(5s)` + 裸 `fetch` 在这种延迟下会堆积请求。Studio 改为：

- **串行排期**：上一次返回（或超时）后才排下一次（`setTimeout` 链，不用 `setInterval`）；
  间隔 10s、`fetch` 用 `AbortController` 超时 **≥ 8s**。
- 超时/失败时**保留上次成功值**并标注"陈旧"，不要闪成"挂了"。
- 阶段 2 的"一键重启"等待就绪也用同样宽松的探测（否则重启完成也探测不到）。

### 2.6 记忆页交互（危险操作的防线）

- 删除：**二次确认**（真实 DELETE，无回收站——AGENTS.md"不静默删除用户数据"）。
- 清空：**二次确认** + 明确当前条数。
- 导出：`GET /v1/memories/export` 触发浏览器下载。
- 语义搜索：输入 → `?query=`；空查询回到普通列表；结果显示相似度分。
- 启用开关：`PUT /v1/memories/settings`（运行时生效，不用重启——已有能力）。

### 2.7 对话页细节

- 发送路径沿用 dev.html（`POST /v1/chat/messages` 202；结果走 WS 事件渲染）。
- `turn.interrupted` → 在被打断的气泡上打"已被打断"标记（**不要删掉那一轮**）。
- `assistant.text.completed` 的 emotion / action 显示为气泡下沿的小标签。

### 2.8 守门测试（`tests/integration/test_studio_console.py`）

照 `test_dev_console.py` 的断言逐条对应：

1. `GET /studio` → 200 + `text/html`；
2. **prod 环境也在**（与 /dev 相反——显式断言这个差异，ADR 0025 Consequences 要求钉住）；
3. 离线可用（无 `http(s)://`、无 `<script src=`、无 `<link`）；
4. 渲染安全（无 `.innerHTML`，有 `textContent`）；
5. `<script>` 块过 `node --check`（无 node 则跳过）；
6. `include_in_schema=False`（不在 `/openapi.json`）；
7. 依赖端点存在性（health / config / chat / conversations / memories / state）。

---

## 三、目标文件树

```text
e:\DreamingPath\MyFriend\
├─ src/astra/api/static/studio.html            # 新：产品 UI（单文件，零构建）
├─ src/astra/api/routes/studio.py              # 新：GET /studio → FileResponse
├─ src/astra/api/app.py                        # 改：+studio_router（无条件挂载）
├─ tests/integration/test_studio_console.py    # 新：守门测试（模式同 test_dev_console.py）
├─ docs/decisions/0025-studio-application.md   # 新：配套 ADR（本计划同批交付）
├─ README.md                                   # 改（完成时：Studio 段落 + 验收 + 已知限制）
└─ .trae/documents/studio-implementation.md    # 本文件
```

**无新依赖、无新迁移、无新业务端点。**

---

## 四、实施顺序（每步停下来都能看见进展）

| # | 步骤 | 验证 |
|---|---|---|
| 1 | 后端：`routes/studio.py` + `app.py` 挂载 + 测试骨架（先返回最小页面） | `uv run pytest tests/integration/test_studio_console.py -q` + ruff + mypy |
| 2 | `studio.html` 骨架：布局 + 样式 + 切页 + 右栏占位 | 浏览器打开 `/studio`（用户第一个查看点） |
| 3 | 对话页：WS + 发送 / 渲染 + 新会话 + 打断标记 + 历史 | 能和她说上第一句话 |
| 4 | 右栏：health chips（§2.5 策略）+ 角色状态（`/v1/state` + WS） | 状态跟着对话动 |
| 5 | 记忆页：列表 / 搜索 / 编辑 / 删除 / 清空 / 导出 / 开关 | 与 `/dev` 记忆面板功能对齐 |
| 6 | 测试补全（§2.8 七条）+ 全量质量门（四项） | 四项全绿 |
| 7 | README + 真机验收（§六，用户亲手） | 见 §六 |

**第 1 步之后每一步都可让用户打开 `/studio` 看**——不要憋到最后。

---

## 五、依赖变更

**无。** 后端不新增 Python 包；前端零构建、零 npm（ADR 0017 的既定路线）。

---

## 六、验收标准

### 自动化（后端不能回归）

```powershell
uv run pytest -q                          # 1058 + 新增
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

### 真机端到端（用户亲手，8 步）

准备（终端）：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# LM Studio：起 REST + 加载模型（纯文本场景放 GPU 更快；CosyVoice 关着）
lms server start
lms load nvidia/nemotron-3-nano-4b --gpu max
# 后端（仓库没有 .env，用内联环境变量；与 M4/M5 验收一致）
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_LLM_MODEL="nvidia/nemotron-3-nano-4b"
uv run astra serve                        # → http://127.0.0.1:8000/studio
```

```text
1. 打开 http://127.0.0.1:8000/studio —— 界面出来了，右栏组件状态是真实值
   （LLM 应显示 ok；STT/TTS 是 not_configured——不是"永远 ok"）

2. 验证"慢 health 不骗人"：先 lms server stop → 刷新页面，llm 变 down、界面不卡死；
   再 lms server start → 轮询后自己回到 ok

3. 说一句话 —— 回复出现在气泡里，情绪条跟着动

4. 打断：连续发两条 —— 第一条被打上"已打断"标记、第二条正常回

5. 开新会话 —— 历史清空，但记忆页里旧记忆还在

6. 记忆页搜一句话 —— 语义检索出相关条目（?query=）

7. 删掉一条记忆（二次确认后）—— 再搜不再出现

8. 关掉 Studio 浏览器页 —— 后端不受影响（打开 /dev 还能正常对话；M1b 契约）
```

> 语音 / 动画 / 配置编辑的验收分别在阶段 3 / 阶段 3 / 阶段 2，不在此列。

---

## 七、风险点

1. **`/health` 慢（≈6s，实测）**：对策见 §2.5；UI 文案不要出现"即将超时"这类
   会诱导误判的话。
2. **前端复杂度失控**（studio-app.md §11 风险 4 点名）：单文件 + 零构建是护栏；
   超过 ~100 KB 再谈拆文件，**不引入 npm / 构建链**。守门测试（§2.8）防止
   "一个语法错误整页静默失效"（M4 的 `const el` 教训）。
3. **记忆删除是真实删除**：二次确认 + 文案写清"不可恢复"。
4. **不要碰的红线**：`/dev` 不删不改；`/health` 与 `/v1/config` 的 key 集合不动；
   不加迁移；不改任何既有端点语义。
5. **WS 断线重连**：照 dev.html；后端契约"断线不取消在跑的 turn"——
   重连后靠历史接口补齐，不做会话恢复。
6. **阶段 2 的 `.env` 写入**（提前记住）：只改值、不改键、保留注释分组
   （`extra="forbid"`，写坏 = 后端启动失败）。
7. **历史消息全量拉取**：实施时确认分页参数（§2.4），没有就只取最近 N 条。

---

## 八、后续阶段路标（摘要；详细拆解在开工前另写）

- **阶段 2 — 配置与进程**：配置页（读 `.env` / `persona.toml` + 写 `.env` 值 + 一键重启）；
  进程与模型（`lms server start/stop`、`lms ps --json`、模型下拉、显存策略=声明式）；
  原始事件流面板（P1 #11，可顺手补）。
- **阶段 3 — 语音与动画**：语音（移植 dev.html 音频逻辑：16k 采集 / 二进制帧 / 打断）；
  动画映射托管（后端端点 + Unity 改造；**改 Unity 前先备份**）。
- **阶段 4（可选）— 桌面壳**：等 A 做完再决定。
- **收尾**：README / `docs/08` §12 实际落点 / HANDOFF_4。

---

## 九、待用户确认的点

1. **视觉风格**：我准备做"暗色精修"（延续 /dev 的深色底与绿色强调，但重排布局、
   加大留白、气泡与状态条更柔和）。想要别的氛围（暖色 / 粉调 / 更"她"的感觉）现在说，
   样式一次定稿；之后改也只是改一个文件的 CSS。
2. **右栏信息量**：按 §2.2 放"组件状态 + 情绪 / 关系条 + 最近动作 / 表情标签"。
   希望右栏还能显示别的（时间、正在跑的工具等）就说一声；否则按此实施。