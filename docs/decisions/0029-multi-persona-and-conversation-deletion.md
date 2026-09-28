# ADR 0029 — 会话删除与多人格（一位人格 = 一个角色）

> 本轮两件事：① 历史栏能删掉某一段会话（**真删**、二次确认）；② 人格可以有多份，
> 能在 Studio 里切换 / 新建 / 删除，并在界面上编辑提示词相关的全部字段
> （identity / traits / communication / behavior.rules / prompt.hard_constraints / errors）。

## Decision

### 1. 会话删除：`DELETE /v1/conversations/{id}`，真删消息，不碰记忆与状态

- 仓储端口新增 `ConversationRepository.delete(conversation_id) -> bool`：
  **同一事务**里删 `messages` 子表与该会话行。`messages` 与 `conversations` 之间
  **没有外键**（`0001_init.sql`），所以必须显式删——不能指望级联。
- `ConversationService.delete` 先做归属检查：不存在 / 不属于当前
  `(user_id, companion_id)` → **404**（`conversation_not_found`），不静默成功——
  与 `load_history`（ADR 0023 时代的既有约定）一致。
- **允许删 active 会话**：下一轮 `resolve_or_create` 找不到 active 会新建一段，
  用户不需要先"结束"再删。
- **不删**从这段会话里抽取出的记忆，也不动情绪 / 关系状态：
  - 记忆有自己的生命周期与删除入口（`DELETE /v1/memories/{id}`，ADR 0019）。
    按会话级联删会把"记住了关于你的事"和"那次聊天的原文"混为一谈；
  - `memories.source_message_id` 会悬空——它是 provenance 提示，不是外键，读取端不依赖。
- Studio：历史栏每项一个「删」+ `window.confirm` 二次确认（带摘要与"不可恢复"）。
  删掉的若正是当前会话 → 清空对话区与 `localStorage`，提示"发消息会开新的一段"。
  **当前会话正在跑 turn 时该按钮禁用**——否则那一轮落库会把消息写进已删的会话，
  变成不可见的孤儿行。

### 2. 多人格：id = 文件名 stem = `companion_id`，存储 = 人格目录下一个 `<id>.toml`

- **目录约定**：人格目录 = `ASTRA_PERSONA_PATH` 的**父目录**（默认 `./personas/`）。
  不新增配置键——"多个人格 = 同一目录下的多个 `.toml`" 就是约定，Studio 上会显示这个目录。
- **id 规则**：`^[a-z][a-z0-9_-]{0,31}$`。id 是身份的一部分，它同时就是
  `companion_id`——历史、记忆、情绪 / 关系状态**全部按 `companion_id` 归档**
  （ADR 0012 的既有设计）。所以"换人格"天然就是"换一套历史与记忆"，
  不需要任何额外的隔离机制。
- **激活 = 写 `.env` 的两个键**（`ASTRA_COMPANION_ID=<id>`、
  `ASTRA_PERSONA_PATH=<dir>/<id>.toml`），**重启后生效**。
- **编辑 = 重写该 `.toml`**（校验用与加载同一套 Pydantic 模型）。**重启后生效**——
  人格在装配期加载（ADR 0015）。
- **删除 = 只删文件**：
  - 拒绝删除"正在使用 / 已被 `.env` 引用"的那一位（409 `persona_active`），提示先切换；
  - 拒绝删除最后一位（409 `persona_last`）——否则下次启动没有身份，`PersonaLoadError`
    快速失败（ADR 0015 的既定行为）；
  - **不删**该 companion 的会话 / 记忆 / 状态：数据库里的东西只有用户显式删
    （记忆页 / 历史栏）才会消失。用同名 id 建回一位人格就能继续用——这是
    "不静默删除用户数据"在人格这种"配置"上的落点。
- **新建不自动激活**：写好人格后由用户显式切换。

### 3. API：`/v1/admin/personas`（无条件挂载，与其它 admin 面一致，ADR 0026 §2）

```text
GET    /v1/admin/personas               # 列表 + 目录 + 运行中 / 待启用 id
GET    /v1/admin/personas/{id}          # 完整文档（与 TOML 同构的 JSON）
POST   /v1/admin/personas               # 新建（id 在 body 里）→ 201
PUT    /v1/admin/personas/{id}          # 覆盖保存
DELETE /v1/admin/personas/{id}          # 删文件 → 204
POST   /v1/admin/personas/{id}/activate # 写 .env → {restart_required, supervised}
```

- 稳定错误码：`invalid_persona`（400，消息带字段路径）、`persona_exists`、
  `persona_not_found`、`persona_active`、`persona_last`。
- **写前备份** `<id>.toml.bak`（与 `.env.bak` 同一习惯）；保存**重写整份文件、
  注释不保留**——UI 上明说（见 Reason）。
- `activate` 写 `.env` 复用 admin_config 的落盘路径（改值 → `check_env_file` → 备份 →
  `os.replace`）：把 `admin_config._apply` 提取成 `dotenv_store` 的公共函数，两个路由共用。
- 列表里区分 **running**（当前进程装配时用的）与 **pending**（`.env` 里写的）：
  刚切换但还没重启时两者不同，UI 显示"重启后启用"，不假装已生效（ADR 0026 §5 的同一精神）。

### 4. Studio：新增「人格」页（第 7 个页签）

- 左栏：人格列表（显示名 + id + 使用中 / 待启用徽标），操作「新建 / 切换 / 删除」；
- 右侧编辑器，覆盖人格文件的全部字段：
  identity（name / display_name / pronouns / version / self_description / backstory）、
  traits（键 + 0..1 数值）、communication（五个字段）、behavior.rules 与
  prompt.hard_constraints（多行文本，每行一条）、errors.llm_unavailable；
- 切换走「写 `.env` → 重启」：复用设置页的 `restartBackend()` 与重启遮罩；
  未监督时如实提示手动重启。
- 渲染仍然只用 `textContent` / `createElement`（模型与用户内容都不可信）；
  页签与端点同步进 `tests/integration/test_studio_console.py` 的守门清单。

## Reason

- **为什么切换要重启、不做运行时热切换**：`companion_id` 在装配期烧进了五个位置
  （会话解析 / 记忆检索与写入 / 状态读写 / 提示词与降级话术 / `/v1/config`），
  而 WS 连接会**在握手时**解析并持有 `Orchestrator`（`api/dependencies.py` 的依赖在
  `session_socket` 参数上求值）。要热切换就得给四个服务加一层"当前角色"的间接引用、
  并让 WS 每帧重解析——两秒的重启换到的是"所有子系统在同一时刻、以同一种方式换人"，
  热切换的复杂度与"半切换"风险不成比例。切换人格本来就是"换一个人来跟你说话"，
  重启是诚实的语义。
- **为什么 id 即 `companion_id`**：历史 / 记忆 / 状态的隔离已经按 `companion_id` 做了；
  再立一套 persona id ↔ companion id 的映射只会多一个会漂的真相。
- **为什么编辑不保留注释**：保留注释需要"结构化编辑 TOML 文本"的额外机制
  （stdlib 没有 TOML writer，且不引新依赖）；人格文件里的注释是给仓库维护者看的
  （`docs/05` 有等价说明），UI 编辑面向用户改值。保存前有 `.bak`，UI 里明说这件事。
- **为什么删人格不级联删数据**：级联删是"静默删除用户数据"的反面教材。
  要删历史与记忆，用户手上有历史栏与记忆页两个显式入口。

## Consequences

- 切换 / 编辑人格后**需要重启才生效**；Studio 上显示"待启用"。未监督运行时切换被拒（409），
  提示手动重启——不静默假装成功。
- 第一次在 UI 里保存 `personas/astra.toml` 后，文件里的注释会消失、格式变成统一输出；
  原文件留 `<id>.toml.bak`。
- 会话删除是真删、没有撤销。防线是二次确认（确认框里带该段会话的摘要）。
- 已连接客户端（Unity / `/dev`）不需要任何改动：切换后 `companion_id` 与历史 / 记忆
  的作用域一起变，协议一行未改。
- 文档更新：`docs/04_API_AND_EVENTS.md` 补 `DELETE /v1/conversations/{id}`；
  `docs/05_PERSONA_AND_PROMPTS.md` 补"多个人格"一节；README 的人格一节指向 Studio 人格页。