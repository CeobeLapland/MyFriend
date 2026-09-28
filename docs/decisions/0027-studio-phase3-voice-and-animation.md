# ADR 0027 — Studio 阶段 3：语音的归属与动画配置的托管边界

> 本文落实两件 ADR 0025 已定方向、但在实施前暴露出具体机制问题的事：
> ① 语音对话要在 Studio 内实现（ADR 0025 §4）；② 动画映射改为后端托管（ADR 0025 §3）。
> 配套实施计划：`.trae/documents/studio-phase3.md`。

## Decision

### 1. 语音：**协议一行不改**，照 `/dev` 已工作的链路移植进 Studio

- 复用既有协议帧：`session.voice_mode.set` / `input.audio.started` / `input.audio.stopped`
  / 二进制 PCM 帧；播放侧照 `tts.started.sample_rate` 建 AudioContext、逐块排队、
  `turn.interrupted` 立即停播（ADR 0021 的既有语义）。
- 采集仍是 16 kHz 单声道 int16 + push-to-talk（服务端**不做重采样**，格式不符会被拒——
  `ASTRA_AUDIO_INPUT_SAMPLE_RATE` 与客户端声明必须一致，这条 ADR 0022 已钉住）。
- **STT / TTS 未配置时如实禁用**：`/health` 的 `stt`/`tts` 是 `not_configured` 就把
  「按住说话」禁用并说明去哪儿配（设置页 → 改 `.env` → 重启）；不假装按钮能用。
- Studio **不做** Unity 侧的麦克风采集（M5 的边界未变），也不改"Unity 与浏览器同时在线时
  两边都会收到音频"这条既有事实。

### 2. 动画配置：后端持有权威副本（`data/astra_animation.json`），Unity 拉取 + 本地缓存兜底

**存储**：`data/astra_animation.json`（用户数据目录，与 `studio-state.json` 同层）。
不放仓库（它描述的是本机的 Unity 工程）、不进数据库（它是人要能看/能备份/能手改的 JSON）。

**端点**（进 OpenAPI，无条件挂载，与 ADR 0026 §2 同一套边界）：

| 端点 | 作用 |
|---|---|
| `GET /v1/avatar/animation` | 返回配置 JSON；后端还没有副本时 **404**（不是空配置——那会覆盖 Unity 的映射） |
| `PUT /v1/avatar/animation` | 校验后落盘；Studio 的编辑器与 Unity 的首次播种都用它 |

**后端只校验、不重写**：落盘的就是 PUT 进来的那份文本（保留未知字段），
这样手改文件、将来加字段都不会被后端吃掉。校验范围：

- 结构与 C# 的 `Normalize` 对齐（`idlePool` / `actions` / `poses`，空 clip 名视为非法）；
- `actions[].action` 必须落在**协议词表**里（`/v1/config` 的 `actions`，唯一真相在
  `src/astra/domain/vocabulary.py`）——Studio 不该写进一个后端永远不会发的动作名；
- `poses[].pose` 限定 `sit` / `lie`（与 Unity 的 `PoseNames` 对齐；将来加姿态两侧一起改）；
- `weight > 0`、`clip` 非空。

**Unity 侧改动的边界（只改"配置来源"，不动动画表现）**：

1. 启动/**每次连上后端**时 `GET /v1/avatar/animation`：拿到就应用到内存 + 覆盖本地缓存文件；
2. **404（后端还没有副本）**：把本地文件 `PUT` 上去一次（首次播种）；
3. **连不上后端**：继续用本地缓存文件（ADR 0025 §3 的要求：不能变成"打不开后端就没有动画"）；
4. Unity 面板保存时**顺手 `PUT` 一次**（尽力而为）——否则面板里改完的东西会在下次连上时
   被后端的旧版本覆盖，属于"静默丢改动"。PUT 失败时日志要如实写清这一点。

**改 Unity 前先备份**：那个工程**不是 git 仓库**（ADR 0024 §9），所以
`astra_animation.json` 与要改的 `.cs` 都先复制一份 `.bak`。

### 3. Studio 的动画页：三块结构化编辑 + 导入/导出

待机池（clip + 权重 + 循环）/ 11 个动作名各自的候选 clip / 坐躺姿态三件套。
页面读到的是后端那份 JSON，编辑后整体 PUT 回去（`JSON.parse → 改 → JSON.stringify`
能原样保留它不认识的字段）。**clip 名目前要手填**——clip 清单在 Unity 侧
（`AstraClipLibrary` 从模型里枚举），后端不知道；"Unity 上报 clip 清单"记为可选后续（P1）。

## Reason

- **为什么语音照抄 `/dev` 而不是重写**：那条链路已经在 M4 真机验收过（16 kHz 采集、
  二进制帧、按 sample_rate 播放、打断停播），重写只会引入新的音频坑；
  而且协议不变意味着 Unity 端**一行都不用改**。
- **为什么动画要"后端权威 + 本地缓存兜底"而不是"Studio 直接写 Unity 工程的文件"**：
  那需要把机器专属的工程路径写进配置（studio-app.md §7.3 方案 b 已否），
  而且 Unity 工程不在 git 里，写坏了没有历史可回退。
- **为什么后端只校验不重写**：这份 JSON 现在是用户会手改的东西（Unity 面板 + 编辑器），
  后端做"结构化重写"就会吃掉未知字段与手写注释式的排版，等于把单一真相变成"后端的模型"。
- **为什么面板保存要顺手上报**：托管之后"双写"是真实存在的（Unity 面板 + Studio），
  不处理就是静默丢改动——这与本项目"如实、可解释"的风格冲突。

## Consequences

- `data/astra_animation.json` 成为权威副本后，**Unity 工程里的那份文件降级为缓存**；
  README 与 Unity 工程的 `ANIMATION.md` 都要写明这一点。
- Unity 多了一处对后端的运行时依赖，但**有回退路径**（本地缓存），符合 ADR 0025 §3。
- Studio 首次打开动画页时可能是空的（后端还没副本）——页面要如实说明
  "跑一次 Unity 就会把当前映射上报上来"，并给一个"粘贴 JSON 导入"的入口。
- 语音让 Studio 与 `/dev` 都能说话；**两者同时开着时音频会两边都播**（既有广播语义，
  与 Unity 同时在线时同样的问题），属于已知行为，不在本阶段改。
- 阶段 3 仍是**零新增依赖**（Web Audio API 是浏览器自带的；后端只用标准库）。
- 桌面形象（Unity）与界面（浏览器）仍是两个窗口；桌面壳（阶段 4）不在本轮承诺内。