# ADR 0017 — Developer Console (Not a Product UI)

## Decision

新增 `GET /dev` —— 一个**开发者控制台**，单文件 HTML + 原生 JS，零构建、
零 npm、零新增 Python 依赖，由 FastAPI 的 `FileResponse` 直接提供。

- **只在 `ASTRA_ENV=dev`（默认）时挂载**。设成 `prod` 或绑到 `0.0.0.0` 时它不在
  路由表里——不引入新的配置项。
- `include_in_schema=False`：不是对外 API 的一部分。
- 内容：health 实时状态、原始事件流、对话（WS / 同步两种模式）、历史、
  协议词表、每轮的 stub/重试/耗时。

**它明确不是产品 UI。** 产品 UI（角色形象、对话气泡、设置页）是另一个东西，
需要独立的技术选型与设计，属于 `docs/01_REQUIREMENTS.md` 的 P1/P2。

## Reason

`docs/08_IMPLEMENTATION_PLAN.md` 的 8 个阶段里**没有任何 UI 里程碑**；UI 只在
`01_REQUIREMENTS.md` 的 P1/P2 里作为零散条目出现（"记忆管理 UI"、"角色状态 UI"），
`02_ARCHITECTURE.md` 的架构图里有一个 `Text UI / Voice UI / Settings / Memory UI` 框，
但前端技术栈从未被指定。

**为什么现在做**：不是因为"顺手加个功能"，而是当前工作流本身有缺口——
所有验证都只能靠读命令行输出与直接查库。接下来的两个里程碑恰好最需要肉眼检查：

- **M2 记忆**：需要看见"这条记忆为什么被创建了""这一轮检索到了什么"，
  否则验收（"重启后能在相关语境检索出来"）只能靠读数据库转述。
- **M3 状态**：emotion / relationship 是一组 0..1 的数值，没有可视化就无法判断
  衰减与转换规则是否符合预期。

**为什么用零依赖方案**：如果现在上 React / Vite，等于为一个尚未定义的 UI
提前锁定技术栈——正是 `AGENTS.md` 里"不要做投机抽象"所禁止的。原生 JS 够用，
而且这个控制台**不需要**组件化、路由、状态管理这些真正需要框架的东西。

**为什么它不会被产品 UI 淘汰**：两者看的东西不同。控制台看的是**协议与运行状态**
（原始事件、health、stub、重试、耗时），产品 UI 看的是**角色**。产品 UI 做出来之后，
排查"某个事件为什么没发"依然要靠控制台。

## Consequences

- `src/astra/api/static/dev.html` 是单文件，无构建产物需要提交。
- 打包时 hatchling 会把包目录下的非 `.py` 文件一起带上，无需额外配置。
- **安全边界**：控制台只是把本地已有的端点渲染出来，不新增任何权限。
  真正的风险是"有人把 `ASTRA_HTTP_HOST` 设成 `0.0.0.0` 却忘了关它"——
  所以用 `ASTRA_ENV` 作为开关，并在 `.env.example` 与 README 里写明。
- **渲染必须用 `textContent`**，绝不拼 HTML：模型输出与用户输入都是不可信内容。
  这条由 `tests/integration/test_dev_console.py` 守着。
- 控制台自身不引任何外部资源（CDN / 字体 / 脚本），保证离线可用；同样有测试守着。
