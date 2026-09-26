# M0 工程骨架 — 实施计划

## Context

`MyFriend` 目前是一个只有 `docs/`（13 份规格文档 + 2 个 JSON Schema）的空仓库，没有任何代码。

目标是实现 **Astra**（本地 AI 伙伴）项目的 **M0 阶段**——按 `docs/AGENT_KICKOFF.md` 的定义，M0 只交付"能启动、能测试、数据库能从零建起"的工程骨架，**不碰 Unity / STT / TTS / Agent 行为**（那些是 M1 之后）。

M0 的意义是把后面 7 个里程碑的地基一次性打对：分层边界、配置、日志、迁移机制、健康检查协议。这些一旦定错，后面每个阶段都要还债。

**环境（已实测确认）**
- `uv 0.12.18`（不在 PATH，需 `$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"`），Python 3.12.14 由 uv 托管
- Python 3.12 的 `sqlite3` = SQLite 3.53.1，`threadsafety == 3`
- LM Studio 可达：`qwen/qwen3.5-9b`、`nvidia/nemotron-3-nano-4b`、`text-embedding-nomic-embed-text-v1.5`
- 硬件：RTX 4060 Laptop 8GB / 16GB RAM —— M0 只用 LM Studio，压力小

**验收标准（文档原文）**：`pytest passes` / `health returns 200` / `empty DB can be initialized from scratch`

---

## 一、技术选型结论

| 项 | 结论 | 理由 |
|---|---|---|
| 包名 | `astra`（import 名与 distribution 名统一） | 用户确认；`02_ARCHITECTURE.md` §6 已写死 `src/astra/` |
| 目录 | **不建 `apps/`** | ADR 0002 已定单进程；`apps/worker` 会永远空着。API 放 `src/astra/api/`，CLI 放 `cli.py` |
| 数据访问 | **stdlib `sqlite3`（同步）+ `asyncio.to_thread`** | 09 号文档要求"先看 stdlib"、"依赖面小"；02 号文档 §5 禁止 `Domain -> ORM` |
| 连接策略 | 每个工作单元一个新连接 + PRAGMA（WAL / foreign_keys / busy_timeout） | SQLite 打开是微秒级；天然规避跨线程共享 |
| Migration | **手写 runner（~70 行，零依赖）** | DDL 在文档里已是原生 SQL，Alembic 的价值全用不上且会拖入 SQLAlchemy |
| 配置 | `pydantic-settings`，**扁平** `Settings`（`env_prefix="ASTRA_"`，`extra="forbid"`） | Pydantic 已是既有依赖；扁平避免 v2 嵌套子模型的 env 填充歧义 |
| lint/format | `ruff`（lint + format 一体） | 替代 flake8+isort+black 三件套 |
| 类型检查 | `mypy --strict` + `pydantic.mypy` 插件 | 纯 Python 包；新仓库从零开始，strict 边际成本最低 |
| 日志 | stdlib `logging` + 自写 `JsonFormatter` + 敏感键脱敏 Filter | 零依赖拿到 90% 价值；structlog 的核心价值要到 M1 才兑现 |
| CLI | stdlib `argparse`（`serve` / `migrate` 两个子命令） | 不引入 typer/click |

**依赖清单**

运行时：`fastapi`、`uvicorn`（**不加 `[standard]`**，uvloop 在 Windows 无效）、`pydantic`、`pydantic-settings`

开发：`pytest`、`ruff`、`mypy`、`httpx`（TestClient 的底层依赖）

**明确不加**：`aiosqlite`、`SQLAlchemy`、`SQLModel`、`alembic`、`structlog`、`loguru`、`typer`、`python-dotenv`、`pytest-asyncio`、`sqlite-vec`、`websockets`

---

## 二、目标文件树

```
MyFriend\
├─ .gitattributes                 # * text=auto eol=lf（抵消 core.autocrlf=true 的 CRLF 抖动）
├─ .gitignore                     # .env / data/ / .venv / *.db* / cache
├─ .editorconfig                  # UTF-8 + LF + 4 空格
├─ .python-version                # "3.12"（防止 uv 挑到系统的 3.14）
├─ .env.example                   # 全部配置键示例，无真实密钥
├─ AGENTS.md                      # 09_AGENT_INSTRUCTIONS.md 的落地副本
├─ README.md                      # 安装/运行/测试/验收命令
├─ pyproject.toml                 # 元数据 + 依赖 + ruff/mypy/pytest 配置
├─ uv.lock                        # 提交
├─ data\.gitkeep                  # 运行期 SQLite 目录（内容 gitignore）
├─ docs\                          # 现有规格，不改
│  └─ decisions\0003~0008-*.md    # 本次新增 ADR
├─ src\astra\
│  ├─ __init__.py                 # __version__
│  ├─ main.py                     # create_app() 工厂 + uvicorn 入口；唯一的依赖装配点(DI)
│  ├─ settings.py                 # pydantic-settings 扁平 Settings + get_settings()
│  ├─ logging_setup.py            # JsonFormatter + 敏感键脱敏 Filter + configure_logging()
│  ├─ cli.py                      # argparse: serve / migrate
│  ├─ api\
│  │  ├─ app.py                   # FastAPI 实例、lifespan(建库)、路由注册
│  │  └─ routes\health.py         # GET /health
│  ├─ domain\
│  │  ├─ health.py                # HealthStatus 枚举 / ComponentHealth / HealthReport（纯数据）
│  │  └─ ports.py                 # Protocol: HealthProbe
│  ├─ application\
│  │  └─ health_service.py        # 汇总 probe → 计算整体 status（降级策略唯一实现处）
│  ├─ infrastructure\db\
│  │  ├─ connection.py            # connect() + PRAGMA
│  │  ├─ migrations.py            # upgrade() / status() / expected_version()
│  │  ├─ health.py                # DatabaseHealthProbe
│  │  └─ migrations\0001_init.sql # 03_DATA_MODEL §3 的 DDL 原文 + 3 个索引（逐字照抄）
│  └─ providers\__init__.py       # M0 仅占位包（02 §6 要求该层存在）
└─ tests\
   ├─ conftest.py                 # 临时 DB、干净 Settings、TestClient、默认库保护
   ├─ unit\
   │  ├─ test_settings.py
   │  ├─ test_migrations.py
   │  ├─ test_logging.py
   │  ├─ test_health_service.py
   │  └─ test_architecture_rules.py   # AST 扫描：domain 不得 import fastapi/sqlite3/infrastructure
   └─ integration\test_health_api.py
```

**与 `02_ARCHITECTURE.md` §6 的差异**（记入 ADR）：无 `apps/`；无 `unity/`（延后 M5）；无根级 `schemas/`（沿用 `docs/schemas/`）；`worker` 不是独立目录。

---

## 三、关键实现要点

### migration 0001 只建 3 张表

`0001_init.sql` = `conversations` + `messages` + `memories` + 文档里的 3 个索引，**逐字照抄 `03_DATA_MODEL.md` §3**。

理由：这三张是文档给了**完整 DDL** 的。其余 8 个实体（users / companions / emotion_states / relationship_states / persona_traits / events / tool_executions / memory_embeddings）只有字段列表，类型、主键、宽表还是窄表全要猜——猜错就要写迁移去改。等 M2/M3 设计定型时再建。

`schema_migrations` 表由 runner 自己建，不在 0001 里。

### migration runner 的事务语义（实测验证过，最容易写错）

`sqlite3.Connection.executescript()` 的语义是"若有未提交事务先隐式 COMMIT，除此之外不做事务控制"。所以：

- **不能**在外层 `BEGIN` 后再调 `executescript`
- 正确做法：把事务控制**拼进脚本文本** → `"BEGIN;\n" + 文件内容 + "\nINSERT INTO schema_migrations ...;\nCOMMIT;"`
- 配合 `isolation_level=None`
- 失败分支必须写 `if conn.in_transaction: conn.execute("ROLLBACK")`——直接 `ROLLBACK` 在无事务时抛 `OperationalError`
- 迁移文件内**禁止**出现 `BEGIN`/`COMMIT`（用单元测试断言）
- `checksum` 用 `hashlib.sha256` 校验，已应用的迁移被改动时报错而非静默跳过

### /health 契约（M0 一次定死 key 集合，M4 只改 value）

```json
{
  "status": "ok",
  "version": "0.1.0",
  "schema_version": 1,
  "uptime_s": 3.42,
  "components": {
    "api":      {"status": "ok",             "latency_ms": 0.0,  "detail": null},
    "database": {"status": "ok",             "latency_ms": 1.8,  "detail": {"schema_version": 1}},
    "llm":      {"status": "not_configured", "latency_ms": null, "detail": null},
    "stt":      {"status": "not_configured", "latency_ms": null, "detail": null},
    "tts":      {"status": "not_configured", "latency_ms": null, "detail": null}
  }
}
```

- 组件状态枚举：`ok | degraded | down | not_configured | disabled`
- **5 个 key 现在就全输出**，值 `not_configured`，让客户端从 M0 起就能写死解析逻辑
- **HTTP 状态码恒 200**（进程活着就 200），严重程度只在 body 表达。依据：验收要求"health returns 200"；`01_REQUIREMENTS.md` §3 要求任何外部依赖不可用都要降级继续运行，不存在"该返 503"的语义
- M0 **不实现 LLM 探针**（kickoff 第 7 条），M1 注册 `HttpReachabilityProbe` 即可，协议零改动

### 测试要点

- FastAPI 用 **`TestClient` 的 `with` 块**（同步），**不用** `httpx.ASGITransport`——后者不触发 lifespan，会得到"no such table"的诡异失败
- DB 测试用 **`tmp_path` 真实临时文件**，**不用** `:memory:`——每条连接是独立库，且无法验证 WAL / 重启持久性
- `conftest.py` 必须 `monkeypatch.delenv` 清 `ASTRA_*` + `Settings(_env_file=None, ...)`，防止本机 `.env` 污染测试
- 加一个 session 级 fixture 断言测试期间默认的 `data/astra.db` 未被创建（防污染真实数据）

---

## 四、实施顺序

每步以"该步验证命令通过"为完成标志。

| 步 | 内容 | 验证 |
|---|---|---|
| 0 | 仓库卫生：`.gitattributes` / `.gitignore` / `.editorconfig` / `.python-version` / `AGENTS.md` | `git status` 干净 |
| 1 | `uv init --app --package --name astra --python 3.12`，手写 `pyproject.toml`，`uv add` 依赖，配 ruff/mypy/pytest | `uv sync` / `uv run ruff --version` |
| 2 | `settings.py` + `.env.example` | `uv run pytest tests/unit/test_settings.py -q` |
| 3 | `logging_setup.py` | `uv run pytest tests/unit/test_logging.py -q` |
| 4 | `domain/health.py` + `domain/ports.py` + `application/health_service.py` | `uv run pytest tests/unit/test_health_service.py -q` |
| 5 | `infrastructure/db/`（连接 + migration runner + `0001_init.sql`） | `uv run pytest tests/unit/test_migrations.py -q` |
| 6 | `infrastructure/db/health.py`（DatabaseHealthProbe） | 并入 test_health_service |
| 7 | `api/` + `main.py` + `cli.py` | `uv run pytest tests/integration/test_health_api.py -q` |
| 8 | `conftest.py` 收口 + `test_architecture_rules.py` | `uv run pytest -q` 全绿 |
| 9 | ADR 0003~0008 + README | `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src` |
| 10 | 手工验收 + 写 Phase 0 报告 | 见下方验证节 |

---

## 五、验收与验证命令（PowerShell）

```powershell
# 前置：uv 不在 PATH
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# A. pytest passes
uv sync
uv run pytest -q

# B. lint / format / type check
uv run ruff check .
uv run ruff format --check .
uv run mypy src

# C. health returns 200
uv run astra serve                       # 终端 1
$r = Invoke-WebRequest -Uri http://127.0.0.1:8000/health -UseBasicParsing   # 终端 2
$r.StatusCode                            # 期望 200
$r.Content | ConvertFrom-Json | ConvertTo-Json -Depth 5

# D. empty DB can be initialized from scratch
Remove-Item .\data\astra.db* -Force -ErrorAction SilentlyContinue
uv run astra migrate
uv run python -c "import sqlite3;c=sqlite3.connect('data/astra.db');print(sorted(r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")));c.close()"
# 期望：['conversations','memories','messages','schema_migrations']
uv run astra migrate                     # 再跑一次，应幂等成功
```

注意 `Remove-Item .\data\astra.db*` 的通配是必须的（WAL 会留下 `-wal`/`-shm`），且删库前必须停掉服务。

---

## 六、需要记录的 ADR

| 文件 | 结论 |
|---|---|
| `0003-package-name-and-layout.md` | 包名统一 `astra`；取消文档建议的 `apps/`；API 放 `src/astra/api/`、CLI 放 `cli.py` |
| `0004-sqlite-without-orm.md` | stdlib `sqlite3`（同步、每工作单元一连接、WAL），仅在调用边界用 `asyncio.to_thread`；端口层保持 `async def`；不引入 ORM / aiosqlite |
| `0005-hand-rolled-sql-migrations.md` | 手写 runner + `schema_migrations` + 单事务原子应用；不用 Alembic |
| `0006-health-contract.md` | `/health` 固定 5 组件 key；含 `not_configured` 枚举；HTTP 恒 200 |
| `0007-schema-and-deferred-dirs.md` | JSON Schema 留在 `docs/schemas/`；根级 `schemas/`、`unity/` 延后 |
| `0008-dependency-direction-enforced.md` | Domain 只定义 Protocol，由 `test_architecture_rules.py` 作为门禁强制执行 |
| `0009-voice-and-avatar-constraints.md` | 记录已确认的两条外部约束：CosyVoice 雨落版**不支持流式**（Gradio + 文件路径返回，端口 7860），TTS/STT 需按句切分折中；Unity 侧**可以复用原工程**，用户已经把除了 `Celandine.vrm` 模型资源以外的脚本都删了，旧脚本一律不用 |

---

## 七、风险点

**SQLite / 迁移**
1. `executescript` 的事务语义（见上），必须脚本内嵌 `BEGIN/COMMIT` + `if conn.in_transaction` 守卫
2. `PRAGMA foreign_keys` 每连接生效，`journal_mode=WAL` 持久生效，别搞混
3. WAL 留下 `-wal`/`-shm`，清理脚本和 `.gitignore` 都要覆盖
4. `data/` 可能不存在，`connect()` 前必须 `mkdir(parents=True, exist_ok=True)`

**Windows / 环境**
5. `core.autocrlf=true` + ruff format 会打架 → `.gitattributes` 强制 `eol=lf`，并建议设仓库本地 `git config core.autocrlf false`（**需要用户确认**，会写 `.git/config`）
6. cp936 编码：日志 handler 显式 `encoding="utf-8"`，入口 `sys.stdout.reconfigure(encoding="utf-8")`，否则中文日志重定向时 `UnicodeEncodeError`
7. `.python-version` 必须固定 `3.12`，否则 `uv run` 可能挑到系统的 3.14

**FastAPI / 测试**
8. `httpx.ASGITransport` 不触发 lifespan → 用 `TestClient` 的 `with` 块
9. `uvicorn.run(..., log_config=None)`，避免与自配 logging 冲突
10. `extra="forbid"` 的副作用：`.env` 里任何未声明的键都会让启动失败 → `.env.example` 里预留键必须保持注释状态

**范围纪律**
11. M0 **不**实现 `LLMProvider`、事件总线、`/v1/chat/messages`——那是 M1。提前做会让 M0 验收无法独立判定
12. `Settings` 不得传进 `domain/`，否则架构测试会红

---

## 八、完成后要交付的报告（kickoff 要求）

`files created` / `commands to run` / `test results` / `known limitations` / 推荐的下一里程碑
