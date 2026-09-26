# M5 Avatar — 实施计划

## Context

M0–M4 已完成并逐个真机验收通过。当前质量门：**1057 tests passed / ruff check+format 全绿 /
mypy --strict 全绿（75 个源文件）**；迁移到 `0004`，ADR 到 `0023`，`STUB_STAGES` 为空集。

现在实现 **M5「Avatar」**——`docs/08_IMPLEMENTATION_PLAN.md` 阶段 5：
Unity project、VRM import、WebSocket client、expression controller、action controller、
lip sync、idle system。

**验收标准（文档原文）**：

```text
- 文本聊天会驱动表情。
- TTS 会驱动口型。
- action 能正确播放。
- Unity 断开不会影响 backend。
```

**这是第一个有独立客户端工程的里程碑**：前四个里程碑"跑一遍质量门 = 完事"在这里不成立，
后端全绿不代表 Unity 里能看到东西。所以本计划的验收章节必须以**用户亲手操作**的步骤为主。

**已由用户拍板**（本轮，均给出了推荐项与理由）：

1. **范围：只做「能看」**——VRM 显示 + 表情 + 动作 + 口型 + idle。麦克风采集仍走浏览器
   控制台（`/dev`）。M5 不做 Unity 内录音。
2. **动作：代码程序化手势**——用 VRM humanoid 骨骼在 C# 里驱动，零美术资源。
3. **Unity 版本：保留 2022.3.48f1c1**——本机唯一已装版本，UniVRM 0.131.0 已就位。
   （`docs/07` §5 推荐的 Unity 6 LTS 与实际环境冲突，按 AGENTS.md 不能默默偏离 →
   同步改文档 + 记 ADR。）
4. **场景接线：全自动**——运行时代码自建一切，用户只需按 Play。

---

## 一、已实测确认的环境事实

| # | 事实 | 影响 |
|---|---|---|
| 1 | Unity **2022.3.48f1c1**（`E:\2022.3.48f1c1\Editor\Unity.exe`），Hub 里**只有这一个**版本 | 不需要下载、不需要迁移工程 |
| 2 | UniVRM **0.131.0** 已装：`Packages/com.vrmc.vrm`（`displayName: VRM-1.0`）+ `Packages/com.vrmc.gltf`，`unity: 2021.3` | **不需要装包**。VRM 1.0 导入器已就位 |
| 3 | `com.vrmc.vrm/Runtime/VRM10.asmdef` 是 `autoReferenced: true` | 脚本放 `Assets/`（Assembly-CSharp）即可直接 `using UniVRM10;`，**不需要写 asmdef** |
| 4 | `com.unity.nuget.newtonsoft-json 3.2.2` 已在 `manifest.json` | JSON 解析可用（事件信封是动态 payload，`JsonUtility` 处理不了） |
| 5 | 模型 `Assets/Models/Celandine.vrm`：合法 glTF 二进制、**15.33 MB**、**VRM 1.0** | 可用。运行时从 `StreamingAssets` 加载 |
| 6 | 模型自带表情 = VRM 1.0 标准全集：`neutral/happy/angry/sad/relaxed/surprised` + `blink/blinkLeft/blinkRight` + `lookUp/Down/Left/Right` + **口型 `aa/ih/ou/ee/oh`** | 口型 blendshape 是现成的，省一大截事；但**没有** `embarrassed/excited/sleepy` |
| 7 | **`Assets` 下没有任何动画资源**（无 `.anim` / `.controller` / `.prefab`），`Assets/Scripts` 为空 | 11 个动作无米下锅 → 走程序化（已拍板） |
| 8 | `Assets/StreamingAssets` 不存在 | 需要新建并把 VRM 复制进去 |
| 9 | 运行时加载 API 已确认：`Vrm10.LoadPathAsync(path, canLoadVrm0X, controlRigGenerationOption, showMeshes, ...) -> Task<Vrm10Instance>`；`ControlRigGenerationOption.None = 0` 存在 | 加载路径确定，用 `None`（用 Animator 骨骼，不生成 control rig） |
| 10 | 表情驱动 API 已确认：`Vrm10Instance.Runtime.Expression.SetWeight(ExpressionKey, float)`；`ExpressionKey.Happy/Angry/Sad/Relaxed/Surprised/Neutral/Aa/Ih/Ou/Ee/Oh/Blink/BlinkLeft/BlinkRight/LookUp/Down/Left/Right` 都是现成的静态属性 | 表情与口型实现路径确定 |
| 11 | 后端**已经在发布** `avatar.expression.set` / `avatar.action.play`（`orchestrator.py` 的 `_publish_assistant_output`） | **交接文档 §6.2「没有人发布它们」是过时信息**。M5 后端改动因此极小 |
| 12 | `avatar_enabled` 在 `api/routes/config.py` 硬编码 `False`，`Settings` 里没有该字段，`.env.example` 里仍是注释 | 需要：加 Settings 字段 + 取消注释 + 读真值 |
| 13 | Unity 工程**不是 git 仓库**（无 `.git`、无 `.gitignore`） | 客户端源码不进版本控制（已拍板）。风险记进 ADR |

---

## 二、技术选型结论

### 2.1 后端改动（刻意做到最小）

M5 里后端只做三件事，**不动任何事件协议、不加迁移**：

1. `settings/base.py` 加 `avatar_enabled: bool = False`
   （一个布尔字段，**不新开 mixin 文件**——为单个字段抽模块是过度设计；放在
   "运行环境 / HTTP / 数据"那一段之后，加一节 `# ── Avatar ──`）。
2. `.env.example` 把 `ASTRA_AVATAR_ENABLED=false` 从「预留」区**移出来并取消注释**
   （§3 的同步规则：新增 Settings 字段必须同步，否则 `extra="forbid"` 会让启动失败）。
3. `api/routes/config.py`：`avatar_enabled=settings.avatar_enabled`。**key 集合不变**
   （客户端从 M0 起就写死解析这个 key）。

**`avatar_enabled` 的门控语义：纯通告，不门控任何东西。**

- `avatar.expression.set` / `avatar.action.play` **继续无条件发布**。理由：
  (a) 它们每条只有几十字节，代价可忽略；
  (b) `docs/04` §2 的既定事件序列**本身就是无条件列出它们的**；
  (c) 开发者控制台的原始事件流与 M1 验收都依赖能看到这两条事件——
  加门控会让 `avatar_enabled=false`（默认值）时 M1 的验收步骤直接失效。
- 所以它是给客户端的一句"后端知道形象这回事"的通告，Unity 端读它只用于日志提示。
  这条**必须写进 ADR**，否则下一个人会以为它该门控什么。

> 不新增迁移：形象状态（表情/动作）全部从 turn 里派生，没有任何需要持久化的领域状态。

### 2.2 Unity 工程布局

```
E:\UnityProject\VirtualTuber\          # 现有壳，不用改 ProjectSettings / manifest
├─ Assets/
│  ├─ Astra/
│  │  └─ Scripts/                      # 新：全部 C# 源码（不进 git，已拍板）
│  ├─ StreamingAssets/                 # 新：Celandine.vrm 的副本（运行时加载源）
│  ├─ Models/Celandine.vrm             # 保持原样（编辑器导入用，运行时不用它）
│  └─ Scenes/SampleScene.unity         # 保持原样，不手写场景 YAML
```

**为什么不手写场景文件**：用了 `[RuntimeInitializeOnLoadMethod(BeforeSceneLoad)]`，
脚本会在**任意场景**进入 Play 时自建相机 / 灯光 / 画布 / 客户端对象。
于是"打开工程 → 按 Play"就够了，不需要写 `.unity` YAML（手写 YAML 要处理 GUID 引用，
是纯粹的风险而不带来收益）。

**为什么用 `StreamingAssets` 而不是 `Resources`**：`Vrm10.LoadPathAsync` 接受真实文件路径，
`Application.streamingAssetsPath` 在 Windows 编辑器下就是真目录，`File.ReadAllBytes` 直接可用。
（注意：这只在桌面端成立；将来上 Android 要改走 `UnityWebRequest`——记进 ADR 的"未做"。）

**不做 `asmdef`**：VRM10 / UniGLTF / Newtonsoft 的 asmdef 都是 `autoReferenced`，
`Assembly-CSharp` 直接能用（已实测确认，见 §一 #3）。加 asmdef 反而要手抄一堆 GUID 引用。

### 2.3 协议契约（Unity 端必须自己实现的全部）

这些**已经是既有协议，M5 不改它们**，只是 Unity 端要照着实现：

```text
HTTP  GET  http://127.0.0.1:8000/v1/config     # 取 emotions / actions 词表（不硬编码）
WS         ws://127.0.0.1:8000/ws/v1/session/{ulid}   # session_id 客户端自己生成

客户端 → 服务端（JSON 文本帧）
  {"type":"session.voice_mode.set","version":1,"payload":{"enabled":true}}
  {"type":"input.user_text","version":1,"payload":{"text":"...","conversation_id":null}}

服务端 → 客户端（JSON 事件，6 字段信封 event_id/type/version/occurred_at/correlation_id/payload）
  turn.started / turn.completed / turn.failed / turn.interrupted
  assistant.text.completed {turn_id, reply, emotion{primary,intensity}, action{name,intensity}, voice{...}}
  avatar.expression.set    {turn_id, emotion, intensity}
  avatar.action.play       {turn_id, name, intensity, priority}
  tts.started              {turn_id, text, encoding, sample_rate, channels, chunk_count, synthesis_ms, duration_ms}
  tts.completed            {turn_id, status, chunk_count, synthesis_ms, duration_ms}
  error.protocol           {code, message}
  state.emotion.updated / state.relationship.updated   # M5 不消费，只打印

服务端 → 客户端（**二进制帧**）
  int16 PCM / 单声道 / 24000 Hz（`sample_rate` 以 tts.started 为准，**不要假设 16k**）
```

**分派规则（ADR 0021）**：二进制帧**不是事件**，没有信封，必须紧跟一条说明格式的 JSON 帧。
Unity 端用 `ClientWebSocket.ReceiveAsync` 返回的 `WebSocketMessageType.Text / Binary` 区分——
这正是选 `ClientWebSocket`（.NET Standard 2.1 自带）而不是引第三方 WS 库的原因：**零新增依赖**。

**实际 payload 与 `docs/07` §11 不一致，要顺手改文档**：
`avatar.expression.set` 的真实 payload 是 `{turn_id, emotion, intensity}`，
**没有 `duration_ms`**。过渡时长由 Unity 端自己定（本计划定 0.25 s 的插值）。

### 2.4 事件 → 行为

**情绪 8 → VRM 6 映射表**（写在 C# 里，不从 `/v1/config` 拿——词表是协议，
映射是 Unity 的实现细节，见 AGENTS.md「Unity decides animation implementation」）：

| 后端 emotion | VRM ExpressionKey | 说明 |
|---|---|---|
| `neutral` | `Neutral` | |
| `happy` | `Happy` | |
| `sad` | `Sad` | |
| `angry` | `Angry` | |
| `surprised` | `Surprised` | |
| `excited` | `Happy` | VRM 无 excited |
| `sleepy` | `Relaxed` | 近似（半闭眼） |
| `embarrassed` | `Relaxed` | **已知近似，不完美**；看到效果后可一行改掉 |

- 强度直接取 payload 的 `intensity`（0..1），用 0.25 s 插值过渡到目标值，
  而不是瞬间跳变（跳变看起来像鬼畜）。
- 切换表情时**先把上一个表情权重插值回 0**，避免多个表情叠加成一张怪脸。

**动作 11 → 程序化手势**（`Animator.GetBoneTransform` + LateUpdate 写 `localRotation`）：

| action | 程序化实现 | 用到的骨骼/表情 |
|---|---|---|
| `idle` | 不额外做（交给 idle 系统） | — |
| `nod` | 头 pitch 上下 2 次 | Head / Neck |
| `shake_head` | 头 yaw 左右 2 次 | Head / Neck |
| `wave` | 右臂抬起 + 前臂左右摆 3 次 | RightUpperArm / RightLowerArm |
| `think` | 头微低 + 视线偏左上 | Head + `LookUp`+`LookLeft` |
| `laugh` | 上身前倾小幅抖动 3 次 | Spine / Chest |
| `shy` | 头微低 + 视线偏开 | Head + `LookLeft` |
| `stretch` | 双臂上举 + 上身伸展 | 双 UpperArm + Spine |
| `yawn` | 头微仰 + 张嘴 | Head + `Oh` |
| `look_away` | 视线偏右 | `LookRight` |
| `look_at_user` | 视线回正 | 清掉 look 表情 |

- 每个手势是一个**协程**，时长 0.6–1.5 s，结束后把骨骼**恢复到基准姿态**
  （记录进入前的 localRotation，动画结束插值回去）。
- 高优先级动作（`priority != "normal"`，当前后端恒为 `normal`）可打断当前手势；
  否则新动作排队 1 个，超过就丢弃——**不做队列管理**。

### 2.5 口型（振幅包络，不做音素）

- 服务端是**整段批发**音频（ADR 0011 不做流式），所以客户端能拿到完整 PCM：
  收到 `tts.started` 后**累积二进制帧**，收到 `tts.completed` 时校验
  `chunk_count` 与实收帧数一致，然后一次性建 `AudioClip` 并播放。
- `AudioClip.Create(name, samples.Length, 1, sample_rate, false)` + `SetData(float[])`
  （int16 → `float` 除以 32768f）。
- 口型：**预计算 30 ms 窗口的 RMS 包络**，播放时按 `AudioSource.time` 查表，
  把包络值（归一化后）写到 `ExpressionKey.Aa`。这是 `docs/07` §9 说的
  "MVP 可以先做 volume based"。
- **不做 phoneme/viseme 时序**：那需要音素对齐，后端不提供。这是 M5 的已知限制，
  写进 README。
- 口型用的 key（`Aa`）与表情用的 key（`Happy` 等）、眨眼用的 key（`Blink`）**互不冲突**，
  可以同时设置，不需要仲裁。

### 2.6 Idle 系统（独立于 LLM，`docs/07` §10）

一个 MonoBehaviour，纯本地随机 + 正弦，**不产生任何网络请求**：

- **呼吸**：Spine/Chest 的轻微旋转正弦（≈0.25 Hz，幅度极小）。
- **眨眼**：随机 2–6 s 触发一次，闭合 → 张开共 ≈0.12 s，写 `Blink`。
- **视线漂移**：随机 3–8 s 在 `LookLeft/Right/Up/Down` 里挑一个，小权重 ≈0.3，持续 1 s。
- **身体摇摆**：Spine 的 yaw 正弦（≈0.1 Hz，幅度极小）。

> `docs/07` §10 的 idle 状态机还列了 `yawn` / `stretch`——它们已经作为**动作**实现了，
> idle 里**不重复**触发（避免"她没事就伸懒腰"）。记进 ADR 的"未做"。

**与动作/口型的共存**：idle 只写骨骼的**偏移量**，动作写的是**同一条骨骼的叠加偏移**，
两者相加后再写回。实现上统一由一个 `AstraPoseController` 收口：每帧
`finalLocalRotation = baseRotation * idleOffset * actionOffset * lookAtOffset`，
避免两个协程抢同一根骨骼（`docs/07` §8 明确要求分层，不要互相抢）。

### 2.7 音频与断线

- **播放**：`AudioSource` 挂在模型对象上（3D 音源），摄像机上。整段播完即结束。
- **`turn.interrupted`**：立刻 `AudioSource.Stop()` + 清空口型 + 中止当前手势
  （`docs/10` §7 Scenario D 要求）。注意被中断的 turn **不会**再发 `turn.completed`。
- **Unity 断开**：直接关连接即可，**不发送任何取消**——M1b 已定"断线不取消在跑的 turn"，
  backend 有自己的 orphan 处理。这条**不要为了 M5 改掉**。
- **重连**：连接意外关闭时每 2 s 重试一次（约 10 行）。这是开发期刚需
  （改一次脚本就要重进 Play），不算过度设计。**不做会话恢复**，重连就是新 `session_id`。

### 2.8 线程模型（**这是最容易翻车的地方**）

- Unity **没有给 `Task` 装 SynchronizationContext**，所以 `await` 一个 `Task` 之后
  续体会跑在线程池上，**碰任何 Unity API 都会炸**。因此：
  - **绝不 `await Vrm10.LoadPathAsync(...)`**。改用协程轮询：
    `var task = Vrm10.LoadPathAsync(path, ...); while (!task.IsCompleted) yield return null;
    var instance = task.Result;`（UniVRM 的 `RuntimeOnlyAwaitCaller` 内部会把活派回主线程，
    轮询只是保证**我们自己**回到主线程）。
  - WebSocket 的接收循环跑在线程池：收到帧后**只入 `ConcurrentQueue`**，
    在 `Update()` 里出队并处理（所有 Unity API 调用都在主线程）。

### 2.9 文档与 ADR 同步

| 位置 | 改什么 |
|---|---|
| `docs/07` §5 | "推荐 Unity 6 LTS" → 改成实际使用的 **Unity 2022.3 LTS + UniVRM 0.131.0**，并写明理由 |
| `docs/07` §11 | `avatar.expression.set` 的 payload 补上 `turn_id`、**去掉不存在的 `duration_ms`** |
| `docs/08` 阶段 5 | 补「实际落点」小节（与阶段 2/3/4 同格式） |
| `docs/decisions/0024-*.md` | **新增**，见 §七 |
| `README.md` | 新增 M5 段落 + 验收步骤 + 已知限制；"尚未接 Unity / 3D 形象（M5）"这句删掉 |
| `.trae/documents/HANDOFF_2.md` §6.2 | 标注已过时（avatar 事件其实在 M4 就已经发布了） |

### 2.10 测试策略

**Unity 侧没有自动化测试**（C# 不进 CI，也不值得为一个 dev 客户端搭 Unity Test Framework）。
真正守门的是后端：

```
tests/integration/test_chat_api.py     # 新增一条：ASTRA_AVATAR_ENABLED=true 时 /v1/config 如实反映
tests/contract/                        # 不动：M5 不改任何事件 payload
```

- 既有 `test_chat_api.py` 里 `assert body["avatar_enabled"] is False` **不会红**
  （默认值仍是 `False`）——所以是"新增用例"而不是"改断言"。
- 后端改动跑全量质量门（四项）。**Unity 的效果只能靠 §六 的真机验收**。

---

## 三、目标文件树

```
e:\DreamingPath\MyFriend\                      # 后端（改 4 处）
├─ src/astra/settings/base.py                  # 改：+avatar_enabled
├─ src/astra/api/routes/config.py              # 改：avatar_enabled 读真值
├─ .env.example                                # 改：ASTRA_AVATAR_ENABLED 移出预留区并取消注释
├─ tests/integration/test_chat_api.py          # 改：+1 条 config 用例
├─ docs/07_AUDIO_AND_AVATAR.md                 # 改：§5 Unity 版本 / §11 payload
├─ docs/08_IMPLEMENTATION_PLAN.md              # 改：阶段 5 实际落点
├─ docs/decisions/0024-m5-avatar-runtime.md    # 新
├─ README.md                                   # 改：M5 段落 + 已知限制
└─ .trae/documents/m5-avatar.md                # 本文件

E:\UnityProject\VirtualTuber\                  # 客户端（全新，不进 git）
├─ Assets/StreamingAssets/Celandine.vrm        # 新：从 Assets/Models/ 复制
└─ Assets/Astra/Scripts/
   ├─ AstraBootstrap.cs        # RuntimeInitializeOnLoadMethod：自建相机/灯光/UI/对象；装配；连 WS
   ├─ AstraClient.cs           # GET /v1/config 取词表 + ClientWebSocket + 主线程队列 + 重连
   ├─ AstraAvatar.cs           # Vrm10 加载 + 8→6 表情映射 + 0.25s 插值
   ├─ AstraPoseController.cs   # 骨骼分层收口（base × idle × action × lookAt）
   ├─ AstraActions.cs          # 11 动作 → 程序化手势协程
   ├─ AstraLipSync.cs          # 累积 PCM → AudioClip + RMS 包络驱动 Aa
   ├─ AstraIdle.cs             # 呼吸 / 眨眼 / 视线漂移 / 摇摆
   └─ AstraConfig.cs           # 常量：后端 URL、模型文件名、过渡时长
```

`AstraConfig.cs` 里是常量（`http://127.0.0.1:8000`、`Celandine.vrm`），改地址要重新编译。
**不引入 settings 文件**——当前只有一个后端地址，加配置文件是过度设计。

---

## 四、实施顺序

前置（Windows）：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

| # | 步骤 | 验证 |
|---|---|---|
| 1 | 后端：`settings/base.py` + `.env.example` + `config.py` | `uv run pytest tests/integration/test_chat_api.py -q` + `ruff` + `mypy src` |
| 2 | 后端全量质量门（四项） | `ruff check` / `ruff format --check` / `mypy src` / `pytest -q` |
| 3 | 建 `Assets/StreamingAssets/`，复制 `Celandine.vrm` | 文件存在 |
| 4 | `AstraConfig.cs` + `AstraBootstrap.cs`（只建相机/灯光/空模型位） | **用户第一次打开工程按 Play**：能看到一个空场景 + 控制台 "astra bootstrap ok" |
| 5 | `AstraAvatar.cs`（加载 VRM + 贴地 + 面向相机） | 按 Play：**Celandine 站在画面里**（第一个可见里程碑） |
| 6 | `AstraIdle.cs` + `AstraPoseController.cs` | 按 Play：呼吸 + 眨眼 + 轻微摇摆 |
| 7 | `AstraClient.cs`（config + WS + 主线程队列 + 重连） | 按 Play：控制台出现 ws 连接成功 / 收到 turn 事件 |
| 8 | IMGUI 输入框 + 事件日志 | 在 Unity 里打字发送 → 后端 `turn.completed` 回来 |
| 9 | `AstraAvatar` 接 `avatar.expression.set` | 发送"谢谢你，你太厉害了"→ 表情变成 happy |
| 10 | `AstraActions.cs` 接 `avatar.action.play` | 发送不同内容 → 分别看到点头 / 挥手 / 歪头等 |
| 11 | `AstraLipSync.cs` + voice mode | 开始 voice mode → 听到声音 + 嘴在动 |
| 12 | 打断处理（`turn.interrupted` → 停音频 + 停动作） | 浏览器 `/dev` 打断时 Unity 立刻静音 |
| 13 | 文档 + ADR 0024 + README | 人工检查 |
| 14 | 真机端到端验收 | 见 §六 |

**第 4 步之后每一步用户都能看到进展**，不要憋到最后才让用户开编辑器。

---

## 五、依赖变更

- **后端：无新增依赖**（只加一个布尔字段）。
- **Unity：无新增包**。UniVRM 0.131.0、Newtonsoft Json 3.2.2 已在
  `Packages/manifest.json`/`Packages/` 里；WebSocket 用 .NET 自带的
  `System.Net.WebSockets.ClientWebSocket`。**不引 NativeWebSocket / websocket-sharp**。

---

## 六、验收标准与命令

### 自动化（后端）

```powershell
uv run pytest -q                          # 1057 + 新增，全绿
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

### 真机端到端（**用户亲手做，共 7 步**）

```powershell
# ── 准备（终端 1）──────────────────────────────────
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
$env:ASTRA_LLM_MODEL="nvidia/nemotron-3-nano-4b"
$env:ASTRA_AVATAR_ENABLED="true"
uv run astra serve                        # → http://127.0.0.1:8000
```

开发期（不需要口型）把 LLM 放回 GPU 会快很多——CosyVoice 已关，GPU 空着：

```powershell
lms load nvidia/nemotron-3-nano-4b --gpu max
```

做**第 6 步（口型）**时才需要语音，届时按 8GB 的硬约束把 LLM 挪回 CPU 并开 CosyVoice：

```powershell
lms load nvidia/nemotron-3-nano-4b --gpu off --context-length 16384
$env:ASTRA_STT_PROVIDER="cosyvoice"; $env:ASTRA_TTS_PROVIDER="cosyvoice"
$env:ASTRA_TTS_REFERENCE_AUDIO="E:\Voice\cosyvoice3-rainfall-v2\cosyvoice-rainfall-v2\cosyvoice-rainfall\assets\zero_shot_prompt.wav"
# 重启 astra serve
```

```text
1. 打开 Unity Hub → 打开 E:\UnityProject\VirtualTuber（首次会导入包，等它跑完）
   → 打开 Assets/Scenes/SampleScene.unity

2. 按 Play。
   ✅ 应该看到：Celandine 站在画面中央、面向相机、在呼吸和眨眼
   ✅ Console 里应该看到： "astra: loading vrm ..." / "astra: ws connected"

3. 在左上角输入框里输入「谢谢你，你真的太厉害了，我很喜欢你。」→ 回车
   ✅ 应该看到：她的表情变成开心
   ✅ 应该看到：播一个动作（点头/开心地晃一下）
   ✅ 事件日志里出现 assistant.text.completed / avatar.expression.set / avatar.action.play

4. 输入「你真是个小天才呢」→ 看表情是否切换（状态机在动）
   输入「今天好累啊」→ 看是否变成 sad / 播一个不同的动作

5. 验证「Unity 断开不影响 backend」：
   停止 Play（或直接关掉 Unity）→ 在浏览器打开 http://127.0.0.1:8000/dev
   → 还能正常对话、/health 一切正常

6. 口型（需要 CosyVoice 在跑 + LLM 已挪回 CPU，见上面的准备）：
   重新 Play → Unity 自动进 voice mode
   → 从 Unity 输入框发一句话
   ✅ 应该看到：听到她的声音，并且嘴跟着声音开合
   ✅ 说完后嘴闭回去

7. 打断：
   保持 /dev 在浏览器里开着并进 voice mode → 让 Unity 说一句话，
   → 在她说话中途从 /dev 按住说话
   ✅ 应该看到：Unity 端声音**立刻停**，动作也停，嘴闭回去
```

### 记录（`docs/10` §6 要求）

记录并写进 ADR：VRM 加载耗时、WS 连接耗时、`tts.started → 首个可见口型` 的延迟手感。

---

## 七、新增 ADR

| 编号 | 结论 |
|---|---|
| **0024** | **M5 客户端运行时**：(1) 形象事件由**后端发布**（M4 已实现，此处正式确认），与 `docs/04` §2 一致；(2) `avatar_enabled` 是**纯通告**，不门控任何事件——理由是 `docs/04` §2 的序列本身无条件列出 avatar 事件，且开发者控制台/M1 验收依赖看到它们；(3) Unity 用 **2022.3.48f1c1 + UniVRM 0.131.0**，不是 `docs/07` §5 推荐的 Unity 6（实际环境约束，改文档）；(4) 动作走**程序化骨骼手势**，因为工程内无任何动画资源，也不引入美术管线；(5) 情绪 8→VRM 6 映射表与"近似是已知的"（`embarrassed`→`relaxed`）；(6) 口型是**振幅包络驱动 `Aa`**，不做 phoneme/viseme 时序；(7) 客户端用 .NET 自带 `ClientWebSocket`，**零新增依赖**；(8) 加载用**协程轮询**而非 `await`（Unity 没有 Task 的 SynchronizationContext）；(9) 客户端源码**不进版本控制**（用户在 Unity 工程内开发，工程不是 git 仓库）——这是**风险**，记录在案；(10) 未做：Unity 内麦克风采集、idle 的 yawn/stretch、`avatar.look_at.set` / `avatar.lipsync.*` 事件（不登记也不发布） |

---

## 八、风险点

1. **`await` Task 会掉到线程池 → 碰 Unity API 必炸。** 这是 M5 最可能让人查半天的问题。
   缓解：加载用协程轮询 `task.IsCompleted`；WS 接收只入 `ConcurrentQueue`，`Update()` 里出队。
   写法在 §2.8 固定下来，**不要图省事写 `await`**。
2. **没有动画资源**，动作全靠程序化骨骼旋转。观感会**明显朴素**（不是"美术级"动作），
   而且不同 VRM 的骨骼比例不同，幅度要现场调。
   缓解：幅度参数集中在 `AstraConfig.cs`，看到效果后一次性调；并在 ADR 里诚实写明这是取舍。
3. **表情映射有损**：`embarrassed` / `excited` / `sleepy` 在 VRM 1.0 里没有对应，
   只能近似。`embarrassed`→`relaxed` 很可能"看起来不像害羞"。
   缓解：映射表只有 8 行，看到效果后逐条调；ADR 记录这个已知缺口。
4. **首次打开 Unity 会导入 UniVRM 包并生成 `Library/`**，可能几分钟且看着像卡住。
   缓解：计划里明确告知用户"第一次要等"。
5. **`RuntimeInitializeOnLoadMethod` 会在任意场景自动建对象**——如果用户以后在这个工程里
   放别的东西，它会一起跑。当前工程是专用壳，可接受；ADR 记录这个副作用。
6. **IMGUI 默认字体可能不显示中文**（会变成方块），而回复正文是中文。
   缓解：用 `Font.CreateDynamicFontFromOSFont("Microsoft YaHei", 16)` 建动态字体
   （Windows 自带，纯几行）。**输入框的可用性不依赖字体**——即使字是方块也能打字发送。
7. **音频总线是全局广播**（`docs/04` / ADR 0021 的既有局限）：Unity 与浏览器 `/dev`
   同时在线时**两边都会收到音频**。单用户本地场景可接受，但验收第 7 步（打断）会看到
   这个现象，属预期。
8. **客户端源码没有版本控制**（Unity 工程不是 git 仓库，用户已拍板）。
   误删即永久丢失。缓解：计划里建议——若愿意，我可以在 Unity 工程根目录 `git init`
   并加一份 Unity 的 `.gitignore`（**需要用户点头，不擅自做**）。
9. **口型与表情可能"抢脸"**：说话时 `Aa` 与 `Happy` 同时设权重，叠加效果未经验证。
   缓解：分层收口在 `AstraPoseController`，口型只写 viseme key、表情只写 emotion key，
   互不覆盖；看到效果再调权重上限。
10. **新增 Settings 字段必须同步 `.env.example`**（M0/M1/M2 都踩过），否则
    `Copy-Item .env.example .env` 后 `extra="forbid"` 会让启动直接失败。
11. **`docs/07` §11 的 payload 与实现不一致**（多了 `turn_id`、少了 `duration_ms`）——
    必须顺手改文档，否则下一个接手的人会按错的结构写客户端。
12. **HANDOFF §6.2 是过时信息**——它说 avatar 事件没人发布，实际 M4 就发了。
    改掉它，否则下一个 AI 会去做一件已经做完的事。

---

## 九、待用户确认的点

1. **开发期是否接受"先在 Unity 里看到静态模型"再逐步加功能**（§四 的第 4–6 步会分几次
   让用户开编辑器按 Play）。如果希望一次性交付再验收，我也可以憋到最后——
   但那样一旦哪步不对，排查面会大很多。
2. **要不要顺手给 Unity 工程 `git init`**（加一份标准 Unity `.gitignore`）。
   你选了"源码不进仓库"，那客户端代码目前**没有任何版本历史**；初始化一个独立仓库
   是最便宜的保险。要我做吗？
3. **动作幅度与观感**必然要现场调。计划里把可调参数集中到了 `AstraConfig.cs`，
   但"点头多大幅度算自然"只能你看过之后告诉我。
4. **`embarrassed`→`relaxed` 这个近似我不保证好看**（§二 §2.4）。
   如果你更在意"害羞"的表现，可以在 VRM 里补一个自定义表情（需要美术），
   或者接受它暂时看起来像"平静"。
