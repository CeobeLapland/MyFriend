# ADR 0003 — Package Name and Layout

## Decision

- import 名与 distribution 名统一为 **`astra`**（`src/astra/`）。
- **不采用** `02_ARCHITECTURE.md` §6 建议的 `apps/{api,worker,cli}` 目录。
  FastAPI 应用放在 `src/astra/api/`，CLI 放在 `src/astra/cli.py`，
  ASGI 工厂与依赖装配点放在 `src/astra/main.py`。

## Reason

`apps/` 的唯一价值是"多个可执行入口共享一个 src 包"，但 ADR 0002 已确定 MVP 单进程。
M7 的 Scheduler 在单进程模型下必然是进程内模块，`apps/worker/` 会永远空着。
CLI 只有两个子命令，用 stdlib `argparse` 约 40 行即可，不需要 `typer` / `click`。

包名沿用文档代号 `astra`：`02_ARCHITECTURE.md` §6 已把 `src/astra/` 写死，
`09_AGENT_INSTRUCTIONS.md` 也以 Astra 称呼本项目。仓库目录名 `MyFriend` 只是
宿主容器，不参与命名。本地项目不发布到 PyPI，无重名风险。

## Consequences

- 与 `02_ARCHITECTURE.md` §6 的目录建议存在差异，本 ADR 即为该差异的记录。
- `unity/` 目录延后到 M5；根级 `schemas/` 不建立（见 ADR 0007）。
- 未来若真的需要拆进程（见 ADR 0002 的触发条件），再引入 `apps/` 并在此 ADR 上追加。
