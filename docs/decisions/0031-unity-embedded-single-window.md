# ADR 0031 — Unity 形象并入桌面壳：单窗口内嵌（`-parentHWND`）

> 用户拍板：**单窗口内嵌，先原型验证**（2026-09-28）。壳的选型见 ADR 0030。
> 本文记录"Unity 怎么进那个窗口"的做法、必须实测的四个点，以及原型失败时的回退顺序。

## 本轮实测（定案前在 Unity 工程与本机核过）

| # | 事实 | 影响 |
|---|---|---|
| 1 | Unity 编辑器在 `E:\2022.3.48f1c1\Editor\Unity.exe`（版本 2022.3.48f1c1，Hub 的 secondaryInstallPath = `E:\`） | batchmode 构建命令可以写死这条路径（或用环境变量） |
| 2 | `EditorBuildSettings.asset` 里**只有一个启用场景** `Assets/Scenes/SampleScene.unity` | 构建脚本不用处理多场景 |
| 3 | 工程里**没有 `Assets/Editor` 脚本**，也**没有任何 Build 产物**（无 `Builds/`） | 构建链从零加：一个 `BuildPlayer.cs` + 一条命令 |
| 4 | `ProjectSettings.asset`：`fullscreenMode: 1`（FullScreenWindow）、`resizableWindow: 0`、`runInBackground: 0` | 嵌入的前提是"窗口化 + 可调整 + 后台继续渲染"——三条都要改（构建脚本里设，运行时再兜一层） |
| 5 | 工程里没有任何 WebView / CEF 插件（`manifest.json` 里搜不到） | 走"Unity 当宿主 + WebView"那条路的成本更高（要买插件）——被否 |
| 6 | Unity 工程**不是 git 仓库** | 改工程前逐文件备份 `.bak`（ADR 0024/0027 的既有规矩） |
| 7 | Unity Personal 版 | 打出来的播放器带 "Made with Unity" 启动闪屏，属已知瑕疵，接受 |

## Decision

### 1. 形态：壳当宿主，Unity 作为**子窗口**嵌进预留区域

```text
┌─ Astra（pywebview 窗口，唯一顶层窗口）───────────────────┐
│  ┌──────────┐  ┌─ #avatar-slot（HTML 里的占位元素）─┐   │
│  │ 左栏导航 │  │  ← 这块矩形被 Unity 子窗口占住      │   │
│  │          │  │    （HTML 画不到它上面）           │   │
│  └──────────┘  └───────────────────────────────────┘   │
│  ┌─ 其余页面内容（Studio 原样）──────────────────────┐  │
│  └───────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
        │ 启动：Astra.exe -parentHWND <壳窗口句柄> -screen-fullscreen 0
        └→ Unity 播放器进程（Builds/Windows/Astra.exe）
```

- **用官方 `-parentHWND`**（Unity Standalone 支持的嵌入参数，微软的"Unity in WPF"
  示例走的就是这条路），**不手工 `SetParent`**——后者要自己补窗口样式/消息处理，
  官方参数把这类细节留在 Unity 内部。
- 位置与尺寸由**壳**决定：Studio 里放一个 `#avatar-slot` 占位元素，把它的
  `getBoundingClientRect()` 通过 ADR 0030 §3 的 `avatar_rect` 桥上报给 Python 侧，
  壳用 `SetWindowPos` 摆放 Unity 子窗口；窗口缩放/换页时重报一次。
- **硬约束：HTML 不能盖在 Unity 之上**（两个都是真 HWND）。所以布局按"角色区 + UI 区"
  设计，不做"UI 浮在角色身上"；`#avatar-slot` 要给一个最小尺寸，别让它被挤成一条缝。

### 2. Unity 侧改动清单（改前备份 `.bak`）

| 文件 | 改什么 |
|---|---|
| `Assets/Editor/BuildPlayer.cs`（**新增**） | `Windows64 + Mono` 构建到 `Builds/Windows/Astra.exe`；构建前设 `PlayerSettings.fullScreenMode = FullScreenMode.Windowed`、`resizableWindow = true`、`runInBackground = true`、`forceSingleInstance = false` |
| `Assets/Scripts/AstraBootstrap.cs`（既有，小改） | 启动时兜底：`Application.runInBackground = true;` `Screen.fullScreenMode = FullScreenMode.Windowed; Screen.fullScreen = false;`（不依赖 Player Settings 资产是否被改过） |
| `ProjectSettings/ProjectSettings.asset` | 由构建脚本写入（不手工改），保证"从仓库重新构建"可复现 |

构建命令（写进计划与 README）：

```powershell
& "E:\2022.3.48f1c1\Editor\Unity.exe" -batchmode -quit `
  -projectPath "E:\UnityProject\VirtualTuber" `
  -executeMethod BuildPlayer.Windows -logFile -
```

### 3. 进程生命周期归壳

- 壳负责启动/停止 Unity；Unity 崩了就如实提示（不静默重启刷屏）。
- UI 上给一个「显示角色」开关（默认开）：关掉 = 结束 Unity 进程。
  这既是交互偏好，也是**显存开关**——8GB 显存里 LLM / CosyVoice / Unity 三者要抢
  （ADR 0022 的既有约束），"今天只想打字"时不该被 Unity 占着。
- 关闭顺序：Unity → 后端 → 壳（ADR 0030 §1）。

### 4. 决策门：原型先行，四个点全过才进正式实施

最小原型 = **空壳窗口 + 一个 :300×300 的占位 div + 打好包的 Unity 播放器**，
只验这四件事：

1. `-parentHWND` 能嵌进去，并且**随壳窗口缩放**（`SetWindowPos` 生效、画面不拉伸变形）；
2. 鼠标：点击/悬停落在 Unity 上的**坐标是准的**（没有偏移）；
3. 键盘焦点：HTML 输入框 ↔ Unity 之间来回切换正常（在输入框里打字不会漏进 Unity）；
4. DPI：125% / 150% 缩放下不错位、不糊。

**任一项失败 → 按顺序回退**：① 桌宠式（Unity 无边框 + 透明 + 置顶，Studio 仍是面板窗口）
→ ② 维持两个窗口。**回退不丢工作**：ADR 0030 的壳本身与 Unity 的构建链都要，
差的只是"嵌不嵌"。原型的结论（含失败项的截图/现象）**回填进本文**再实施。

## P0 原型结论（2026-09-28 实测回填）

原型：`.trae/prototypes/desktop_embed.py`（自检输出存档 `.trae/prototypes/p0-selftest.json`，
**退出码 0**）；播放器由新增的 `Assets/Editor/BuildPlayer.cs` 构建（工程改动只此一个文件）。

| 决策门 | 结论 | 证据 |
|---|---|---|
| ① 嵌入 + 跟随缩放 | **通过** | 播放器窗口直接出现在壳的**子窗口**里（`GetParent == 壳`）；把 `#slot` 的 CSS 矩形按 `GetDpiForWindow/96` 换算后用 `SetWindowPos` 摆放，误差 **0 px**；壳窗口放大后（1377×824 → 1587×944）误差仍是 **0 px** |
| ② 鼠标坐标 | **通过（路由层面）** | 跨进程注入的真实点击落在 Unity 区域 → 焦点归 Unity；落回页面 → 焦点归 WebView。场景里暂无可点交互，逐像素精度没有可验对象 |
| ③ 键盘焦点切换 | **通过** | 同上，双向都对 |
| ④ DPI | **通过** | 本机就是 **150%**（`dpi_scale=1.5`），矩形换算与跟随在这一档全部 0 误差 |

**实现要点（比原方案更简单）**

- **不需要手工 `SetParent`**：Unity 自己就把窗口建成壳的子窗口——实测样式
  `0x50000000`（`WS_CHILD | WS_VISIBLE`）。壳只需 `SetWindowPos`：**客户区坐标** +
  `HWND_TOP`（WebView2 也是子窗口，不置顶会被它盖住）。
- **别用 `FindWindowW("UnityWndClass")` 找播放器**：它只搜顶层窗口；实测第一轮误匹配到
  Unity 编辑器**残留的顶层窗口**，把结论带偏（当时还以为 `-parentHWND` 不生效）。
  要在**壳的子窗口**里按类名找（`EnumChildWindows` + `GetParent == 壳`）。
- **清理遗留播放器**：`Popen.terminate()` 不保证杀死，实测踩到过幽灵 Unity 窗口留在桌面上
  （还会污染后续的窗口查找）。壳要在**启动前与退出时**都按镜像名清理。

**仍然只能人眼确认的**：场景/角色是否真的在画。自检采样到的是实心 `#231F20`
（疑似 "Made with Unity" 启动闪屏）——**能确定 Unity 覆盖并绘制了那块区域**
（颜色与页面背景 `#14171E` 不同），但"画的是什么"要人看。

**结论**：四个决策门通过 → 回退方案（桌宠式 / 双窗口）**不启用**，按本 ADR 继续
（先做 P1 壳，再做 P2 内嵌）。

## P2 回填（2026-09-28，壳内实测）

内嵌进真实壳之后又抓到三条，全部写进代码注释与这里：

1. **壳的启动器镜像名也叫 `astra.exe`**（venv 的 console script），而 Unity 播放器是
   `Astra.exe`。按**镜像名**"清遗留播放器"会把壳自己（以及别的 `astra` 命令）一起干掉
   ——实测把后台的壳干掉了两次。改成按**可执行文件完整路径**枚举 + 按 PID 强杀
   （`win32.pids_of_exe` / `kill_process`），只动"同一个播放器文件"的进程。
2. **不能假设 DPI 比例**：壳进程是 DPI-aware（客户区读作物理像素 1748×1174），
   页面报的是 CSS 像素（视口 1166×783 = 物理/1.5），而**验证用的外部探针**又是
   DPI-unaware（读到的是虚拟化的 1165×782）。三方坐标系不同，硬套 `×1.5` 会把角色区
   摆到窗口外面。最终做法：**换算比实测**——`客户区物理 ÷ 页面自报视口`，
   页面把 `viewport_width/height` 一起报上来（`placement_scales`，有单测）。
3. **`webview.start()` 的回调早于窗口 `Show()`**：那一刻枚举窗口只看到未显示的窗体，
   早先"只看可见窗口"的写法直接找不到句柄 → 角色区永远起不来。改成带重试（8 秒），
   实在没可见的就用那个未显示的窗体句柄当宿主（`-parentHWND` 一样能用）。

**P2 的机械验收（我方冒烟，非人眼）**：真实壳 + 真实播放器 →
Unity 子窗口出现（约 1 秒）、可见、位置与页面报的 `#avatar-slot` **逐像素一致**
（右侧留白 18 CSS px 正是右栏内边距）；硬杀壳之后没有孤儿进程（Unity 随宿主窗口销毁退出）。
托盘/开关/缩放这几件仍需人眼（§六 P2 清单）。

## Reason

- **为什么让壳当宿主、而不是 Unity 当宿主**：ADR 0025 §2 已经定了"不做 Unity 一体化
  外壳"，Studio 的 HTML 与它全部守门测试要原样保留；Unity 当宿主就得买 WebView 插件
  （3D WebView 付费 / 免费方案弱），麦克风采集与音频播放要在插件里重验一遍，
  等于把已经跑通的语音链路推到最不确定的地方。
- **为什么必须"构建 + 嵌入"而不是直接跑 Editor**：Editor 是多窗口程序，没有可嵌入的
  单一窗口；`-parentHWND` 只对**打出来的播放器**有效。
- **为什么原型先行**：输入焦点、DPI、缩放这类东西**只能实测**——ADR 0027 的经验就是
  先花小钱实测（动画配置的真实结构），再写实现；把"能不能嵌"写成假设直接开工，
  失败时才发现要重做布局，代价更大。

## Consequences

- 需要一条 Unity 构建链（首次构建几分钟；产物 `Builds/Windows/` 约几百 MB，不进仓库）。
- 布局受"HTML 不能覆盖 Unity"约束；`#avatar-slot` 的位置/最小尺寸是 Studio 的**设计输入**，
  不是可以随便改的装饰。
- 进程数从 2（壳 + 后端）涨到 3（+ Unity）：关闭顺序、幽灵进程、崩溃恢复都要在
  真机验收里过（ADR 0030 的"退出不干净"清单 + 本文 §3）。
- Unity 版本被钉在 `2022.3.48f1c1`（换版本要重验嵌入行为）；工程改动一律先备份。