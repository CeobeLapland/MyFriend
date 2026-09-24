# Astra

Local-first AI companion：长期记忆、稳定人格、实时语音、3D 形象。

本仓库是 **Astra** 的实现。规格文档在 [`docs/`](docs/)，Agent 执行规则见 [`AGENTS.md`](AGENTS.md)。

当前进度：**M2 完成**（长期记忆 + 记忆管理 API）。
`文字进 → LM Studio → 结构化回复出`，带持久化、实时事件流、降级，
以及**跨重启的记忆**（提取 → 打分 → 去重 → 向量检索 → 用户可控）。

## 环境要求

- **Python 3.12**（由 [`uv`](https://docs.astral.sh/uv/) 托管；**不要用系统的
  3.14**，torch / funasr 等生态尚未适配）
- **LM Studio** 并加载：
  - `qwen/qwen3.5-9b`（或改 `ASTRA_LLM_MODEL`）
  - `text-embedding-nomic-embed-text-v1.5`（记忆需要，或改 `ASTRA_EMBEDDING_MODEL`）
- Windows / macOS / Linux

> **8GB 显存提示**：chat 模型与 embedding 模型同时加载可能装不下。
> 装不下时把 `ASTRA_EMBEDDING_PROVIDER=none`——记忆子系统会整体降级为
> "不检索、不写入"，**聊天不受影响**。

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

## 开发者控制台

浏览器打开 **http://127.0.0.1:8000/dev**

左边是对话（可在 WebSocket / 同步两种模式间切换），右边是**原始事件流**。
顶部是 health 实时状态（每 5 秒刷新），底部可展开看运行配置、协议词表，
以及**记忆面板**（列表 / 单条删除 / 清空 / 启用开关）。

**这是开发工具，不是产品 UI**（见 [ADR 0017](docs/decisions/0017-developer-console.md)）。
它看的是协议与运行状态——原始事件、health、stub、重试、耗时、记忆；产品 UI 看的是角色。
两者不冲突，控制台在产品 UI 做出来之后依然有用。

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
├─ settings.py          # pydantic-settings 配置
├─ logging_setup.py     # 结构化日志 + 敏感字段脱敏
├─ cli.py               # serve / migrate
├─ api/                 # FastAPI 路由层 + HTTP 契约模型
├─ domain/              # 纯数据与 Protocol（不依赖任何框架）
├─ application/         # 用例编排：orchestrator / conversation / prompt / parser / memory
├─ infrastructure/      # SQLite 仓储与迁移、人格 TOML 加载
└─ providers/lmstudio/  # 唯一允许 import httpx 的地方（chat + embeddings）
```

分层依赖方向为 `API -> Application -> Domain`，`Infrastructure` 实现 `Domain`
中定义的端口。`src/astra/domain/**` 禁止 import `fastapi` / `sqlite3` /
`infrastructure`；`domain`、`application`、`api` 都禁止 import HTTP 客户端——
由 `tests/unit/test_architecture_rules.py` 强制执行。

## 已知限制

- **不做流式输出**（留到 P1）。一轮回复耗时约 7–12 秒，期间客户端只能等。
- **记忆让每轮多约 2.4 秒**。候选是随回复内联产出的（ADR 0018 的实测数据），
  换来的是"落库即持久"——否则说完立刻关程序会丢记忆。关掉
  `ASTRA_EMBEDDING_PROVIDER` 或 `PUT /v1/memories/settings {"enabled": false}`
  即可把这 2.4 秒拿回来。
- **同一会话的 turn 是串行的**：一次长 LLM 调用会阻塞该会话的下一轮。
  单用户场景可接受，M4 做 interruption 时重新评估。
- **`apply_state_delta` 仍是显式空桩**（M3 实现）。`TurnResult.stubs_used` 会如实
  上报它，`tests/unit/test_orchestrator.py` 有一条「桩账本」断言——
  M3 实现它时会变红，强制显式更新。
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
- 尚未接 STT / TTS / Unity（M4 / M5）。

## 文档

- [`docs/00_README.md`](docs/00_README.md) — 项目入口
- [`docs/04_API_AND_EVENTS.md`](docs/04_API_AND_EVENTS.md) — HTTP / WebSocket 协议
- [`docs/08_IMPLEMENTATION_PLAN.md`](docs/08_IMPLEMENTATION_PLAN.md) — 阶段划分
- [`docs/decisions/`](docs/decisions/) — 架构决策记录（ADR）
