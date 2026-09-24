# Astra

Local-first AI companion：长期记忆、稳定人格、实时语音、3D 形象。

本仓库是 **Astra** 的实现。规格文档在 [`docs/`](docs/)，Agent 执行规则见 [`AGENTS.md`](AGENTS.md)。

当前进度：**M0 — 工程骨架**（能启动、能测试、数据库能从零建起）。

## 环境要求

- **Python 3.12**（由 [`uv`](https://docs.astral.sh/uv/) 托管；**不要用系统的 3.14**，torch / funasr 等生态尚未适配）
- Windows / macOS / Linux

`uv` 若不在 PATH，先补上（Windows）：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

## 快速开始

```powershell
# 1. 安装依赖（含 dev 组）
uv sync

# 2. 生成本地配置
Copy-Item .env.example .env

# 3. 初始化数据库
uv run astra migrate

# 4. 启动服务
uv run astra serve
# → http://127.0.0.1:8000/health
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run astra serve` | 启动 HTTP 服务 |
| `uv run astra migrate` | 应用数据库迁移（幂等） |
| `uv run pytest -q` | 运行全部测试 |
| `uv run ruff check .` | lint |
| `uv run ruff format --check .` | 格式检查 |
| `uv run mypy src` | 类型检查 |

## 验收（M0）

```powershell
# A. pytest passes
uv run pytest -q

# B. lint / format / type check
uv run ruff check .
uv run ruff format --check .
uv run mypy src

# C. health returns 200
uv run astra serve
# 另开终端：
$r = Invoke-WebRequest -Uri http://127.0.0.1:8000/health -UseBasicParsing
$r.StatusCode   # 期望 200

# D. empty DB can be initialized from scratch
Remove-Item .\data\astra.db* -Force -ErrorAction SilentlyContinue
uv run astra migrate
uv run astra migrate   # 再跑一次，应幂等成功
```

> `Remove-Item .\data\astra.db*` 的通配是必须的 —— WAL 模式会额外产生 `astra.db-wal` / `astra.db-shm`。
> 删库前请先停掉服务，否则 Windows 上文件被占用会删除失败。

## 项目结构

```
src/astra/
├─ main.py              # create_app() 工厂 + uvicorn 入口（唯一的依赖装配点）
├─ settings.py          # pydantic-settings 配置
├─ logging_setup.py     # 结构化日志 + 敏感字段脱敏
├─ cli.py               # serve / migrate
├─ api/                 # FastAPI 路由层
├─ domain/              # 纯数据与 Protocol（不依赖任何框架）
├─ application/         # 用例编排
├─ infrastructure/      # SQLite 等具体实现
└─ providers/           # 外部推理服务适配器（M1 起）
```

分层依赖方向为 `API -> Application -> Domain`，`Infrastructure` 实现 `Domain` 中定义的端口。
`src/astra/domain/**` 禁止 import `fastapi` / `sqlite3` / `infrastructure`，由 `tests/unit/test_architecture_rules.py` 强制执行。

## 配置

所有配置通过环境变量或 `.env` 注入，前缀 `ASTRA_`。完整键清单见 [`.env.example`](.env.example)。

`.env` 已被 `.gitignore` 忽略，**不要提交**。

## 文档

- [`docs/00_README.md`](docs/00_README.md) — 项目入口
- [`docs/08_IMPLEMENTATION_PLAN.md`](docs/08_IMPLEMENTATION_PLAN.md) — 阶段划分
- [`docs/decisions/`](docs/decisions/) — 架构决策记录（ADR）
