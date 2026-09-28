# Studio 应用 — 实施计划（阶段 2：配置与进程）

## Context

**阶段 1 已完成并交付**（`GET /studio` 产品界面：对话 / 组件状态 / 角色状态 / 记忆管理；
1078 tests passed、ruff check+format 全绿、mypy 全绿；守门测试 19 条；
真机验收由用户亲手做，见 README「验收（Studio · 阶段 1）」）。

本阶段对应设计文档 `studio-app.md` §8 的**阶段 2**与 P0 #5–#6、P1 #8–#9（+ 顺手的 P1 #11）：

| 目标（用户视角） | 对应 |
|---|---|
| 改 LLM / STT / TTS 的 provider 与模型**不用碰文件** | P0 #5 |
| 改完**一键重启**后端（而不是去终端 Ctrl+C 再敲一遍） | P0 #6 |
| 模型下拉 + 显存策略（8GB 场景的关键操作） | P1 #8 / #9 |
| 原始事件流能看（阶段 1 没做） | P1 #11（顺手补） |

配套 ADR：`docs/decisions/0026-studio-admin-and-supervisor.md`（进程模型 / 端点边界 / 写配置的四件套）。
**本文件是阶段 2 的执行依据。**

---

## 一、本轮实测（写计划前的真实数据，全部在本机跑过）

| # | 事实 | 影响 |
|---|---|---|
| 1 | **Windows 上 `os.execv` 是"另起进程 + 原进程 `_exit(0)`"**：实测原始 pid=13892 → exec 后 pid=4452，且**命令的退出码已经返回给父进程**（终端命令提前结束）；新进程脱离 `uv` 的等待 | ❌ 排除了"后端自我 re-exec 重启"这条路。**必须用监督进程**（ADR 0026 §1） |
| 2 | `lms.exe` 在 **默认 PATH** 上：`C:\Users\1\.lmstudio\bin\lms.exe`（不带 PATH 修复也能找到） | 后端可以直接 `shutil.which("lms")`，不需要配置项 |
| 3 | `lms ls --json` 字段：`type`（llm/embedding）、`modelKey`、`displayName`、`paramsString`、`sizeBytes`、`maxContextLength`、`quantization`；本机 3 个模型（nemotron-4B / qwen3.5-9B / nomic-embed） | 模型下拉的数据源；解析器按这些字段写 |
| 4 | `lms ps --json` → `[]`（当前无加载）；`lms server status` → 英文文案 `The server is not running.` | 已加载列表解析；server 状态要**解析英文文本**，不能靠 exit code |
| 5 | `uv run python -m astra.cli --version` 可用；`sys.executable` = `.venv\Scripts\python.exe` | 监督进程用 `[sys.executable, "-m", "astra.cli", "serve", "--no-supervisor"]` 拉起子进程 |
| 6 | `astra serve` 现在是 `uvicorn.run("astra.main:app", ...)`：**单进程、无 reload、无监督** | 要改成 `uvicorn.Server(...)` 显式持有对象，才能让端点请求优雅退出 |
| 7 | `Settings`: `env_prefix="ASTRA_"`、`extra="forbid"`、`case_sensitive=False`、**无 alias** | 已知键集合 = `{ASTRA_ + field.upper()}`，可由 `Settings.model_fields` 推导——不用手维护一份键清单 |
| 8 | `extra="forbid"` **只作用于 `.env` 文件**（`test_stray_env_var_is_ignored` 锁着）：进程环境里的未知 `ASTRA_*` 被忽略 | 监督者可以用 `ASTRA_SUPERVISED=1` 传递"有人管我"，不会撞校验 |
| 9 | `.env` 在仓库里**不存在**（只有 `.env.example`，≈40 个键，带分组注释）；README 现在的验收流程用**内联 `$env:ASTRA_*`** 启动 | ① 写配置的第一件事是"从 `.env.example` 复制"；② 内联环境变量**优先级高于 `.env`**，UI 必须如实标注来源（ADR 0026 §5） |

---

## 二、技术设计

### 2.1 监督进程（2a-1）

```text
uv run astra serve                     # 父：监督者，占着终端
  └─ python -m astra.cli serve --no-supervisor    # 子：uvicorn（ASTRA_SUPERVISED=1）
```

- 子进程退出码 **75** ⇒ 父进程重启它；其他码 ⇒ 父进程透传退出码后收摊。
- 决策抽成纯函数（`supervisor.py`），单测不真起进程。
- 父进程把子进程的 stdout/stderr **直接继承**（同一终端，日志照旧）。
- `--no-supervisor` 逃生门；此时 `POST /v1/admin/restart` 返回 **409 `not_supervised`**。

### 2.2 端点（全部挂在 `/v1/admin/*`，无条件挂载、进 OpenAPI）

```text
GET  /v1/admin/config          # 白名单键的值 + source(env|file|default) + restart_pending + unknown_keys + supervised
PUT  /v1/admin/config          # {"values": {"ASTRA_LLM_MODEL": "..."}} → 校验→备份→写；失败 400
POST /v1/admin/restart         # 202 或 409（not_supervised / env_invalid）
GET  /v1/admin/lmstudio        # cli 可用性 / server 状态 / 模型清单 / 已加载 / 上次的显存声明值
POST /v1/admin/lmstudio/server # {"action": "start"|"stop"}
POST /v1/admin/lmstudio/load   # {"model_key": "...", "gpu": "max"|"off"|0~1, "context_length": int|null}
POST /v1/admin/lmstudio/unload # {"identifier": "..."} | {"all": true}
```

失败用真实状态码 + 稳定错误码（控制面语义，见 ADR 0026 §2）：
`unknown_key` / `invalid_value` / `not_supervised` / `env_invalid` / `lms_unavailable` / `lms_failed`。

### 2.3 写配置的四件套（2a-2）

1. **白名单**：键 → Settings 字段（从 `Settings.model_fields` 取类型/枚举候选/default）。
   表里只放"用户真的会改"的键（LLM / embedding / STT / TTS / 记忆开关 / avatar 开关等 ≈16 个）；
   `ASTRA_LLM_API_KEY`（`SecretStr`）**不进白名单、不回显**。
2. **只改值**：逐行替换已存在键的值，保留注释、空行与顺序；键不存在则追加到文件末尾并加一行说明注释；
   `.env` 不存在 → 先复制 `.env.example`。
3. **写前校验**：把候选内容写到临时文件 → **干净环境**（剔除 `ASTRA_*`）跑
   `python -m astra.settings.env_check <临时文件>` → 它 `Settings(_env_file=...)` 实例化后回 JSON。
   能抓住跨字段约束（五条检索权重之和 = 1.0）。失败 ⇒ 不落盘，把 pydantic 的摘要回给 UI。
4. **写前备份**：`copy .env → .env.bak`（`.gitignore` 补一行）。

### 2.4 lms 集成（2b）

- 封装 `run_lms(args, timeout)`（`asyncio.create_subprocess_exec` + 超时 + 原始 stdout/stderr）。
- 解析器与执行分开：`parse_models` / `parse_loaded` / `parse_server_status` 用固定 JSON / 文本样本做单测；
  测试里用**假 runner**，真机命令永不进单测。
- `load` 超时给足（实测 ≈5s，计划取 120s），成功后把 `{model_key, gpu, context_length, at}`
  写进 `data/studio-state.json`（**声明值**，UI 标注"非实测"，ADR 0025 §6）。
- `shutil.which("lms")` 找不到 ⇒ `GET /v1/admin/lmstudio` 返回 `available: false` + 提示文案，
  其余 lms 端点返回 409 `lms_unavailable`。

### 2.5 Studio 新增页（2a-4 / 2b-3 / 2c）

- **设置页**：分组表单（枚举 → 下拉、bool → 开关、数值 → number、其余 → text），
  每行显示 **source 徽标**（进程环境 / `.env` / 默认值）；顶部一条状态带：
  `.env` 路径、`restart_pending`、`unknown_keys` 告警、是否监督模式；
  按钮「保存」与「保存并重启」。
- **重启交互**：点「保存并重启」→ PUT → POST restart（**先把 UI 切到"正在重启"**，
  请求失败也照等）→ 以 1s 间隔、2s 超时轮询 `/health`，最多 30s → 回来后刷新 `/v1/config`、
  `/v1/state`、配置页；成功提示"已用新配置起来了"，超时则如实说"没等到后端，看终端里的报错"。
- **模型页**：LM Studio 卡片（CLI 路径 / server 状态 / 启动·停止）、模型表（名称 / 类型 / 参数 / 大小 /
  上下文）、已加载区块（identifier / ctx / size）、加载表单（模型下拉 + 显存策略 max/off + context length）、
  卸载按钮、显存"声明值"提示。
- **事件页（2c，顺手补）**：前端环形缓冲（最近 300 条 `type + payload + 时间`），可暂停/清空。
  **纯前端**，不加端点。

---

## 三、目标文件树

```text
src/astra/supervisor.py                       # 新：监督循环 + 退出码决策（可单测）
src/astra/cli.py                              # 改：serve 走 uvicorn.Server + 监督；--no-supervisor
src/astra/settings/env_check.py               # 新：干净环境子进程校验（含 __main__）
src/astra/infrastructure/dotenv_store.py      # 新：.env 读/改值/追加/备份（保留注释与顺序）
src/astra/infrastructure/lmstudio_cli.py      # 新：lms CLI 封装 + 三个解析器
src/astra/infrastructure/studio_state.py      # 新：data/studio-state.json（显存声明值）
src/astra/api/routes/admin_config.py          # 新：GET/PUT /v1/admin/config（白名单表在这）
src/astra/api/routes/admin_system.py          # 新：POST /v1/admin/restart
src/astra/api/routes/admin_lmstudio.py        # 新：/v1/admin/lmstudio*
src/astra/api/app.py                          # 改：挂三个 admin 路由（无条件）
src/astra/api/static/studio.html              # 改：+设置页 +模型页 +事件页 +重启等待逻辑
tests/unit/test_supervisor.py                 # 新
tests/unit/test_dotenv_store.py               # 新
tests/unit/test_env_check.py                  # 新（真起一次子进程，标记 slow）
tests/unit/test_lmstudio_cli.py               # 新（解析器 + 假 runner）
tests/integration/test_admin_config_api.py    # 新（tmp_path 里造 .env）
tests/integration/test_admin_system_api.py    # 新（重启：假 server 对象；未监督 409）
tests/integration/test_admin_lmstudio_api.py  # 新（假 lms runner）
tests/integration/test_studio_console.py      # 改：+新页与新端点的守门断言
.gitignore                                    # 改：+.env.bak
README.md                                     # 改：启动流程改为 .env + 阶段 2 验收 + 已知限制
docs/08_IMPLEMENTATION_PLAN.md                # 改：§12 实际落点补一句
.trae/documents/studio-phase2.md              # 本文件
```

**无新依赖、无新迁移。**

---

## 四、实施顺序（每步停下来都能看见进展）

| # | 步骤 | 验证 |
|---|---|---|
| 1 | **2a-1 监督进程**：`supervisor.py` + `cli.py` 改造 + 单测 | 真机 `uv run astra serve` 起得来、Ctrl+C 干净退出、退出码 75 触发重启（临时用 `POST /v1/admin/restart` 未做时先手工验证决策函数） |
| 2 | **2a-2 配置读写**：`dotenv_store` + `env_check` + `admin_config` 路由 + 单测/集成测试 | `GET/PUT /v1/admin/config` 在 tmp 目录下自测通过；质量门 |
| 3 | **2a-3 重启端点**：`admin_system` + 与 `uvicorn.Server` 衔接 | 真机：curl 一把 `POST /v1/admin/restart` → 202 → 服务自动回来 |
| 4 | **2a-4 设置页**：Studio 配置页 + 保存并重启 + 重启等待 | **用户第一个查看点**：能在页面里改模型并重启 |
| 5 | **2b-1 `GET /v1/admin/lmstudio`**：CLI 封装 + 解析 + 端点 | 页面（或 curl）能看到 3 个模型与 server 状态 |
| 6 | **2b-2 load/unload/server 端点** + 声明值落盘 | 真机加载一次 nemotron-4B，`ps` 能看到、声明值落盘 |
| 7 | **2b-3 模型页** | 页面里加载 / 卸载 / 起停 LM Studio |
| 8 | **2c 事件页**（顺手） | 事件流实时滚动 |
| 9 | 测试补全 + 四项质量门 + README / docs/08 | 四项全绿 |
| 10 | 真机验收（§六，用户亲手） | 见 §六 |

**第 4 步之后每一步都能让用户打开 `/studio` 看**——不要憋到最后。

---

## 五、依赖变更

**无。** `subprocess` / `asyncio` / `shutil` 全是标准库；前端仍是零构建单文件。

---

## 六、验收标准

### 自动化

```powershell
uv run pytest -q                          # 1078 + 新增
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

### 真机端到端（用户亲手）

准备（**这次不再内联环境变量**——那是阶段 2 要消灭的流程）：

```powershell
Copy-Item .env.example .env
lms server start
uv run astra serve                        # → http://127.0.0.1:8000/studio（默认带监督）
```

```text
1. 设置页：看到各键的当前值与「来源」徽标（`.env` / 进程环境 / 默认值），
   `.env` 路径正确，没有 unknown_keys 告警

2. 改 ASTRA_LLM_MODEL → 「保存并重启」→ 页面进入"正在重启…" → ~2s 后自动回来，
   `/v1/config` 与设置页显示新模型；说一句话，回复确实来自新模型

3. 故意填一个非法值（例如 ASTRA_LLM_TEMPERATURE=abc）→ 保存被拒、给出原因、
   `.env` 未被写坏（`.env.bak` 还在）

4. 模型页：列出 3 个本地模型；加载 nemotron-4B（显存策略 max）→ 显示已加载（ctx / size）；
   再卸载 → 显示未加载

5. 显存策略切到 off 重新加载 → 页面把这一项标为"声明值（非实测）"

6. 停止 LM Studio 服务 → 右栏 llm 变 down 且页面不卡死 → 启动服务 → 回到 ok

7. 事件页：能实时看到 WS 事件（含 assistant.text.completed / avatar.*）

8. 关掉 Studio 浏览器页 → 后端不受影响；回到终端 Ctrl+C → 父与子**一起**干净退出，
   没有残留进程占用 8000
```

---

## 七、风险点

1. **父子进程与 Ctrl+C**（Windows 上两者都会收到信号）：判据只有"退出码是不是 75"，
   不依赖信号转发；实施第 1 步就真机验证"Ctrl+C 不会误重启"。
2. **`.env` 写坏 = 后端启动失败**（`extra="forbid"`）：四件套（白名单 / 只改值 / 写前校验 / 备份）
   是防线；另外启动失败时终端能看到 pydantic 的原始报错（父进程透传退出码）。
3. **进程环境变量盖过 `.env`**：UI 必须显示 source，否则"改了不生效"无法解释。
   真机验收第 1 步专门盯这条。
4. **重启会中断在跑的 turn**：文案要写明；Studio 侧"正在重启"状态先于请求置位。
5. **`lms` 输出的稳定性**（版本差异、英文文案）：解析器全部防御式（缺字段不炸），
   命令失败把原始 stdout/stderr 一起回给 UI。
6. **别把 Studio 做成第二套真相**：`studio-state.json` 只存"Studio 自己下发的声明值"，
   不冒充配置；配置的唯一真相仍是 `.env`。
7. **不要碰的红线**：`/health` 与 `/v1/config` 的 key 集合不动；`/dev` 不删不改；
   记忆 / 状态仍只走既有 API；不新增 Python 依赖。

---

## 八、待用户确认的点

1. **`astra serve` 默认带监督**（进程从 1 个变成 2 个，Ctrl+C 一起退，日志仍在同一终端；
   `--no-supervisor` 是逃生门）——**推荐接受**，否则"一键重启"没有立足点。
2. **README 的推荐启动流程改为 `.env`**（不再内联 `$env:ASTRA_*`）——**推荐接受**；
   内联方式仍可用，只是 Studio 会把它们标成"来自进程环境"。
3. **阶段 2 是否包含 2c 事件流页**（纯前端、约一个页面的量）——**推荐包含**，
   它是调试配置/模型问题的第一手工具。
4. 人格（`persona.toml`）**本阶段不做编辑**（只显示路径与说明），结构化编辑留 P2 #15——
   如果希望现在就做，说一声，我把它并进 2a。

---

## 九、后续（阶段 3 预告）

语音对话（移植 `dev.html` 音频链路）+ 动画映射托管（后端端点 + Unity 改造，**改前先备份**）；
`.trae/documents/studio-implementation.md` §八 的路标不变。