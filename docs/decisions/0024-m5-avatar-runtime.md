# ADR 0024 — M5 客户端运行时与形象取舍

## Decision

### 1. 形象事件由**后端**发布（正式确认）

`avatar.expression.set` / `avatar.action.play` 在 **M3 就已经由
`Orchestrator._publish_assistant_output` 发布**了，与 `docs/04` §2 的事件序列一致：
紧跟 `assistant.text.completed`、排在状态与记忆事件之前。

> 交接文档 `HANDOFF_2.md` §6.2 说"没有任何地方发布它们"，那是**过时信息**——
> 写那份文档时还没实现，之后 M3 一并做了。

所以 M5 **没有**第二个选项（"客户端从 `state.emotion.updated` 自己推导"）需要讨论：
客户端不用理解状态模型，只需消费两个语义事件。

### 2. `avatar_enabled` 是**纯通告**，不门控任何东西

`settings.avatar_enabled` 只做一件事：通过 `GET /v1/config` 告诉客户端"后端知道形象这回事"。
**无论它为何值，`avatar.*` 事件都照常发布。**

理由：

- `docs/04` §2 的既定事件序列**本身就无条件列出这两个事件**；
- 开发者控制台的原始事件流与 README 里 M1 的验收步骤（"应依次打印 …
  `avatar.expression.set` → `avatar.action.play` → `turn.completed`"）都依赖能看到它们——
  加门控会让默认值（`false`）下这条验收直接失效；
- 两条事件加起来几十字节，代价可忽略。

**这条必须写在这里**，否则下一个人会以为它该门控什么。

### 3. Unity 用 **2022.3.48f1c1 + UniVRM 0.131.0**，不是 `docs/07` §5 推荐的 Unity 6 LTS

实测本机环境：

| 项 | 实际 |
|---|---|
| Unity 编辑器 | **只有** `2022.3.48f1c1`（`E:\2022.3.48f1c1\Editor\Unity.exe`） |
| UniVRM | `com.vrmc.vrm` + `com.vrmc.gltf` **0.131.0**，已装在 `Packages/` 里，`unity: 2021.3` |
| 模型 | `Celandine.vrm`，VRM **1.0**（vrmlib 的 `XYZ_RightUpForward_RH`，朝 **+Z**） |

`docs/07` §5 推荐 Unity 6 LTS，但本机没装（要下载数 GB）、工程要迁移、UniVRM 要重装。
0.131.0 的 `displayName` 就是 `VRM-1.0`，导入器与运行时都满足需求。
**按 AGENTS.md「文档与实现冲突时不要默默选择」，这里同步把 `docs/07` §5 改成实际版本。**

### 4. 动作走**程序化骨骼手势**，不引入美术管线

实测：这个 Unity 工程的 `Assets` 下**没有任何动画资源**（无 `.anim` / `.controller` /
`.prefab`），而 `docs/07` §6 要求动作是**有限集合**（11 个），且不能由 LLM 任意生成。

所以 11 个动作全部用 humanoid 骨骼的旋转在 C# 里实现（点头、摇头、挥手、歪头、
前倾笑、害羞、伸懒腰、打哈欠、视线偏移/回正）。
**代价是观感明显朴素**——它不是"美术级动作"，而且不同 VRM 的骨骼局部轴可能不同，
手臂手势的旋转轴是按常见 humanoid 朝向猜的，可能需要在 `AstraActions` 里按观感调轴。

### 5. 情绪 8 → VRM 6 的映射表，**近似是已知的**

后端情绪词表有 8 个（`docs/03` §5 / `/v1/config`），VRM 1.0 只有 6 个标准表情：

| 后端 | VRM | 说明 |
|---|---|---|
| `neutral` / `happy` / `sad` / `angry` / `surprised` | 同名 | 直接对应 |
| `excited` | `happy` | VRM 没有 excited |
| `sleepy` | `relaxed` | 半闭眼，最接近 |
| `embarrassed` | `relaxed` | **最勉强的近似**，很可能"看起来不像害羞" |

映射表写在 C# 里（`AstraAvatar.EmotionMap`），**不从 `/v1/config` 取**——
词表是协议（ADR 0015），"映射到哪个 VRM 表情"是 **Unity 的实现细节**，
符合 AGENTS.md 的「Backend sends semantic events. Unity decides animation implementation」。

### 6. 口型是**振幅包络驱动 `Aa`**，不做 phoneme / viseme 时序

`docs/07` §9 明确"MVP 可以先做 volume based"。服务端是整段批发音频（ADR 0011 / 0022），
所以客户端能拿到完整 PCM：先收完所有二进制帧，用 `tts.started.chunk_count` 自检帧数，
再建 `AudioClip` 一次性播，同时预计算 30 ms 窗口的 RMS 包络、按播放位置查表驱动 `Aa`。

**不做音素对齐**：那需要音素时序，后端不提供。这是 M5 的已知限制。

包络**按峰值归一化**：TTS 每次的整体音量可能不同，不归一化的话增益就没有可比性。

### 7. 客户端用 .NET 自带的 `ClientWebSocket`，**零新增依赖**

ADR 0021 规定二进制帧**不是事件**、必须紧跟说明格式的 JSON 帧。区分方式就是
`ClientWebSocket.ReceiveAsync` 返回的 `WebSocketReceiveResult.MessageType`
（`Text` / `Binary`）——一个自带的类就能干净地做到，不需要引
NativeWebSocket / websocket-sharp 之类的第三方库。

JSON 用工程里**已经有**的 `com.unity.nuget.newtonsoft-json 3.2.2`
（`JsonUtility` 处理不了动态 payload）。

### 8. VRM 用**协程轮询**加载，因为 Unity 没有给 `Task` 装 SynchronizationContext

`Vrm10.LoadPathAsync` 返回 `Task<Vrm10Instance>`。在 Unity 里 **`await` 它之后的续体会跑在
线程池上**，之后碰任何 Unity API 都会抛异常。所以：

```csharp
var task = Vrm10.LoadPathAsync(path, ...);
while (!task.IsCompleted) yield return null;   // 协程轮询，保证回到主线程
var instance = task.Result;
```

同理，WebSocket 的接收循环（只有异步 API）跑在线程池，它**只把帧塞进
`ConcurrentQueue`**，真正的处理在 `Update()` 里做。

`controlRigGenerationOption` 取 `None`：我们用 Animator 的 humanoid 骨骼直接驱动，
生成 control rig 只是白费内存。

### 9. 客户端源码**不进版本控制**（已知风险，用户已知悉并选择）

Unity 工程 `E:\UnityProject\VirtualTuber` **不是 git 仓库**（无 `.git`、无 `.gitignore`）。
用户选择"源码直接写在 Unity 工程里"，因此 `Assets/Astra/Scripts/` 目前**没有任何版本历史**，
误删即永久丢失。

**这是本里程碑最实在的一处风险。** 最便宜的保险是在该工程里 `git init` + 一份标准
Unity `.gitignore`；本 ADR 只记录现状，不擅自执行。

### 10. 未做

- **Unity 内麦克风采集**：M5 范围是"只做能看"。语音输入仍走浏览器控制台
  `/dev`（那条路径 M4 已经打通）。Unity 端只做"播 TTS 音频 + 驱动口型"。
- **idle 的 `yawn` / `stretch`**：`docs/07` §10 的 idle 状态机列了它们，但它们已经作为
  **动作**实现了，idle 里再触发会变成"她没事就伸懒腰"，所以刻意不重复。
- **`avatar.look_at.set` / `avatar.lipsync.start` / `avatar.lipsync.stop`**：
  `docs/04` §4 列了，但**不登记进 `EventType`、也不发布**。视线用
  `Look*` 表情键在客户端内部实现，口型随 `tts.*` 自动起止——没有消费者就不做死登记
  （与 ADR 0020 / 0022 移除死登记的同一理由）。
- **Unity 侧的自动化测试**：C# 不进 CI，也不值得为一个开发用客户端搭 Unity Test
  Framework。守门的是后端测试 + 用户亲手验收。
- **场景文件**：不发 `.unity` 资产。用 `[RuntimeInitializeOnLoadMethod]` 在运行时
  自建相机/灯光/角色/界面，用户只需按 Play。副作用是它会在**任意场景**执行——
  这个工程是 Astra 专用壳，可接受。

## Reason

**为什么范围切在"能看"**：口型只需要"能播 `tts.started` 给的音频"，而麦克风采集是
另一套完全不同的坑（设备权限、采样率转换、重采样、Unity 的 `Microphone` API 在编辑器
里的行为）。先做完一半能看到效果，比两半都半成品强。

**为什么不做美术资源**：用户明确不想在这个里程碑引入美术管线。程序化手势能立刻验证
"事件 → 动作"这条链路是通的，将来换成正式动画资源时，只需替换 `AstraActions`，
其余（事件、协议、控制器分层）都不用动。

## Consequences

- **`docs/07` §5 与 §11 被改**：§5 的 Unity 6 LTS 推荐改成实际使用的
  Unity 2022.3 LTS + UniVRM 0.131.0；§11 的 `avatar.expression.set` payload
  补上 `turn_id`、**去掉实现里不存在的 `duration_ms`**（过渡时长由客户端定）。
- **`docs/08` 阶段 5 补「实际落点」**，与阶段 2/3/4 同格式。
- **`avatar_enabled` 进了 `.env.example`**（从"预留"区移出并取消注释）。
  新增 Settings 字段必须同步 `.env.example`，否则 `extra="forbid"` 会让
  `Copy-Item .env.example .env` 之后启动直接失败（M0/M1/M2 都踩过）。
- **Unity 侧观感需要现场调**：动作幅度、手臂旋转轴、口型增益都集中在
  `AstraConfig.cs` 与 `AstraActions` 的常量里，看到效果后一次性调。
- **`Assets/StreamingAssets/Celandine.vrm` 是 `Assets/Models/Celandine.vrm` 的副本**：
  运行时加载需要真实文件路径，`StreamingAssets` 在 Windows 编辑器下就是真目录。
  这只在桌面端成立，将来上 Android 要改走 `UnityWebRequest`。
- **音频总线是全局广播**（ADR 0021 的既有局限）：Unity 与浏览器 `/dev` 同时在线时
  **两边都会收到音频**。单用户本地场景可接受，验收时会看到这个现象。
- **动画的完整清单与骨骼归属约定**记在 Unity 工程里的
  `Assets/Astra/ANIMATION.md`（客户端实现细节，不入后端仓库）。
  M5 之后又补了几层**纯代码**、不需要美术资源的表演：

  1. **放松站姿**（`AstraRestPose`）：模型按规范是 T-pose，站着不动完全不像活人。
     这里叠一组固定的静止姿态偏移把手臂放下来（A-pose）。
     **轴向是实测解自 VRM 的 humanoid 骨骼**（上臂/前臂的 local Z = 世界 +Z，
     T-pose 下左臂指向 +X、右臂指向 -X），不是猜的——否则手臂会往奇怪的方向甩。
  2. **第 1 层**（`AstraIdle`）：呼吸、肩膀起伏、重心转移、头部漂移、
     **说话时随重音点头 / 句尾上扬**、**聆听时前倾点头**、偶发歪头/看别处/换腿。
  3. **表情细节层**（`AstraFace`）：VRM 1.0 只有 6 个标准表情，而模型有 56 个 blendshape——
     标准只占了 15 个索引。以 `happy` 为例，`Fcl_ALL_Joy` 是"整脸"合成形、一次性给满，
     看起来就是硬切；叠上 `Fcl_EYE_Joy`（眯眼）+ `Fcl_BRW_Joy`（眉毛），观感立刻不同。
     做法是**绕过 VRM 表达式系统**，直接 `SkinnedMeshRenderer.SetBlendShapeWeight`
     写那批没有 VRM 表达式在用的索引（互不冲突）。
  4. **第 2 层**（`AstraActions`）：缓动 + 预备动作 + 过冲 + 不对称，手势不再"线性正弦"。
  5. **开发者面板**：把每个动作/表情做成按钮单独触发，站姿与细节强度做成实时滑杆——
     没有它就只能靠"发一句对话"去碰运气，微调无从下手。

  接外部美术动画是第 3 层，见该文档 §13。
- **8 个情绪挤在 6 个 VRM 表情上，靠"表情变体层 + 按情绪名插值"区分**：
  用户实测反馈 `excited`/`happy`、`sleepy`/`embarrassed` 看起来完全一样——根因就是
  VRM 只有 6 个标准表情。修法是两条：
  (a) 情绪权重**按名字**各自插值，写回时才按 max 汇总到 VRM 键；
  (b) `AstraFace` 用**按情绪名**的变体表把近似项拉开差距。
  `sleepy` 的半闭眼走 `AstraAvatar.BlinkBias`（`AstraFace` 写、`AstraIdle` 消费），
  而不是直接写 `Fcl_EYE_Close`——那个索引归 VRM 的 `blink`，两个写入者会互相抢。
- **表情的多样性放在客户端，后端情绪词表不动**（`AstraExpressionLibrary`）：
  用户要求扩到"一二十个表情"（wink / 星星眼 / 坏笑…）。**没有往 `EMOTIONS` 里加词**，
  三条理由：
  1. **后端 `emotion` 同时喂状态系统**（本文档上游的 ADR 0020：7 维情绪按"已表达情绪"
     靠拢）。往词表里塞 `wink` 会让"抛媚眼"变成"心情变好"，语义就错了。
  2. **token 有实测代价**：M1 量过，输出 7 字段 15.2 s ↔ 4 字段 8.5 s。
  3. **小模型挑不准**：`nemotron-3-nano-4b` 连 8 个情绪都会吐词表外的值。
  ⇒ 后端给 8 个**语义**情绪（协议契约），客户端把它演成 18 个**变体**
  （按权重抽 + 强度门槛 + 同情绪不连续重复）。加变体只改
  `AstraExpressionLibrary` 一个文件，协议 / 后端 / bootstrap 都不动。
- **wink 是一次性动作，不是持续状态**：它走 VRM 已注册的 `BlinkLeft` / `BlinkRight` 键
  （与 `AstraIdle` 的双眼 `Blink` 相加，不抢键），用协程做"延迟 → 闭合 → 张开"约 0.6 s。
  如果按情绪那样保持 8 秒，看起来就是"眼睛坏了"。
- **表情不是可下载的资产**：它是模型自带的 blendshape，随模型走。网上能下的是模型 /
  贴图 / mocap，不是"表情插件"——所以能做的上限 = 该模型的形态键能组合出什么。
  本模型缺的是需要**新贴图/材质**的那类（脸红、真星星眼）。
- **放松站姿的手臂符号是"实测修正"的，不是推导的**：从 glTF 解出的局部轴是右手系，
  而 Unity 是左手系、UniGLTF 导入时会翻转，所以 glTF 推导出的旋转方向在 Unity 里是反的。
  用户按 Play 实测确认"正角让手臂向上"后才定下符号。**这类改动的教训：轴向必须实测。**
- **`AstraPoseController` 为第 3 层预留了双模式**：检测到
  `animator.runtimeAnimatorController != null` 时切到"读当前姿态 → 叠加 → 写回"，
  这样外部动画**不会**被手写旋转擦掉（否则表现就是"动画播了但骨骼不动"）。
  分层问题因此已经解决，不需要等到接动画时再改架构。
- **第 3 层的素材已实测可用**：本地 `G:\ProjectsRelate\unity素材\EndField\Animation`
  那批 FBX 是**纯骨骼、Mixamo 标准命名、无手指骨骼**（`mixamorig:Hips/Spine/...`），
  正是 Unity Humanoid 自动映射最理想的形态，不需要重定向工具。
  已复制一个进 `Assets/Astra/Animations/` 供验证；剩下的唯一一步需要在编辑器里
  把 Rig 设成 Humanoid（客户端动作，无法由代码代替）。
