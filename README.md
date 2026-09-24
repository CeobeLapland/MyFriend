# Astra

Local-first AI companion：长期记忆、稳定人格、实时语音、3D 形象。

本仓库是 **Astra** 的实现。规格文档在 [`docs/`](docs/)，Agent 执行规则见 [`AGENTS.md`](AGENTS.md)。

当前进度：**M1 完成**（文本大脑 + 事件总线 + WebSocket 通道）。
`文字进 → LM Studio → 结构化回复出`，带持久化、实时事件流与降级。

## 环境要求

- **Python 3.12**（由 [`uv`](https://docs.astral.sh/uv/) 托管；**不要用系统的
  3.14**，torch / funasr 等生态尚未适配）
- **LM Studio** 并加载 `qwen/qwen3.5-9b`（或改 `ASTRA_LLM_MODEL`）
- Windows / macOS / Linux

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
# 编辑 .env，把 ASTRA_LLM_PROVIDER 改成 lmstudio

# 3. 启动 LM Studio 并加载 qwen/qwen3.5-9b（在 LM Studio 里点开 Developer → Start Server）

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
├─ application/         # 用例编排：orchestrator / conversation / prompt / parser
├─ infrastructure/      # SQLite 仓储与迁移、人格 TOML 加载
└─ providers/lmstudio/  # 唯一允许 import httpx 的地方
```

分层依赖方向为 `API -> Application -> Domain`，`Infrastructure` 实现 `Domain`
中定义的端口。`src/astra/domain/**` 禁止 import `fastapi` / `sqlite3` /
`infrastructure`；`domain`、`application`、`api` 都禁止 import HTTP 客户端——
由 `tests/unit/test_architecture_rules.py` 强制执行。

## 已知限制

- **M1 不做流式输出**（留到 P1）。一轮回复耗时约 3–9 秒，期间客户端只能等。
- **同一会话的 turn 是串行的**：一次长 LLM 调用会阻塞该会话的下一轮。
  单用户场景可接受，M4 做 interruption 时重新评估。
- **M2/M3 的功能是显式空桩**：`retrieve_memory` / `apply_state_delta` /
  `enqueue_memory_extraction` 返回空值并记 DEBUG 日志。`TurnResult.stubs_used`
  会如实上报它们，`tests/unit/test_orchestrator.py` 有一条「桩账本」断言——
  M2 实现任何一个都会让它变红，强制显式更新。
- **人格规则并不能 100% 约束模型**：实测在全新会话里，模型仍会说出
  "我正试着把刚才那几段对话的碎片拼起来看"这类**编造记忆**的话，
  尽管 prompt 里明确写了"不要编造记忆"。这属于 prompt 遵从度问题，
  需要 `docs/10_TESTING_AND_ACCEPTANCE.md` §5 的固定对话集来量化，
  并在 M2 引入真实记忆后重新评估。
- 尚未接 STT / TTS / Unity（M4 / M5）。

## 文档

- [`docs/00_README.md`](docs/00_README.md) — 项目入口
- [`docs/04_API_AND_EVENTS.md`](docs/04_API_AND_EVENTS.md) — HTTP / WebSocket 协议
- [`docs/08_IMPLEMENTATION_PLAN.md`](docs/08_IMPLEMENTATION_PLAN.md) — 阶段划分
- [`docs/decisions/`](docs/decisions/) — 架构决策记录（ADR）
