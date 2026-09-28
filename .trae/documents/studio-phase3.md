# Studio 应用 — 实施计划（阶段 3：语音与动画托管）

## Context

**阶段 1 / 2 已完成**（`/studio` 产品界面 + 配置读写 / 一键重启 / LM Studio 管理 / 事件页；
1157 tests、ruff、mypy 全绿；阶段 2 的 8 步真机验收已由用户走完，
并修掉了"grid 子项 `min-height:auto` 导致整页不可滚"的布局缺陷）。

本阶段对应设计文档 `studio-app.md` §8 的阶段 3 与 P1 #7 / #10：

| 目标（用户视角） | 对应 |
|---|---|
| 在 Studio 里**按住说话**，听到她的声音，中途插话能打断 | P1 #7（ADR 0025 §4） |
| 动画映射能在 Studio 里改，Unity 从后端拉 | P1 #10（ADR 0025 §3） |

配套 ADR：[`docs/decisions/0027-studio-phase3-voice-and-animation.md`](../../docs/decisions/0027-studio-phase3-voice-and-animation.md)。
**本文件是阶段 3 的执行依据。**

---

## 一、本轮实测（写计划前在 Unity 工程与机器上核对过的事实）

| # | 事实 | 影响 |
|---|---|---|
| 1 | 动画配置的真实结构（`AstraAnimationConfig.cs` L11–62）：`{idlePool: ClipRef[], actions: ActionBinding[], poses: PoseBinding[]}`；`ClipRef{clip,weight,loop}`、`PoseBinding{pose,enter,loop[],exit}` | 后端校验就按这三块写，别发明新结构 |
| 2 | 当前 `Assets/StreamingAssets/astra_animation.json`（2514 B）：待机池 11 条、动作 7 条（其中 2 条空的 = 未配）、**没有 `poses` 键** | 校验必须容忍缺省/空的 `poses`（C# 的 `Normalize` 也容忍） |
| 3 | **Unity 工程不是 git 仓库**（`E:\UnityProject\VirtualTuber\.git` 不存在）；StreamingAssets 下也没有 `.bak` | 改 Unity 之前**先备份**（ADR 0024 §9 / ADR 0027 §2） |
| 4 | `AstraConfig.BaseUrl = "http://127.0.0.1:8000"` 是 `const`（改地址要重编译，刻意没有配置文件） | 新端点复用它，不引入配置项 |
| 5 | `AstraClient` 已有 `Connected` 事件（**每次重连都触发**），`AstraBootstrap` 已订阅（`_client.Connected += OnConnected`） | "连上就拉配置"的天然钩子，不需要新增组件 |
| 6 | `AstraAnimationConfig` 是静态类，`Load()/Save()` 直接读写 StreamingAssets（打包后只读，`Save` 已捕获异常） | 托管后的"写本地缓存"沿用这条路径，开发期可写 |
| 7 | 后端动作词表 `domain/vocabulary.py:ACTIONS`（11 个）与 Unity 的 `ActionNames`（11 个）**逐字一致** | 后端校验以 vocabulary 为准即可，不需要两边各写一份 |
| 8 | CosyVoice（`127.0.0.1:7860`）**当前没有运行** | 语音的功能性验收要用户先起它（M4 的启动流程可直接复用） |
| 9 | 协议侧既有事实：客户端在 `input.audio.started` 里声明 16 kHz/mono/pcm16，**服务端不重采样**（不符即 `error.protocol`）；播放按 `tts.started.sample_rate` 建 AudioContext；`turn.interrupted` 必须立即停播（ADR 0021/0022） | 语音实现照 `/dev` 的链路，**协议一行不改** |
| 10 | `src/astra/api/static/dev.html` L208–332 / L661–689 是可工作的音频参考实现（采集 / 播放 / 事件处理） | 移植它的**逻辑**，不照抄样式 |

---

## 二、技术设计

### 2.1 语音（3a）：Studio 内的 push-to-talk

**交互**（对话页输入框下方一行）：

```text
[✓ 语音模式（回复用语音）]  [🎤 按住说话]  状态：就绪 / 正在听… / 识别中… / 播放中 / 已被打断
```

- **语音模式**：勾选即发 `session.voice_mode.set`（连上/重连后补发一次，`/dev` 的教训）；
- **按住说话**：`pointerdown` → 取麦克风（16 kHz AudioContext）→ 发 `input.audio.started` →
  逐块发 PCM16 二进制帧；`pointerup`/`pointerleave` → `input.audio.stopped` → 停采集；
- **播放**：`tts.started` 建/换 AudioContext（按 `sample_rate`）→ 二进制帧逐块排队播放 →
  `tts.completed` 收尾；**`turn.interrupted` 立即停播并全清队列**；
- **打断**：说话时若上一轮还在播，直接开始采集即触发服务端 barge-in（协议已支持）；
- **诚实禁用**：`/health` 里 `stt`/`tts` 为 `not_configured` 时按钮禁用，提示
  "去设置页把 ASTRA_STT_PROVIDER / ASTRA_TTS_PROVIDER 打开（并让 CosyVoice 跑起来），改完点保存并重启"。
  健康状态来自已有轮询，不新增请求。
- 音频相关事件在**事件页**可见（`recordEvent` 已覆盖），排查不用开 `/dev`。

### 2.2 动画托管（3b）

**端点**（`/v1/avatar/animation`，进 OpenAPI、无条件挂载，ADR 0027 §2）：

```text
GET /v1/avatar/animation     # 200 配置 JSON；还没有副本 → 404（不返回空配置）
PUT /v1/avatar/animation     # 校验 → 落盘 data/astra_animation.json；只校验不重写
```

**校验**（失败 400 + 稳定错误码 `invalid_animation`，消息里指出第几条）：

- 顶层三个键的类型（缺 `poses` 视为空）；
- `idlePool[].clip` / `actions[].clips[].clip` / `poses[].loop[].clip` 非空；
- `weight > 0`；`loop` 是布尔（缺省按 `false`）；
- `actions[].action` ∈ `vocabulary.ACTIONS`（唯一真相在后端）；
- `poses[].pose` ∈ `{sit, lie}`（与 Unity 的 `PoseNames` 对齐）；
- 未知字段**保留**（落盘的是原文本，见 ADR 0027 的 Reason）。

**Studio 动画页**（第 6 个页签）：

- 三块卡片：**待机池**（clip / 权重 / 循环）、**11 个动作**（每个一张卡，列它的候选 clip）、
  **姿态三件套**（sit / lie 的 enter / loop / exit）；
- 每个 clip 一行：clip 名（文本输入）、权重（数字）、循环（开关）、删除；底部"添加 clip"；
- 顶部：`.env` 式的状态带（后端副本路径、是否已存在、最后修改时间）+ 按钮
  「保存到后端」「重新载入」「导出 JSON」「粘贴导入」；
- 保存时 `JSON.parse(当前) → 改这三块 → JSON.stringify`（不认识的字段原样保留）；
- 后端还没有副本时：如实说明"跑一次 Unity 就会把当前映射上报上来"，并给粘贴导入入口。

**Unity 改动清单**（改前逐文件备份成 `.bak`）：

| 文件 | 改什么 |
|---|---|
| `AstraAnimationConfig.cs` | 加 `LoadFromJson(string json, string source)`（复用 `Normalize` 与日志）+ `ToJson()`；`Load()` 保持原样 |
| `AstraAnimationSync.cs`（新增） | 一个小组件：`Connected` 时 `GET /v1/avatar/animation` → 200 就应用 + 覆盖本地缓存；**404 就把本地文件 PUT 上去**（首次播种）；连不上就什么都不做（继续用本地缓存，日志写清） |
| `AstraBootstrap.cs` | 挂上那个组件（一行） |
| `AstraAnimationSetup.cs`（面板） | `Save()` 之后顺手上报一次（尽力而为；失败日志写"没上报，下次连上会被后端的版本覆盖"） |
| `Assets/StreamingAssets/astra_animation.json` | 只读不改（备份即可） |

**不动**：`AstraAnimator` / `AstraActions` / `AstraPoseController` / `AstraIdle` 等表现逻辑。

---

## 三、目标文件树

```text
src/astra/api/routes/avatar.py                 # 新：GET/PUT /v1/avatar/animation（校验 + 落盘）
src/astra/api/app.py                           # 改：挂 avatar 路由（无条件）
src/astra/domain/animation.py                  # 新：动画配置的校验（纯函数，可单测）
src/astra/api/static/studio.html               # 改：语音（采集/播放/打断/状态）+ 动画页
tests/unit/test_animation_validation.py        # 新
tests/integration/test_avatar_animation_api.py # 新（tmp 数据目录：404 → PUT → GET → 校验失败）
tests/integration/test_studio_console.py       # 改：+动画页守门断言与新端点存在性
E:\UnityProject\VirtualTuber\Assets\Astra\Scripts\*.cs   # 改（先备份 .bak）
README.md                                      # 改：语音用法 / 动画托管 / 阶段 3 验收
docs/08_IMPLEMENTATION_PLAN.md                 # 改：§12 实际落点补阶段 3
.trae/documents/studio-phase3.md               # 本文件
```

**无新依赖、无新迁移。**

---

## 四、实施顺序（每步停下来都能看见进展）

| # | 步骤 | 验证 |
|---|---|---|
| 1 | **3a-1 语音采集/播放**：Studio 对话页的开关、按住说话、播放与打断、状态文案 | 真机：起 CosyVoice → 设置页打开 STT/TTS → 保存并重启 → 按住说话能听到声音 |
| 2 | **3a-2 诚实禁用**：`/health` 未配置时禁用 + 指向设置页 | 关掉 CosyVoice / provider=none 时按钮禁用且文案正确 |
| 3 | **3b-1 后端端点**：`domain/animation.py` 校验 + `routes/avatar.py` + 单测/集成测试 | `uv run pytest tests/unit/test_animation_validation.py tests/integration/test_avatar_animation_api.py -q` |
| 4 | **3b-2 Studio 动画页** | 页面能读/改/存（先用粘贴导入造一份副本） |
| 5 | **3b-3 Unity 改造**：备份 → `LoadFromJson/ToJson` → 同步组件 → Bootstrap 一行 → 面板顺手上报 | 用户在 Unity 里按 Play：Console 里能看到"已从后端取到动画配置" |
| 6 | 测试补全 + 四项质量门 + README / docs/08 | 四项全绿 |
| 7 | 真机验收（§六，用户亲手；语音与 Unity 各一轮） | 见 §六 |

**第 1 / 4 步之后就能让用户打开 `/studio` 看**——不要憋到最后。

---

## 五、依赖变更

**无。** 语音用的是浏览器自带的 Web Audio API；后端只用标准库（`json` / `pathlib`）。

---

## 六、验收标准

### 自动化

```powershell
uv run pytest -q                          # 1157 + 新增
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

### 真机端到端（用户亲手）

**A. 语音**（前置：CosyVoice 雨落版在跑；LLM 按 8GB 显存约束 `--gpu off`，见 ADR 0022）

```text
1. 设置页：STT/TTS 打开（cosyvoice）→ 保存并重启 → 右栏 stt/tts 从 not_configured 变 ok
2. 勾「语音模式」→ 按住说话说一句 → 状态走"正在听… → 识别中… → 播放中"，
   听到她的声音（文本气泡同时出现转写内容）
3. 她说到一半时再按住说话 → 声音**立刻停**、事件页出现 turn.interrupted，
   新一轮正常开始
4. 不勾语音模式时按住说话 → 仍能识别，但**不合成语音**（纯文本路径不回归）
5. 把 providers 设回 none 并重启 → 按钮禁用，提示指向设置页（不假装能用）
```

**B. 动画托管**（前置：Unity 工程已备份）

```text
1. 冷启动（后端 `data/astra_animation.json` 不存在）：Unity 按 Play →
   Console 出现"把本地动画映射上报给后端（首次播种）"→ Studio 动画页立刻能看到那 11 条待机池
2. 在 Studio 动画页把 `wave` 的两个 clip 改成别的 → 保存 →
   Unity 里退出 Play 再进入 → Console 显示"已从后端取到动画配置"，说一句触发 wave 的动作能看出差别
3. 停掉后端（终端 Ctrl+C）→ Unity 再按 Play → 动画照常（用本地缓存兜底），
   Console 如实写"后端不可用，继续使用本地缓存"
4. 在 Unity 面板里改一条并保存 → Console 写"已上报给后端"→ Studio 动画页刷新能看到这条改动
5. 在 Studio 里填一个非法值（空 clip 名 / 未知动作名）→ 保存被拒，页面说明原因
```

---

## 七、风险点

1. **Unity 侧我无法自动验证**（不能编译 C#、不能 Play）——改动要**小到能逐行解释**，
   并且只碰"配置来源"；备份先做；真机验证交给用户。
2. **双写**（Unity 面板 + Studio）：靠"面板保存顺手上报"消除静默丢改动；
   PUT 失败必须写日志（不能让用户以为存上了）。
3. **首次播种的顺序**：如果 Studio 先写入一份空配置，Unity 的播种就永远不会发生
   （后端有副本了）→ 端点**不返回空配置**、`GET` 缺失时 404，前端也不提供"新建空配置"按钮。
4. **音频格式契约**：客户端声明的 16 kHz 必须与 `ASTRA_AUDIO_INPUT_SAMPLE_RATE` 一致，
   否则 `error.protocol`；改设置页那里的值时两边要一起想清楚。
5. **CosyVoice 没跑时的观感**：按钮禁用 + 文案指向设置页，而不是"按了没反应"。
6. **`/dev` 与 Studio 同时开着**：两边都会收到并播放音频（既有广播语义）。
   本阶段不改，但 README 要写明。
7. **不要碰的红线**：`/health` 与 `/v1/config` 的 key 集合不动；`/dev` 不删不改；
   协议帧一行不改；不新增依赖。

---

## 八、待用户确认的点

1. **先语音后动画**（3a → 3b）——**推荐**：语音是"她真的在跟我说话"的核心体验，
   且能完全自测；动画那半要你配合开 Unity。若你更想先要动画，说一声我把顺序倒过来。
2. **Unity 面板保留**（保存时顺手上报后端）而不是删掉它——**推荐保留**：
   它在 Unity 里改单条映射比开浏览器快，删掉会降低你当前的效率。
3. **clip 名在 Studio 里手填**（不引入"Unity 上报 clip 清单"）——**推荐先手填**：
   上报清单是另一个端点 + 一段 Unity 代码，等真觉得手填烦了再做（记为 P1）。
4. **姿态只放 `sit` / `lie`**（与 Unity 对齐；将来加姿态两侧一起改）——如果近期就想加
   第三种姿态（比如 `sleep`），先说，我把它做成两侧都认的列表。

---

## 九、后续（阶段 4 预告）

桌面壳（一个窗口 + 系统托盘 + 子进程管理，ADR 0025 的 A→C 两步走的第二步）；
`.trae/documents/studio-implementation.md` §八 的路标不变。