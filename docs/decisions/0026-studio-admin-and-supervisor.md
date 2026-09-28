# ADR 0026 — Studio 阶段 2：一键重启的进程模型与配置写入的边界

> 本文落实 ADR 0025 §2 已拍板的方向（"写 `.env` + 一键重启后端"）——那份 ADR 定的是
> **方向**，本文定的是**机制**，并回答实施前实测暴露的两个新问题：
> ① 在 Windows 上"后端重启自己"到底能不能做；② 写 `.env` 怎么保证不把后端写坏。
> 配套实施计划：`.trae/documents/studio-phase2.md`。

## Decision

### 1. `astra serve` 默认以**监督进程**运行（父进程 + 子进程）

```text
uv run astra serve                # 父：监督者（持有终端）
  └─ python -m astra.cli serve --no-supervisor   # 子：uvicorn 真正在跑
```

- 子进程以**退出码 75**（EX_TEMPFAIL）退出 ⇒ 父进程**重启它**；
  其他退出码 ⇒ 父进程一起退出并**透传那个码**（配置写坏时终端能看到 pydantic 的报错）。
- `Ctrl+C`：两个进程都在同一控制台，都会收到；父进程以"**这次退出是不是我请求的**"为唯一判据
  决定重启还是收摊——不依赖信号转发。
- 逃生门 `astra serve --no-supervisor`：退回"前台单进程"（今天的形态）。

**为什么不用自我 `os.execv` 重启**（本轮实测，见 §实测）：Windows 上 `os.execv` 是
"**另起一个进程 + 原进程立即 `_exit(0)`**"，于是父进程（`uv`）看到子进程退出就一起退出、
终端命令提前结束，而新进程变成**脱管**的孤儿。看起来"服务还在"，但用户已经拿不回那个终端了。

**为什么不用"另起一个 detached 进程"**：会留下静静在后台跑的幽灵后端（端口占用、
日志丢失、用户以为它已经关了），与本项目"如实、可解释"的风格冲突。

子进程由父进程设置 `ASTRA_SUPERVISED=1`——注意这是个**进程环境变量**，
而 `extra="forbid"` 只作用于 `.env` 文件（`tests/unit/test_settings.py::test_stray_env_var_is_ignored`
锁着这条边界），所以不会撞 `Settings`。

### 2. 新增 `/v1/admin/*` 管理端点（**无条件挂载**，进 OpenAPI，不设鉴权）

| 端点 | 作用 |
|---|---|
| `GET /v1/admin/config` | 可编辑键的**当前值 + 来源**（`env` / `file` / `default`）+ 是否"改了还没重启" + `.env` 未知键告警 |
| `PUT /v1/admin/config` | 只改**白名单键的值**（见 §3），写前校验、写前备份 |
| `POST /v1/admin/restart` | 请求重启；未监督时 **409**，配置非法时 **409**（都不静默） |
| `GET /v1/admin/lmstudio` | `lms` 可用性、server 状态、本地模型清单、已加载模型、Studio 上次的显存声明值 |
| `POST /v1/admin/lmstudio/server` | `lms server start\|stop` |
| `POST /v1/admin/lmstudio/load` | `lms load <key> --gpu max\|off\|0~1 [-c N]`，并记下声明值 |
| `POST /v1/admin/lmstudio/unload` | `lms unload <identifier>` / 全部 |

- `/health` 与 `/v1/config` 的 key 集合**不动**（ADR 0006 / ADR 0016）——新信息一律走 `/v1/admin/*`。
- 挂载规则与 `/studio` 一致（无条件，ADR 0025 §5）：安全边界是默认绑定 `127.0.0.1`，
  给 UI 加门控而 `/v1` 本身没有鉴权并无额外收益。
- 这是**控制面**，因此与"HTTP 恒 200、严重程度写在 body"（ADR 0006）不同：
  失败用真实状态码 + 稳定错误码（`not_supervised` / `env_invalid` / `unknown_key` /
  `invalid_value` / `lms_unavailable` / `lms_failed`）。

### 3. 配置写入 = **白名单 + 值替换 + 写前子进程校验 + 写前备份**

- **白名单**：只有列在表里的键能被 Studio 改（LLM / embedding / STT / TTS 的 provider 与模型、
  地址、音色与参考音频、若干数值开关）。键到 Settings 字段的映射来自
  `Settings.model_fields`（实测 `env_prefix="ASTRA_"`、无 alias），**未知键直接拒绝**。
  `ASTRA_LLM_API_KEY`（`SecretStr`）**不在白名单**：不进 UI，也不回显。
- **只改值、不增删键、保留注释与顺序**：逐行替换已存在键的值；`.env` 不存在则先从
  `.env.example` 复制；写前把现有文件备份成 `.env.bak`。
- **写前校验**：用**干净环境**（剔除所有 `ASTRA_*` 进程变量）起一个子进程，
  让它 `Settings(_env_file=<候选文件>)`——能抓住跨字段约束（例如五条检索权重之和必须为 1.0），
  校验不过就**不落盘**并把 pydantic 的错误摘要回给 UI。
  用子进程而不是在校验进程内直接实例化，是因为**进程环境变量优先级高于 `.env`**：
  直接在进程内校验会验到"启动时内联的那套值"而不是候选文件。

### 4. LM Studio 集成走 `lms` CLI（不新增依赖，不做 REST 加载）

- 实测 `lms.exe` 就在**默认 PATH** 上（`C:\Users\1\.lmstudio\bin\lms.exe`），不需要配置路径；
  找不到时如实报 `lms_unavailable`，UI 明确提示而不是显示空列表。
- 加载/卸载/服务开关用 CLI（实测 `lms load --gpu ... ` ≈5s、`lms ps --json`、`lms server status`）；
  REST 侧**不暴露** "模型在 GPU 还是 CPU"（ADR 0025 §6 已实测），所以
- **显存策略仍是"声明值"**：Studio 记录自己最后一次下发的 `--gpu` 参数，
  存在 `data/studio-state.json`（用户数据目录，不是配置真相），UI 上明确标注"声明，非实测"。

### 5. 配置生效的唯一路径仍是"写 `.env` + 重启"（不做运行时热更新）

ADR 0025 §2 的结论不变。补充一条实施约束：**进程环境变量会盖过 `.env`**，
所以 `GET /v1/admin/config` 必须为每个键返回 `source`——否则用户内联了
`$env:ASTRA_LLM_MODEL=...` 启动时，会在 Studio 里改了 `.env`、重启、发现"没生效"而不知道为什么。

## Reason

- **为什么监督者而不是 exec/幽灵进程**：见 §1 的实测。监督者是唯一同时满足
  "终端还在、Ctrl+C 还管用、日志还看得到、没有孤儿进程"的做法；
  而且**阶段 4 的桌面壳天然就是监督者**——那时它接管同一个子进程协议即可，不需要重做。
- **为什么配置写入要这么小心**：`extra="forbid"` 意味着"写坏 = 下次启动直接失败"，
  而这个文件是用户会手看的单一真相（ADR 0025 §2）。白名单 + 只改值 + 写前校验 + 备份
  四件套的成本很低，坏处是"改一项要重启"——那是用户已接受的代价。
- **为什么 admin 端点不做鉴权**：与 ADR 0025 §5 同一条理由；真要暴露到局域网时，
  该解决的是"整个 `/v1` 都没有鉴权"，而不是只给一个页面加锁。

## Consequences

- `astra serve` 从"一个进程"变成"两个进程"：日志仍在同一终端，Ctrl+C 一起退，
  `--no-supervisor` 是逃生门。README 的启动说明要改。
- **重启会中断在跑的 turn**（配置变更本来就意味着重启）；Studio 侧用健康轮询等待回归，
  期间页面显示"正在重启"，WS 自动重连（阶段 1 已有）。
- 启动期配置非法（`.env` 被手改坏）⇒ 子进程退出、父进程透传退出码、终端可见报错。
  Studio 写配置时的前置校验是防这条的第一道门（第二道是 `.env.bak`）。
- **README 的推荐启动流程改为"复制 `.env.example` → 在 Studio 里改"**：
  内联 `$env:ASTRA_*` 的做法在 Studio 能写配置之后不再是推荐路径（但它仍然能工作，
  只是 Studio 会如实标注这些键"来自进程环境"）。
- `docs/08` §12 的"实际落点"在阶段 2 结束后补一句（配置与进程管理已落地）。
- 本阶段**不新增 Python 依赖**（`subprocess` / `asyncio` 标准库）；仍是零构建前端。
- 人格（`persona.toml`）**仍不提供编辑**：阶段 2 只显示它的路径与"改它要手编 toml"，
  结构化编辑留在 P2 #15。