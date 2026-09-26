# 交接文档（M5 → 下一步）

> **给下一个接手这个仓库的 AI。**
> 这份文档只写**不在别处的东西**：当前状态快照、用户期望的工作节奏、
> 踩过的坑、以及下一步开工前必须先解决的问题。
>
> 常驻规则看 [`AGENTS.md`](../../AGENTS.md)（分层、测试、依赖、输出格式）；
> 规格看 `docs/`；每个里程碑的**决策与实测数据**看 `docs/decisions/`。
> 本文件**不重复**它们，只做索引与补充。
>
> **上一份是 `HANDOFF_2.md`（M4 → M5）**。它的 §1（工作节奏）、§2（环境细节）、
> §3（会故意变红的测试）、§7（不要重新讨论的决定）**依然全部有效**，本文件不重抄。
> 特别地：M5 期间**没有遇到任何"故意变红"的测试**——因为 M5 只给 Settings 加了一个布尔字段。

---

## 0. 一句话现状

**M0–M5 全部完成并逐个真机验收通过。** 项目现在是一个能跑、能聊、记得住、有情绪、
**能听能说、能被打断**、并且**在 Unity 里站着、会眨眼会笑会说、能播美术动画**的
本地 AI 伙伴。

```
质量门：1058 tests passed / ruff check+format 全绿 / mypy --strict 全绿（75 个源文件）
迁移：  0001_init → 0002_message_turn_index → 0003_memories_and_vectors → 0004_state
ADR：   0001 → 0024
空桩：  STUB_STAGES 已空
git：   两个 M5 commit（6bafac3、33e72aa）；只有 ADR 0024 的后续 30 行未提交
```

**下一个里程碑未定**，但用户已经选了方向：**做「Studio 应用」**——
把后端 / 文本 / 语音 / Unity 整合成一个能真正用起来的应用程序。
设计文档见 [`.trae/documents/studio-app.md`](studio-app.md)（**新上下文先读那份**）。

---

## 1. M5 有什么是新的（与前四个里程碑不同的地方）

**这是第一个有独立客户端工程的里程碑**，所以有几条新的现实：

1. **"跑一遍质量门 = 完事"在这里不成立**。后端全绿不代表 Unity 里能看到东西。
   M5 的验收步骤里有 6 步**只能由人在 Unity 里做**（见 README「验收（M5）」）。
2. **客户端代码不在这个仓库里**，也不在 git 里：
   `E:\UnityProject\VirtualTuber\Assets\Astra\`，**该工程是另外的 git 仓库**。
   这是用户明确选的（ADR 0024 §9），**风险已记录**。不要擅自 `git init`——先问。
3. **用户现在有自己的调试工具了**。M5 后半程的工作方式是"我给面板，用户在面板上调"：
   - 动作 / 表情 / 动画分配 → Unity 的**配置面板**（点按钮）
   - 站姿 / 表情强度 → Unity 的**滑杆**（实时生效）
   - 所以：**能做成面板的就别硬编码**。用户明确说过"一个个跟你说有一点小麻烦"。
4. **用户会自己准备素材**。46 个 FBX 是他自己在 Blender 里过了一遍、
   重命名、设成 Humanoid 之后放进来的。给建议时要考虑"用户能自己动手"这个前提。

---

## 2. 环境与操作细节（Windows）

### 2.1 新增：**Unity 侧怎么编译验证**（**这一节最重要**）

M5 期间踩到一个必踩的坑：**Unity 编辑器开着时，`-batchmode` 会被直接拒绝**：

```text
多个 Unity 实例无法打开同一个项目。项目：E:/UnityProject/VirtualTuber
（退出码 1073741845）
```

如果出现编辑器一直开着的情况，可以使用一条不依赖 batchmode 的编译验证路径：

```powershell
# 用 Unity 自己的 Roslyn + 它自己生成的编译参数，编译到临时路径
$rsp = Get-ChildItem "E:\UnityProject\VirtualTuber\Library\Bee\artifacts" -Recurse `
       -Filter "Assembly-CSharp.rsp" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$tmp="$env:TEMP\astra_check.rsp"; $out="$env:TEMP\astra_check"

(Get-Content $rsp.FullName -Raw) `
  -replace '-out:"[^"]*Assembly-CSharp\.dll"', "-out:`"$out.dll`"" `
  -replace '-refout:"[^"]*Assembly-CSharp\.ref\.dll"', "-refout:`"$out.ref.dll`"" `
  | Set-Content $tmp -Encoding UTF8

# ★ 关键：**新建的 .cs 不在旧 rsp 里**，要手动追加，否则报 CS0246 找不到类型
$existing = Get-Content $tmp -Raw
Get-ChildItem "E:\UnityProject\VirtualTuber\Assets\Astra\Scripts\*.cs" | ForEach-Object {
  if ($existing -notmatch [regex]::Escape($_.Name)) {
    Add-Content $tmp -Value ('"Assets/Astra/Scripts/' + $_.Name + '"')
  }
}

& "E:\2022.3.48f1c1\Editor\Data\NetCoreRuntime\dotnet.exe" exec `
  "E:\2022.3.48f1c1\Editor\Data\DotNetSdkRoslyn\csc.dll" "@$tmp"
# 0 error / 0 warning + 临时目录出现 .dll = 通过
Remove-Item "$tmp","$out.dll","$out.ref.dll","$out.pdb","$out.xml" -Force -ErrorAction SilentlyContinue
```

**这条路径是真实有效的**，M5 期间靠它抓到了 5 个编译错误（每个都是真 bug，不是风格问题）：
`CS0102` 字段重名 ×2、`CS1503` 参数顺序错、`CS0246` 新文件不在 rsp、`CS0104` `Random` 歧义。

**`cwd` 必须设成 `E:\UnityProject\VirtualTuber`**（rsp 里的源文件路径是相对路径）。

### 2.2 M5 用的其它固定事实

| 项 | 值 |
|---|---|
| Unity 编辑器 | **只有** `2022.3.48f1c1`：`E:\2022.3.48f1c1\Editor\Unity.exe` |
| Unity 工程 | `E:\UnityProject\VirtualTuber`（**是独立 git 仓库**） |
| UniVRM | `Packages/com.vrmc.vrm` + `com.vrmc.gltf` **0.131.0**，`autoReferenced: true` |
| 渲染管线 | **Built-in RP**（manifest 里**没有** URP） |
| 模型 | `Assets/StreamingAssets/Celandine.vrm`（VRM 1.0，15.33 MB） |
| 动画素材 | `Assets/Astra/Animations/`，**46 个 FBX**（原始在 `G:\ProjectsRelate\unity素材\EndField\Animation\未放缩\`） |
| 动画配置 | `Assets/StreamingAssets/astra_animation.json`（**已经生成过**，说明用户跑过了） |
| 后端的注释/文档 | 全中文。代码标识符英文。**回复也用中文** |

### 2.3 后端质量门（没变，四项全绿才算完成）

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
```

---

## 3. M5 到底改了什么

### 3.1 后端（刻意做到最小，**已提交**）

| 文件 | 改动 |
|---|---|
| `src/astra/settings/base.py` | `+avatar_enabled: bool = False` |
| `src/astra/api/routes/config.py` | `avatar_enabled` 从硬编码 `False` 改成读 Settings（**key 集合不变**） |
| `.env.example` | `ASTRA_AVATAR_ENABLED=false` 从「预留」区移出并取消注释 |
| `tests/integration/test_chat_api.py` | `+test_config_reports_avatar_enabled`（1057 → 1058） |
| `docs/07` §5 / §11 | Unity 版本改成实际用的 2022.3；`avatar.expression.set` 的 payload **去掉不存在的 `duration_ms`** |
| `docs/08` 阶段 5 | 补「实际落点」小节 |
| `docs/decisions/0024-m5-avatar-runtime.md` | **新增** |
| `README.md` | M5 段落 + 验收 6 步 + 已知限制 |

**`avatar_enabled` 是纯通告，不门控任何东西**（ADR 0024 §2）——
`avatar.*` 事件无论它为何值都发布。**别以为它该门控什么。**

**没有新迁移**：形象状态全部从 turn 派生。

### 3.2 Unity 客户端（15 个 `.cs` + 1 份文档，**是独立 git 仓库**）

```
E:\UnityProject\VirtualTuber\Assets\Astra\
├─ ANIMATION.md              39 KB ★ 动画/表演的完整清单 + 骨骼归属约定 + 怎么加新的
└─ Scripts\
   ├─ AstraConfig.cs          常量（后端地址、过渡时长、口型增益）
   ├─ AstraRestPose.cs        放松站姿（T-pose → A-pose）的可调参数
   ├─ AstraPoseController.cs  骨骼姿态的**单一收口**（基准姿态 × 各来源偏移之和）
   ├─ AstraAvatar.cs          VRM 加载 + 情绪→表情映射 + avatar/模型能力诊断
   ├─ AstraExpressionLibrary.cs ★ 表情变体表（18 个变体）—— 加表情只改这里
   ├─ AstraFace.cs            表情变体引擎（抽变体 / 叠 blendshape / wink / 头部微动）
   ├─ AstraIdle.cs            呼吸 / 眨眼 / 视线 / 说话 / 聆听 / 偶发小动作
   ├─ AstraActions.cs         11 个动作的程序化手势（缓动 + 预备动作 + 过冲）
   ├─ AstraLipSync.cs         口型（30 ms RMS 包络驱动 Aa）
   ├─ AstraClient.cs          /v1/config + ClientWebSocket + 主线程队列 + 重连
   ├─ AstraClipLibrary.cs     扫描 Animations 目录（编辑器下用 AssetDatabase）
   ├─ AstraAnimator.cs        外部动画播放层（PlayableGraph + crossfade + 姿态调度）
   ├─ AstraAnimationConfig.cs 动画映射配置（json 落盘）
   ├─ AstraAnimationSetup.cs  ★ 动画配置面板（试播 + 分配 + 调权重）
   └─ AstraBootstrap.cs       运行时自建场景 + 界面 + 开发者面板
```

**两个 ★ 是"用户直接操作的东西"**：加表情改 `AstraExpressionLibrary`，
加动画映射用 `AstraAnimationSetup` 的面板（不用改代码）。

---

## 4. M5 的实测结论（**不要再凭直觉重做一遍**）

### 4.1 轴向：**推导和实测相反，以实测为准**

我从 `Celandine.vrm` 的 glTF 里解出了骨骼局部轴：

```text
上臂 / 前臂：local X = 世界 +X、local Y = +Y、local Z = +Z
T-pose 下：左臂指 +X、右臂指 -X
```

按这个推导，"绕 local Z 转负角"是放下手臂。**错。**
**Unity 是左手系，UniGLTF 导入时会做轴翻转**，所以实际方向是反的。
用户按 Play 实测：**正角让手臂向上**。

⇒ 最终符号（`AstraRestPose`）：放下 = `左 +ArmDown / 右 -ArmDown`；
抬起 = `左 -(target+ArmDown) / 右 +(target+ArmDown)`。
两条规则分别由 `UpperArm()` 与 `ArmRaiseOffset()` 负责，**手势不要自己拼符号**。

> **教训：坐标系推导不能代替实测。** 尤其跨右手系（glTF）↔ 左手系（Unity）时。

### 4.2 VRM 模型是 **T-pose**，必须自己掰成站姿

VRM 规范要求模型以 T-pose 存储（给绑定用）。所以直接用骨骼基准姿态 = 她是个"晾衣架"。
`AstraRestPose` 就是干这个的（这是用户实测反馈"idle 一直是 T-pose"之后加的）。

### 4.3 表情：56 个 blendshape，VRM 只占了 15 个

实测解出的索引表（**这是最重要的一张表**）：

```text
VRM 标准表情占用的索引 —— 每帧会被 VRM 覆盖，不能裸写：
  0-5    Fcl_ALL_Neutral/Angry/Fun/Joy/Sorrow/Surprised   （"整张脸"合成形）
  13/14/15  Fcl_EYE_Close / _R / _L
  39-43  Fcl_MTH_A / I / U / E / O                        （口型）

剩下 41 个空闲 —— `AstraFace` 直接 SetBlendShapeWeight 写它们：
  6-10   Fcl_BRW_Angry/Fun/Joy/Sorrow/Surprised   （眉毛）
  16-24  Fcl_EYE_Joy/Fun/Sorrow/Surprised/Spread/Iris_Hide/... （眼睛细节）
  26-38  Fcl_MTH_Up/Down/Small/Large/Fun/Joy/...  （嘴部细节）
  44-55  Fcl_HA_*（头发）
```

**为什么裸写是安全的**：已核对 UniVRM 源码 ——
`MorphTargetBindingMerger.Apply()` 只写它自己累积过的索引（`if (!weight.HasValue) continue;`），
**别人的一概不碰**。

**"整脸合成形"和"分部位形态键"是两套东西，强度要分别控制**：
用户实测"happy 眼睛眯太狠、嘴张太大"，把 `Gain` 调到 0.2 也压不住 ——
因为那张嘴是 `Fcl_ALL_Joy` 自带的。所以有**两个旋钮**：
`AstraAvatar.BaseScales`（压底表情，默认 happy/excited = 0.5）与 `AstraFace.Gain`（细节层）。

### 4.4 后端 `emotion` 词表**不能扩**（三条理由）

用户想扩到"一二十个表情"。**没有扩词表**，而是把多样性放在客户端的"表情变体层"：

1. **后端 `emotion` 同时喂状态系统**（ADR 0020：7 维情绪按"已表达情绪"靠拢）
   → 往词表里塞 `wink` 会让"抛个媚眼"变成"心情变好"，语义就错了。
2. **token 有实测代价**：M1 量过，输出 7 字段 15.2 s ↔ 4 字段 8.5 s。
3. **小模型挑不准**：`nemotron-3-nano-4b` 连 8 个情绪都会吐词表外的值（`nervous`）。

⇒ 后端给 8 个语义情绪（协议契约），客户端把它演成 **18 个变体**
（按权重抽 + 强度门槛 + 同情绪不连续重复）。

**这次扩的也不只是情绪**：`AstraFace` 的权重是**按情绪名**存的，写回 VRM 时才按 max 汇总 ——
这是 `excited`/`happy` 能长得不一样的前提（按键插值它们必然完全一样）。

### 4.5 Unity 没有给 `Task` 装 SynchronizationContext

`await` 一个 `Task` 之后的续体**跑在线程池上**，碰任何 Unity API 都会炸。所以：

- **加载 VRM**：`var task = Vrm10.LoadPathAsync(...); while (!task.IsCompleted) yield return null;`
  **绝不 `await`**。
- **WebSocket 接收**（只有异步 API）：只把帧塞进 `ConcurrentQueue`，在 `Update()` 里出队处理。

### 4.6 `PlayableGraph` 播外部动画：**mixer 不能静默**

用 `PlayableGraph` 而不是 AnimatorController（46 个 clip 就要 46 个 state，每次增删都得重做资产）。
但有一条**必须记住**：

> **如果让 mixer 静默（权重全 0），Animator 会输出绑定姿态 = 弹回 T-pose。**

所以设计成"**永远播点什么**"：永远有一个默认待机 clip 在播，动作 crossfade 叠上来、播完淡回去。
**`Stop()` 的语义因此等于"回到待机"。**

### 4.7 Humanoid 重定向：缩放不用担心；位移要关

- **按肌肉归一化** → 骨骼长短、FBX import scale **都不影响动作形状**。
  这是 Humanoid 相对 Generic 的全部价值。
- **`applyRootMotion = false`**：Humanoid 下 Unity 会丢弃 `RootT`（根位移）、只应用
  `BodyPosition` → **走路动画自动变成原地踏步**，正合"她站着聊天"。

### 4.8 姿态（坐/躺）只需要**一个变量**，不需要 Mecanim

`Sitting` / `Lying Down` 这类 clip 是"状态改变"而不是"动作"，**不该和 `nod` 混在一个池子里**。
模型是：`姿态 = stand/sit/lie` + 每个非站姿态配 `enter → loop → exit` 三件套。

**三件套齐全才启用**（`CanSwitchPose`）—— 缺 `exit` 她会一直躺着起不来；
crossfade 只能让她**弹起来**，程序化缓动只能让她**飘起来**，都比不做更糟。

### 4.9 IMGUI 的中文要自己建字体

Unity 默认字体不含中日韩字形（显示成方块）。用
`Font.CreateDynamicFontFromOSFont("Microsoft YaHei", 14)`，零资源零依赖。

---

## 5. M5 期间真实踩到的坑

| 坑 | 表现 | 教训 |
|---|---|---|
| **`-batchmode` 被锁** | "多个 Unity 实例无法打开同一个项目"、退出码 1073741845 | 用户习惯开着编辑器 → 用 §2.1 的 Roslyn 路径 |
| **新 `.cs` 不在旧 rsp 里** | `CS0246: 找不到类型` | 用 rsp 编译验证时要**手动追加新文件** |
| **`_pose` 字段重名** | `CS0102`（`AstraAnimator` 里姿态控制器与姿态名字撞了） | 命名要带语境：`_poseController` vs `_poseName` |
| **`using System;` 让 `Random` 歧义** | `CS0104`（`System.Random` vs `UnityEngine.Random`） | 要么不引，要么全限定；用 `System.StringComparison.Ordinal` 更省事 |
| **`Init` 参数顺序写反** | `CS1503`（`AstraActions.Init(pose, avatar)`） | 同类型参数多的方法，调用点要逐个核对 |
| **"笑完一直张着嘴"** | 表情设上去**永不回落** | 后端一轮只发一次 `expression.set` → 必须有 `HoldSeconds` 回落 |
| **打断后嘴永远张着** | `Oh` 键残留 | **`StopCoroutine` 会掐断协程，它末尾的清理语句不执行** → 必须显式补清理 |
| **偶发小动作的视线残留** | 她的视线永远偏着 | 骨骼偏移不用清（每帧重算），但**表情键必须显式清** |
| **`happy`/`excited` 看起来一样** | 用户实测反馈 | 8 情绪挤 6 表情 → 按键插值必然相同；要按**名字**插值 + 细节层区分 |

---

## 6. 未完成 / 待用户做的事

| 项 | 状态 |
|---|---|
| **46 个动画的映射** | 工具就绪（配置面板），**用户在配**。不用催 |
| **表情数值调优** | 面板有「底表情强度」+「细节强度」两个滑杆，**用户在调** |
| **B 类三件套** | 代码完成，**等用户试播确认哪几个 clip 能配对**（`Type To Sit` / `Situp To Idle` 方向未确认） |
| `AstraRestPose.ArmForward` / `ElbowBend` | **方向未实测**，滑杆可改到负数。用户没反馈说明可能是对的 |
| **Unity 内麦克风采集** | M5 明确不做（"只做能看"）。语音输入走浏览器 `/dev` |
| **idle 的 yawn/stretch** | 已作为**动作**实现，idle 里刻意不重复触发 |
| **房间 / 氛围背景** | 未开始。已给建议：**先做"氛围"（打光/色调/相机/Skybox，成本几乎为零）**，再考虑 AI 生成整间房（有材质/管线/显存三个坑） |
| **Studio 应用** | **下一个里程碑**，见 [`studio-app.md`](studio-app.md) |
| **操作电脑（像 neuro）** | 用户说"不着急"。⚠️ **与 `docs/00` §4「当前明确非目标：自动玩游戏」冲突**，要做必须先改文档 + 记 ADR |
| **M6 Tools / M7 主动行为** | 按 `docs/08` 还没做。Tool Registry 基础设施（P0#9）已就绪，`tool_calls` 还没加 |

---

## 7. 不要重新讨论的决定

`HANDOFF_2.md` §7 那份**全部继续有效**。M5 新增的：

- **Unity 用 2022.3.48f1c1 + UniVRM 0.131.0**（不是 `docs/07` 原本推荐的 Unity 6）
- **动作用程序化手势 + 可选的外部 clip**（`AstraActions` 先查配置，没配就退回程序化）
- **外部动画用 `PlayableGraph`，不用 AnimatorController**
- **客户端源码不进版本控制**（用户选的；风险记在 ADR 0024 §9）
- **`avatar_enabled` 是纯通告，不门控任何事件**
- **后端 `emotion` 词表不动，表现力放在客户端的变体层**
- **表情不是可下载的资产**（它是模型自带的 blendshape，随模型走）
- **`avatar.look_at.set` / `avatar.lipsync.*` 不登记、不发布**（没有消费者）

---

## 8. 关键文件速查（M5 新增部分）

```
.trae/documents/
├─ HANDOFF_2.md            # M4 → M5（§1/§2/§3/§7 依然有效）
├─ HANDOFF_3.md            # 本文件
├─ m5-avatar.md            # M5 实施计划（含风险清单）
└─ studio-app.md           # ★ 下一个里程碑的设计文档

docs/decisions/0024-m5-avatar-runtime.md   # M5 的全部决策与理由

E:\UnityProject\VirtualTuber\Assets\
├─ Astra\ANIMATION.md              # ★ 动画/表演的完整清单 + 骨骼归属约定
├─ Astra\Scripts\                  # 15 个 .cs（见 §3.2）
├─ Astra\Animations\               # 46 个 FBX
└─ StreamingAssets\
   ├─ Celandine.vrm                # 模型
   └─ astra_animation.json         # 动画映射（用户在配）
```

**读代码时的顺序建议**：`ANIMATION.md` → `AstraPoseController`（骨骼收口）→
`AstraAnimator`（动画播放）→ 其余。

---

## 9. 最后

M5 的代码注释和 `ANIMATION.md` 都写得**很密**（为什么这么做、否决了什么、实测数字）。
**改动前先读那一段的注释**——大概率你会看到"为什么不能那样改"。

几条这个仓库反复验证过的原则：

1. **轴向 / 坐标系 / 跨系转换：必须实测**，推导只能用来缩小范围（§4.1 的教训）。
2. **"整脸合成形"和"分部位形态键"强度要分开控制**（§4.3）。
3. **协议契约（词表、`/health` 的 key、`/v1/config` 的 key）越稳定越好**，
   表现力往客户端放（§4.4）。
4. **能做成面板的就别硬编码**——用户明确说过"一个个跟你说有一点小麻烦"。
5. **`StopCoroutine` 掐断协程时，末尾的清理语句不执行**（§5）——
   所以"打断路径"必须显式补清理。