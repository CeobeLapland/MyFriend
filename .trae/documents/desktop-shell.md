# 桌面应用 — 实施计划（壳 + Unity 单窗口内嵌）

## Context

Studio 阶段 1–4 已交付（1270 tests 全绿）。本轮开始**桌面应用**：一个窗口、托盘、
进程管理，并把 Unity 形象并入同一个窗口。

配套 ADR：
[`0030-desktop-shell-pywebview.md`](../../docs/decisions/0030-desktop-shell-pywebview.md)（壳 = pywebview，接管监督者）、
[`0031-unity-embedded-single-window.md`](../../docs/decisions/0031-unity-embedded-single-window.md)（Unity 用 `-parentHWND` 内嵌，原型先行）。
**本文件是执行依据。P0 原型的结论要回填 ADR 0031，再进 P1。**

---

## 一、本轮实测（写计划前在机器与两个工程里核过）

| # | 事实 | 影响 |
|---|---|---|
| 1 | WebView2 运行时 `136.0.3240.64` 已装 | pywebview 的 EdgeChromium 后端零额外安装 |
| 2 | `uv pip install --dry-run pywebview` → 共 7 个包：pywebview 6.2.1 / pythonnet 3.1.0 / clr-loader 0.3.1 / bottle 0.13.4 / proxy-tools 0.1.0 / cffi 2.1.1 / pycparser 3.0 | 新增依赖面就这么大，且只在 `astra desktop` 路径导入（§2.1 有守门测试） |
| 3 | 本机无 Rust；有 Node 24 / npm 11 / .NET 9 SDK | Tauri/Electron/.NET 的取舍写进了 ADR 0030 §Reason |
| 4 | `supervisor.py`：`child_command()` / `is_supervised()` / `run_supervised()` / `EXIT_RESTART=75`；`cli.py --no-supervisor` 的帮助文本已写明"供……桌面壳使用" | 壳的监督逻辑**零新增**；只需给 `run_supervised` 加可选的 `stop` 事件 |
| 5 | `_serve_foreground` 把 `uvicorn_server` / `supervised` / `restart_requested` 放在 `app.state` 上 | 桌面壳拉起的子进程同样走这条路，Studio 的「保存并重启」在桌面壳下**原样可用** |
| 6 | Studio 是 `GET /studio` 静态页；页面里的 WS 是相对路径、静态路由同源 | 壳加载 `http://127.0.0.1:{port}/studio`，不用 `file://` |
| 7 | Studio 守门测试：`node --check`、禁用 `innerHTML`、依赖端点清单、7 个 `data-page` | 新增桌面专属 JS 必须守同样的规矩；新增"角色区"若引入新 DOM id 要同步更新守门测试 |
| 8 | Unity：`E:\2022.3.48f1c1\Editor\Unity.exe`；`EditorBuildSettings` 只有一个启用场景 `Assets/Scenes/SampleScene.unity`；无 `Assets/Editor` 脚本、无 `Builds/` 产物 | 构建链从零加（`BuildPlayer.cs` + 一条命令） |
| 9 | Unity `ProjectSettings.asset`：`fullscreenMode: 1` / `resizableWindow: 0` / `runInBackground: 0` | 嵌入前必须窗口化 + 可调整 + 后台渲染（构建脚本设 + 运行时兜底） |
| 10 | Unity 工程不是 git 仓库；无任何 WebView 插件 | 改前逐文件 `.bak`；"Unity 当宿主"那条路被否（要买插件） |
| 11 | pywebview 的 GUI 必须跑在主线程；`run_supervised` 是**阻塞循环** | 壳 = 主线程窗口 + 工作线程 supervisor |
| 12 | 测试基线 **1270**；四项质量门：pytest / `ruff check src tests` / `ruff format --check src tests` / `mypy src` | 每步跑；GUI 与 Unity 的部分靠真机验收 |

---

## 二、技术设计

### 2.1 桌面壳（`astra desktop`）

**进程模型**

```text
Astra（壳进程，python -m astra.cli desktop）
├─ 主线程：pywebview 窗口（WebView2）→ http://127.0.0.1:8000/studio
├─ 工作线程：run_supervised(child_command(), {**env, ASTRA_SUPERVISED: "1"})
│    └─ 后端子进程：python -m astra.cli serve --no-supervisor
└─ （P2 起）Unity 子进程：Builds/Windows/Astra.exe -parentHWND <壳窗口> -screen-fullscreen 0
```

- **启动顺序**：读 `Settings` → 探 `GET /health`：**已在跑就不拉第二个**（提示"复用了
  已启动的后端"，并如实说明那个后端的重启能力取决于它的监督状态）→ 没有才拉子进程 →
  等 `/health`（最多 ~15s）→ 开窗。
- **退出顺序**：Unity → 后端（`stop` 事件 + 宽限）→ 壳。关窗 = 隐藏到托盘；
  托盘「退出」才真收摊。
- **托盘**：显示/隐藏、退出；**单实例**：命名互斥体 `Global\astra-desktop`，
  第二实例把已存在窗口唤到前台后自行退出。
- **日志**：子进程 stdout/stderr 重定向到 `data/astra-desktop.log`
  （`ASTRA_LOG_FILE` 非空时沿用它的设置），因为 GUI 启动没有控制台可看。

**JS 桥（极窄，ADR 0030 §3）**

```python
class DesktopApi:
    def avatar_rect(self, rect: dict) -> bool: ...   # P2 用
    def quit_app(self) -> None: ...
```

Studio 侧只在 `window.pywebview` 存在时才注册这些行为。**不加通用 IPC。**

**依赖边界守门**：`import astra.cli` 之后 `sys.modules` 里不得有 `webview`
（`astra desktop` 分支里延迟导入）。

### 2.2 supervisor 的小扩展（唯一动到既有的改动）

`run_supervised(command, env, *, stop: threading.Event | None = None)`：
`stop is None` 时**行为逐字节不变**（现有测试守着）；给定时等待循环改成
`process.wait(timeout=0.5)` 轮询 + 检查事件，退出时先发优雅信号（Windows 上
`CTRL_BREAK_EVENT`，子进程需以 `CREATE_NEW_PROCESS_GROUP` 启动）→ 宽限 10s →
`terminate` → `kill`。**原型阶段先核实** `CTRL_BREAK_EVENT` 在 uvicorn/Windows 下的
真实行为，核实结果写进实现注释。

### 2.3 Unity 构建与内嵌（P2）

- **构建**：新增 `Assets/Editor/BuildPlayer.Windows()`（`Windows64 + Mono` →
  `Builds/Windows/Astra.exe`），构建前写 Player Settings（Windowed / resizable /
  runInBackground / 非单实例）；运行时再兜一层（`Application.runInBackground`、
  `Screen.fullScreenMode`）。命令见 ADR 0031 §2。
- **slot 契约**：Studio 里新增 `#avatar-slot`；页面在挂载、窗口 resize、切页时上报
  `getBoundingClientRect()`（含 `visible`）；壳侧 clamp 到窗口内并 `SetWindowPos`。
  纯函数（rect → 窗口坐标）要单测。
- **开关**：「显示角色」= 起/停 Unity 进程（显存开关，ADR 0031 §3）。
- **决策门**：ADR 0031 §4 的四个点（嵌入+缩放、鼠标坐标、键盘焦点、DPI）。
  原型只做"空壳 + 300×300 占位 div + 播放器"。

---

## 三、改动清单

| 文件 | 动作 |
|---|---|
| `pyproject.toml` | 加 `pywebview` 依赖 |
| `src/astra/cli.py` | `desktop` 子命令（延迟导入壳模块） |
| `src/astra/desktop/__init__.py` | **新增**（壳包：窗口、托盘、单实例、DesktopApi） |
| `src/astra/desktop/app.py` | **新增**（启动/退出顺序、health 复用探测、日志重定向） |
| `src/astra/supervisor.py` | `run_supervised` 加可选 `stop` 事件 |
| `assets/astra.ico` | **新增**（托盘图标） |
| `src/astra/api/static/studio.html` | `#avatar-slot` + 桌面桥（仅在 `window.pywebview` 下启用）+ 「显示角色」开关 |
| `E:\UnityProject\VirtualTuber\Assets\Editor\BuildPlayer.cs` | **新增**（先备份 `.bak`） |
| `E:\UnityProject\VirtualTuber\Assets\Scripts\*.cs` | 运行时兜底三行（先备份 `.bak`） |
| `docs/decisions/0031-*.md` | P0 结论回填（实测表） |
| `README.md` | 桌面应用一节 + 验收步骤 + 构建命令 |
| `tests/unit/test_cli.py` | `desktop` 参数解析 + 不导入 pywebview 的守门 |
| `tests/unit/test_supervisor.py` | `stop` 事件用例（含 `stop=None` 行为不变） |
| `tests/unit/test_desktop_geometry.py` | rect → 窗口坐标（纯函数） |
| `tests/integration/test_studio_console.py` | 若新增 `data-page`/DOM id，同步守门清单 |

---

## 四、分步实施（每步四项质量门；GUI/Unity 部分真机验收）

- **P0 原型（先做，结论回填 ADR 0031）**：① Unity 加构建脚本、打出 `Astra.exe`（首次
  构建耗时属正常）；② 写一个 ~80 行的临时 pywebview 空壳 + 300×300 占位 div；
  ③ 用 `-parentHWND` 挂上去，按 ADR 0031 §4 的四点逐条验；④ 结论（通过/回退）写回 ADR。

  > **P0 已完成（2026-09-28）**：四个决策门全部通过（自检退出码 0，证据见
  > `docs/decisions/0031-*.md` 的「P0 原型结论」与 `.trae/prototypes/p0-selftest.json`）。
  > 三条实现要点也随之确定：**不用手工 `SetParent`**（Unity 自己设 `WS_CHILD|WS_VISIBLE`）、
  > **在壳的子窗口里找播放器**（别用 `FindWindowW`）、**启动前/退出时清遗留播放器**。
- **P1 壳**：`astra desktop`（监督复用、health 复用探测、托盘、单实例、退出顺序、
  日志重定向）+ supervisor 的 `stop` + 单测。

  > **P1 已完成（2026-09-28，代码与自动化门）**：`astra desktop` 落地；`supervisor.py`
  > 加了可选的 `stop` 事件与 `popen_kwargs`（`stop=None` 行为不变，有测试）。
  > 我方冒烟实测：窗口 ~3 秒出现、后端 1.5 秒就绪、`POST /v1/admin/restart` → 202 且
  > 4.3 秒恢复（**"壳接管监督者"成立**）、第二个实例秒退并唤前（单实例成立）。
  > 两个实测修正：① `/health` 一次要 ~2.4 秒（LM Studio 在跑时），探针超时从 2 秒提到
  > 8 秒——否则"以为后端没起来"、窗口白等 30 秒；② 托盘图标改成 ≤48px 的 BMP 帧
  > （256px 的 PNG 帧会让 Pillow 在加载时喷 10 行 libpng 警告）。
  > 托盘 / 关窗到托盘 / 优雅退出的干净程度留给真机验收（§六 P1 清单）。
- **P2 内嵌**：Unity 构建脚本进"可复现"状态；`#avatar-slot` 与 `avatar_rect` 桥、
  「显示角色」开关、布局定稿（角色区位置与最小尺寸）。

  > **P2 已完成（2026-09-28，代码与机械验收）**：新增 `settings.avatar_player_path`
  > （`.env.example` + 设置页白名单）、`desktop/win32.py`、`desktop/avatar.py`
  > （几何有单测），Studio 右栏改「状态 / 角色」两态（浏览器里不出现切换）。
  > 实测抓到三条并已修：① **按镜像名清播放器会把壳自己杀掉**（壳启动器也叫 astra.exe）
  > → 改按可执行文件路径；② **不能假设 DPI 比例**（壳/页面/外部探针三方坐标系不同）
  > → 改成"客户区 ÷ 页面自报视口"实测；③ `webview.start()` 回调早于窗口显示 → 找句柄带重试。
  > 机械验收：Unity 子窗口与页面 `#avatar-slot` 逐像素一致、硬杀无孤儿进程。
  > 托盘 / 开关 / 缩放跟随留真人验收（§六 P2 清单）。
  >
  > **布局回填（用户实机反馈后定稿）**：角色区最初放进 320px 右栏 → 16:9 被挤成竖条、
  > 看不到人物。改成四象限：左栏 = 导航 + 状态卡（可滚动）；对话页 = 上「聊天（窄）+
  > 角色区（16:9）」、下「横向滚动历史带（23% 高）」。壳侧零改动（摆放是位置驱动的）。
  > 实测最大化窗口下角色区 1059×601（1.76 ≈ 16:9）。
- **P3 收尾**：README（桌面一节 + 构建命令 + 验收）、`docs/decisions/0031` 回填、
  全量质量门、真机验收报告。

  > **P3 已完成（2026-09-29）**：README 加了「桌面应用已具备」一节（含 Unity 构建命令与
  > shader 注意事项）与「验收（桌面应用 · 阶段 5）」；ADR 0031 的 P2 回填补齐走到
  > 6 条（布局、shader 剥离、「保存并重启」超时）。两个实机 bug 均修复并实测：
  > VRM `loaded in 2.3s`、带 WS 连接的重启 6.1 秒恢复。

---

## 五、风险与已知取舍

- **内嵌的四点风险**（焦点/坐标/DPI/缩放）→ 原型专治；失败有回退顺序（ADR 0031 §4），
  回退不丢工作。
- **三个进程的退出/幽灵进程**：关窗二次确认是"隐藏"、退出走托盘；退出顺序写死并在
  验收里要求用任务管理器核实"没有残留进程"。
- **显存**：Unity + LLM + CosyVoice 三者抢 8GB（ADR 0022）——「显示角色」开关是显式答案。
- **从快捷方式启动没有控制台**：日志重定向到 `data/astra-desktop.log`，错误要能事后看。
- **打包不做**（ADR 0030 §5）：`uv run astra desktop` 是能用的交付形态；独立 exe 单开 ADR。
- **不重写前端**：Studio 的 HTML/守门测试保持；新增的桌面行为全部"浏览器里不存在"。

---

## 六、真机验收（用户）

**P1（壳）**

```text
1. `uv run astra desktop` → 独立窗口打开 Studio；先把浏览器里的 Studio 关掉
2. 托盘：关窗 → 窗口消失但后端还在（浏览器开 /studio 仍能用）→ 托盘点回来 → 窗口复原
3. 再跑一次 `uv run astra desktop` → 不出现第二个窗口/第二个后端（焦点回到已有窗口）
4. 设置页「保存并重启」→ 后端重启、页面自动重连（桌面壳下原样可用）
5. 托盘「退出」→ 任务管理器里 python 进程清干净（无幽灵后端占端口）
6. 先手动 `uv run astra serve` 再 `astra desktop` → 壳复用已有后端并如实提示
```

**P2（Unity 内嵌，按 ADR 0031 的决策门）**

```text
1. 构建：Unity batchmode 命令跑通 → Builds/Windows/Astra.exe 出现
2. 壳里「显示角色」→ 角色出现在预留区；拖动窗口边缘缩放 → 角色跟着变、不拉伸变形
3. 鼠标在角色区域点击/悬停 → 坐标准确（不偏移）
4. 键盘：在 Studio 输入框打字不漏进 Unity；点角色区域再回来打字正常
5. 系统缩放切 125% / 150% → 不错位、不糊
6. 「显示角色」关掉 → 任务管理器里 Unity 进程消失（显存释放）
7. 托盘退出 → Unity、后端、壳三个进程全部清干净
```

> 任一条不过 → 记录现象，按 ADR 0031 §4 的回退顺序决策（桌宠式 → 双窗口）。