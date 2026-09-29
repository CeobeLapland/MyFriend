# ADR 0030 — 桌面壳：`astra desktop`（pywebview 单进程 + 接管监督者）

> 用户拍板：桌面壳用 **pywebview**（2026-09-28）。这一步的目标是"一个窗口 + 托盘 +
> 进程管理"，把 Studio 从浏览器搬进独立应用窗口；Unity 的合并另见 ADR 0031。
> ADR 0025 §2 早就写明"将来若要独立应用窗口 + 系统托盘 + 进程管理，再给这个网页套
> 桌面壳（Tauri / PyWebView），**前端代码不重写**"，ADR 0026 §86 写明"桌面壳天然就是
> 监督者，接管同一个子进程协议即可"——本 ADR 把这两条兑现。

## 本轮实测（定案前在机器上核过）

| # | 事实 | 影响 |
|---|---|---|
| 1 | WebView2 运行时已装：`136.0.3240.64`（注册表 EdgeUpdate Clients） | pywebview 的 EdgeChromium 后端不需要额外安装运行时 |
| 2 | `uv pip install --dry-run pywebview` → **pywebview 6.2.1** + 6 个传递依赖：pythonnet 3.1.0 / clr-loader 0.3.1 / bottle 0.13.4 / proxy-tools 0.1.0 / cffi 2.1.1 / pycparser 3.0 | 新增依赖面就这么大，且**只在桌面包里**（见 §4） |
| 3 | 本机 **没有 Rust** 工具链；有 Node 24 / npm 11、.NET 9 SDK | Tauri 要新装整条工具链（否决）；Electron / .NET 可行但被 §2 的理由否决 |
| 4 | `supervisor.py` 已有：`child_command()`、`is_supervised()`、`run_supervised()`、`EXIT_RESTART=75`；`cli.py` 的 `--no-supervisor` 帮助文本已写"供脚本、或**自己当监督者的桌面壳**使用" | 壳**零新增监督逻辑**：工作线程里跑 `run_supervised(child_command(), {**os.environ, SUPERVISED_ENV: "1"})` |
| 5 | Studio 是 `GET /studio` 的静态页，同源下 WebSocket 相对路径 `/ws/v1/session/{id}` | 壳用 **URL**（`http://127.0.0.1:{port}/studio`）加载，不用 `file://`——见 Reason |

## Decision

### 1. 入口：`astra desktop`（第 4 个子命令）

- 主线程跑 pywebview 窗口（GUI 必须在主线程），**工作线程**跑既有的 supervisor 循环。
- 启动顺序：读 `Settings` → 探 `GET /health`：**已有后端在跑就不拉起第二个**
  （用户自己开着 `astra serve` 时壳复用它，如实提示；那个后端的"一键重启"按它自己的
  监督状态行事——被监督的照常可用，`--no-supervisor` 起的会如实 409）→ 没有才拉子进程 →
  等 `/health` 通过 → 打开窗口。
- 退出顺序：**先停 Unity（如果起了）→ 再停后端 → 最后关窗口**。后端停机走 supervisor
  新增的可选 `stop` 事件（轮询式等待 + 优雅信号 → 宽限 → terminate），
  `astra serve` 的既有调用路径**行为不变**（`stop=None` 时逐字节等价）。

### 2. 窗口、托盘与单实例

- 窗口标题 `Astra`（固定标题在原型阶段还要用来 `FindWindowW` 找句柄，见 ADR 0031）。
- 托盘：显示/隐藏窗口、**退出**；关闭窗口 = 隐藏到托盘（后端继续跑），只有托盘"退出"
  才真的收摊。单实例：命名互斥体 + 第二实例唤起第一实例的窗口（不启动第二个后端）。
- 平台**只承诺 Windows**（pywebview 的三平台差异不在本轮范围）。

### 3. UI 与壳的桥：一个极窄的 JS API

- **三个方法**（P2 加了第三个只读方法，见下）：`avatar_rect(payload)`、`avatar_state()`、
  `quit_app()`。**不做通用 IPC通道**——它不是插件平台。
- `avatar_rect` 的载荷在 P2 扩了：`{x, y, width, height, visible, enabled, viewport_width,
  viewport_height}`。两个开关语义不同——`enabled=False`（关掉「显示角色」）**结束 Unity 进程**；
  `visible=False`（切到"状态"卡片 / 窗口太窄）只是**藏起来**。
  `viewport_*` 是 P2 实测加的：**壳不能假设一个 DPI 比例**（见 ADR 0031 的 P2 回填），
  换算比要用"客户区物理尺寸 ÷ 页面自报视口"**实测**。
- Studio 的 HTML/CSS/JS 与全部守门测试**保持不变**（ADR 0025 §2 的承诺：前端代码不重写）；
  只在检测到 `window.pywebview` 时启用桌面专属行为（否则浏览器里照旧——连"状态 / 角色"
  那个切换都不出现）。

### 4. 依赖边界：`astra serve` 不许沾 pywebview

- `import astra.cli` 与 `astra serve` 的路径**不得**导入 pywebview —— 它只在
  `astra desktop` 分支里**延迟导入**。守门：单测断言
  `import astra.cli` 之后 `sys.modules` 里没有 `webview`。
- `pyproject.toml` 里作为普通依赖（个人项目，单机，不做 optional-extra 的机械划分），
  但上面那条测试钉住"服务端不受影响"。

### 5. 打包（PyInstaller / 独立 exe / 安装包）**不在本轮承诺**

先以 `uv run astra desktop` 使用；壳稳定后单开 ADR 决定分发方式（PyInstaller 与
pythonnet/WebView2 的组合有已知坑，值得单独实测一轮）。本轮交付的"桌面应用" =
`astra desktop` 这个入口。

## Reason

- **为什么是 pywebview 而不是 Electron / Tauri / .NET**：壳的核心职责恰好是项目**已有**
  的能力——监督子进程（ADR 0026 的子进程协议）、读 `Settings`、管理进程生命周期。
  用 Python 写壳 = 这些逻辑一行不用重写、不用跨语言同步；Electron 要把监督逻辑在 Node
  里重写，Tauri 还要先装 Rust 工具链（本机没有），.NET 则让仓库多出第二个系统。
  体积/生态的差距（Electron 更成熟、Tauri 更小）在本机单用户场景里不足以抵消三份
  重复的系统。
- **为什么不用 `file://` 加载 studio.html**：页面里的 WebSocket 是相对路径、音频要用
  `AudioContext`/`getUserMedia`，浏览器里这些都在 http/WS 的同源语义下常年验证过；
  `file://` 会引入 origin 与媒体权限的新变量。加载本机 URL 与浏览器里**逐字节同一份**
  页面，也是"前端不重写"的意思。
- **为什么关闭窗口 = 隐藏而不是退出**：桌面陪伴应用的预期是"她一直在"——关窗顺手退出
  会把正在跑的 turn 和记忆抽取一起杀掉；托盘留一个出口，退出是显式动作。
- **为什么不把 pywebview 变成 `serve` 的依赖**：`astra serve` 是调试与脚本的一等入口
  （README/验收步骤全在用），它的依赖面不该因为 GUI 而变大。

## Consequences

- 新增 7 个 Python 依赖（pywebview 及其传递依赖），只在 `astra desktop` 路径生效。
- 后端子进程的输出在 GUI 场景没有控制台：壳把子进程 stdout/stderr 重定向到
  `data/astra-desktop.log`（并尊重 `ASTRA_LOG_FILE`），否则用户看不到启动失败的原因。
- 壳与后端是**两个进程**（加上 Unity 是三个）：任何"退出不干净"都会留下占端口的幽灵
  进程——这是原型阶段必须专门验的一条（任务管理器核实）。
- 托盘图标需要一个 `.ico` 资源进仓库（新增 `assets/`），并在打包 ADR 里再决定它的分发。
- 未完成事项（明确记录，不许假装）：窗口/托盘的 DPI 表现、WebView2 宿主细节、
  从快捷方式启动（无控制台）时的日志与错误面 —— 全部进 P1 的真机验收。