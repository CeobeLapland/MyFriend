# 会话删除与多人格 — 实施计划

## Context

**阶段 3 与 ADR 0028 的修正都已交付**（语音预览 / 历史栏 / 记忆门槛；1192 tests、
ruff、format、mypy 全绿）。本轮做用户新提的两件事：

| 目标（用户视角） | 对应 |
|---|---|
| 历史栏里能删掉某一段会话，别只增不减 | 本计划 §2.1 |
| 人格可以有多份、能切换 / 增删；提示词相关字段在 Studio 里可编辑 | 本计划 §2.2–§2.4 |

配套 ADR：[`docs/decisions/0029-multi-persona-and-conversation-deletion.md`](../../docs/decisions/0029-multi-persona-and-conversation-deletion.md)。
**本文件是执行依据。** 先做会话删除（小、独立），再做人格子项目（大）。

---

## 一、本轮实测（写计划前在代码里核对过的事实）

| # | 事实 | 影响 |
|---|---|---|
| 1 | `messages.conversation_id` **没有外键**（`0001_init.sql` L26–34）；`connection.py` 的 `foreign_keys=ON` 只对 0003 的 embedding 表起作用 | 删会话必须**显式删 messages**，不能指望级联 |
| 2 | WS 路由的 `OrchestratorDep` 在**握手时**解析并持有（`chat_ws.py` L146–155） | 运行时替换 `app.state.orchestrator` 对已连接客户端不生效 → 切换人格用**重启**（ADR 0029 §2） |
| 3 | `companion_id` 的归档点：conversations（0001）、memories（0001/0003）、emotion_states + relationship_states（0004），各仓储一律按它分区 | "id 即 companion_id"后，换人格天然换历史 / 记忆 / 状态 |
| 4 | `companion_id` 在构造期烧进 4 个服务：`ConversationService.__init__`、`MemoryConfig`、`StateConfig`、`TurnConfig`（约 20 处引用）+ `app.py` 装配 5 处 + `routes/config.py` 1 处 | 热切换要改四层构造契约 + 全部测试 fixture → 不做（ADR 0029 Reason） |
| 5 | Studio 守门测试 `test_studio_console.py`：`data-page` 断言（L108）、endpoints 清单（L123）、`node --check`（L158）、禁用 `innerHTML`（L90） | 新页签 / 新端点必须同步更新它，否则红 |
| 6 | 记忆页删除用 `window.confirm`（`studio.html` L1482） | 会话删除沿用同一模式 |
| 7 | `.conv-item` 是 `<button>`（L1311 附近）；CSS 在 L113–126 | 删除按钮不能嵌在 button 里 → 该项改成 wrapper + 主按钮 + 删按钮 |
| 8 | Studio 用 `app.turnBubbles`（Map）跟踪在跑的 turn：`turn.started` 时 set，completed / failed / interrupted 时 delete（L548/827/859/871/890） | `size > 0` 即"有 turn 在跑"，可作为"当前会话禁止删除"的依据 |
| 9 | `persona_path` 默认 `./personas/astra.toml`；测试 fixture 11 处显式传它 | **不新增设置键、不改 `persona_path` 语义**；人格目录 = 它的父目录 |
| 10 | `admin_config._apply`（L265）已实现"改值 → `check_env_file` 校验 → 备份 → `os.replace`"，`.env` 写路径唯一 | 提取为 `dotenv_store.write_values()`，`activate` 复用它 |
| 11 | admin 面（config / restart / lmstudio）无条件挂载（ADR 0026 §2）；`studio.html` 页签：对话 / 记忆 / 设置 / 模型 / 事件 / 动画 | `/v1/admin/personas` 同样无条件挂载；人格页是第 7 个页签 |
| 12 | `toml_loader` 的校验模型是私有的（`_PersonaFile` 等），`tomllib` 只读 | 把模型提为公共 `PersonaDocument` 供 store / API 复用；新写 `toml_writer.py`（手写固定 schema 的序列化，不引依赖） |
| 13 | 测试基线 **1192**；四项质量门：`uv run pytest -q`、`ruff check src tests`、`ruff format --check src tests`、`mypy src` | 每步跑；最后对一遍基线增量 |

---

## 二、技术设计

### 2.1 会话删除（先做）

**后端**

- `ConversationRepository`（`domain/ports.py`）新增：
  `async def delete(conversation_id: str) -> bool` —— 真删该会话与其全部消息。
- `SQLiteConversationRepository._delete_sync`：`BEGIN` → `DELETE FROM messages WHERE
  conversation_id = ?` → `DELETE FROM conversations WHERE id = ?` → `COMMIT`；
  失败 `ROLLBACK` 重抛（照 `_append_messages_sync` 的既有模式）。返回会话行是否被删。
- `ConversationService.delete(conversation_id) -> bool`：先 `repository.get()` 做归属
  检查（None / 非当前 `(user_id, companion_id)` → False）；不检查 status（active 也允许删）。
- 路由：`DELETE /v1/conversations/{conversation_id}` → **204**；False → **404**
  （`conversation_not_found`，与 `load_history` 同一文案风格）。

**Studio（对话页历史栏）**

- `renderConversations()` 里每一项改成：

```text
<div class="conv-row">            ← 相对定位容器
  <button class="conv-item">…</button>   ← 主按钮（打开会话）
  <button class="conv-del" title="删除这段会话">删</button>
</div>
```

- 点击「删」：`window.confirm("删除这段会话？\n\n" + 摘要 + "\n\n真实删除，不可恢复。")`
  → `DELETE` → 刷新列表（`loadConversations()`）；
  - 删的是**当前**会话：清 `app.conversationId` / `localStorage` / 聊天区，提示
    "已删除。发消息会开新的一段。"
  - **当前会话且有 turn 在跑**（`app.turnBubbles.size > 0`）：按钮禁用，
    title 提示"等这一轮说完"（防孤儿消息写入，见 ADR 0029 §1）。

### 2.2 人格存储与校验（persona 子项目）

- `infrastructure/persona/toml_loader.py`：私有模型提为公共
  （`PersonaDocument` / `IdentityDocument` / `CommunicationDocument` / `BehaviorDocument` /
  `PromptDocument` / `ErrorsDocument`），新增 `validate_document(mapping) -> PersonaDocument`
  作为唯一校验入口；`load_persona` 行为不变（仍抛 `PersonaLoadError`）。
- 新 `infrastructure/persona/toml_writer.py`：`render_persona(document) -> str`
  ——固定 schema 的手写序列化（输出稳定、带一段标准头注释；字符串转义 `\` `"` 与换行，
  用三引号渲染多行字段）。**不保证保留原注释**（ADR 0029 Reason）。
- 新 `infrastructure/persona/store.py`：`PersonaStore(dir)`（同步实现，路由侧 `to_thread`）：
  - `list_ids()`（扫 `*.toml`，按 id 排序）、`path_for(id)`、`exists(id)`、
    `read(id) -> PersonaDocument`（走 `load_persona` 的同一校验）、
    `write(id, document)`（写前备份 `<id>.toml.bak`）、`create(id, document)`
    （已存在 → 冲突）、`delete(id)`（删文件；无文件 → False）；
  - id 校验 `^[a-z][a-z0-9_-]{0,31}$`（非法 → `ValueError`，路由转 400）。
- 校验错误消息：pydantic `ValidationError` 转成"字段路径 + 原因"的一行（不吐整块 JSON）。

### 2.3 `/v1/admin/personas`（新路由文件 `admin_personas.py`）

```text
GET    /v1/admin/personas               # {dir, running_id, pending_id, personas:[…]}
GET    /v1/admin/personas/{id}          # 文档 JSON（与 TOML 同构）
POST   /v1/admin/personas               # {id, persona:{…}} → 201
PUT    /v1/admin/personas/{id}          # {persona:{…}} → 200
DELETE /v1/admin/personas/{id}          # → 204
POST   /v1/admin/personas/{id}/activate # 写 .env 两个键 → {restart_required, supervised}
```

- `running_id`：`settings.persona_path` 的 stem（被删掉了就 `null`）；
  `pending_id`：`.env` 里 `ASTRA_PERSONA_PATH` 的 stem；两者不同 → UI 显示"重启后启用"。
- 错误码：`invalid_persona`(400) / `invalid_id`(400) / `persona_exists`(409) /
  `persona_not_found`(404) / `persona_active`(409：使用中或已被 .env 引用) /
  `persona_last`(409)。
- `activate`：先 `read(id)` 确认文件存在且合法 → `dotenv_store.write_values(...)` 写
  `ASTRA_COMPANION_ID` 与 `ASTRA_PERSONA_PATH`（相对路径渲染成 `./personas/<id>.toml` 风格）
  → 返回 `{restart_required: true, supervised}`；未监督时前端提示手动重启。
- `dotenv_store.write_values(env_file, example_file, values)`：从
  `admin_config._apply` 原样提取（含 `check_env_file` 与 `.bak`），admin_config 改调用它
  ——既有集成测试守着这次重构不改变行为。
- 路由注册：`app.py` 里 `include_router(admin_personas_router)`，与其它 admin 并列。

### 2.4 Studio 人格页（第 7 个页签）

- nav 加 `<button data-page="persona">人格</button>`；页面结构：

```text
左栏（240px）：人格列表（显示名 + id + 徽标：使用中 / 重启后启用 / —）
               底部按钮：新建 / 切换为这一位 / 删除
右侧：编辑器（选中某一位后加载 /v1/admin/personas/{id}）
  身份：name / display_name / pronouns / version / self_description / backstory
  性格：traits 行（名称 + 0..1 数字 + 删行）、+ 添加
  说话风格：language / sentence_length / emoji_frequency / rhetorical_questions /
            preferred_interaction
  行为规则：多行文本（每行一条）
  硬约束：多行文本（每行一条）
  错误话术：errors.llm_unavailable
  底部：保存（重写整份 TOML、注释不保留、重启后生效——文案在页面上）
        "保存并重启" 复用 restartBackend()；状态带显示人格目录与 .env 路径
```

- 「新建」：先弹一个 id + 显示名的小表单（`window.prompt` 两问，或页内联行——
  实现时取页内联行，避免 prompt 的样式不可控），以一份最小模板 POST 创建，然后自动选中。
- 「切换」：POST activate → 若 `supervised`：确认框"已写入 .env，现在重启？" →
  `restartBackend()`；否则横幅提示手动重启命令。
- 「删除」：`window.confirm`（说明：**只删人格文件；该位的对话与记忆仍在数据库里**）→
  DELETE → 刷新列表。
- 守门：全部 `textContent` / `createElement`；`node --check` 必须过。

---

## 三、改动清单

| 文件 | 动作 |
|---|---|
| `src/astra/domain/ports.py` | `ConversationRepository` 加 `delete` |
| `src/astra/infrastructure/db/conversation_repository.py` | `delete` + `_delete_sync` |
| `src/astra/application/conversation_service.py` | `delete`（归属检查） |
| `src/astra/api/routes/conversations.py` | `DELETE /{id}` 端点 |
| `src/astra/api/static/studio.html` | ① 历史栏删除；② nav + 人格页 + 编辑器 |
| `src/astra/infrastructure/persona/toml_loader.py` | 模型提公共 + `validate_document` |
| `src/astra/infrastructure/persona/toml_writer.py` | **新增**（序列化） |
| `src/astra/infrastructure/persona/store.py` | **新增**（目录 / 读写 / 备份 / id 校验） |
| `src/astra/infrastructure/dotenv_store.py` | 加 `write_values`（从 admin_config 提取） |
| `src/astra/api/routes/admin_config.py` | 改调 `dotenv_store.write_values` |
| `src/astra/api/routes/admin_personas.py` | **新增**路由 |
| `src/astra/api/app.py` | 注册 personas 路由 |
| `.env.example` | `ASTRA_PERSONA_PATH` 注释说明多人格约定 |
| `docs/04_API_AND_EVENTS.md` | 补 `DELETE /v1/conversations/{id}` 与 personas 管理端点 |
| `docs/05_PERSONA_AND_PROMPTS.md` | 补"多个人格"一节 |
| `README.md` | 人格一节指向 Studio 人格页 |
| `tests/unit/test_conversation_repository.py` | + delete 用例 |
| `tests/unit/test_conversation_service.py` | + delete 归属用例 |
| `tests/integration/test_conversations_api.py`（若无则并入 chat_api） | + DELETE 204/404 |
| `tests/unit/test_persona_writer.py` | **新增** round-trip / 转义 |
| `tests/unit/test_persona_store.py` | **新增** CRUD / id 校验 / 备份 |
| `tests/integration/test_admin_personas_api.py` | **新增** 全端点 + 409/400 + .env 落盘 |
| `tests/integration/test_studio_console.py` | 页签断言 + endpoints 清单 + 一条 personas 端点 |

---

## 四、分步实施（每步结束跑四项质量门）

1. **S1 会话删除·后端**：port / repo / service / route + 单测与集成测。
2. **S2 会话删除·Studio**：历史栏 wrapper + 删除按钮 + confirm + 当前会话处理 + 运行中禁用。
3. **S3 人格底座**：`toml_loader` 模型提公共 + `toml_writer` + `store` + 单测（round-trip、备份、id）。
4. **S4 personas API**：`dotenv_store.write_values` 提取（admin_config 跟随改）+ `admin_personas.py` + 集成测 + app 注册。
5. **S5 Studio 人格页**：nav / 列表 / 编辑器 / 切换重启 / 守门测试更新。
6. **S6 收尾**：docs（04 / 05 / README / `.env.example`）+ 全量四项质量门 + 报告。

---

## 五、风险与已知取舍

- **保存重写 TOML、注释丢失**：已决策（ADR 0029 Reason）；UI 明说 + `.bak` 留档。
- **切换 / 编辑要重启**：未监督运行时 activate 会 409；UI 如实提示手动重启。
  已连接客户端（Unity）会随重启断一次线，重连后就是新角色。
- **删除会话不可恢复**：二次确认是唯一防线；当前会话在 turn 运行中禁用删除。
- **删除人格不删数据**：用同名 id 建回来会"找回"旧历史与记忆——这是**有意**的
  （不静默删数据），确认框里会说明。
- **`persona_path` 被手改成目录外路径**：人格页仍按它的父目录列人格；
  这是约定不是加密——文档里写明。
- Studio 页签数到 7，左栏是纵向列表，不需要改布局；人格页复用 `config-body` 的栅格样式。

---

## 六、真机验收步骤（用户，含重启）

**① 会话删除**

1. Studio 对话页 → 左栏随便挑一段旧会话点「删」→ 确认框出现且带摘要 → 确认；
2. 列表里它消失；刷新页面后也没有；
3. 删当前正在聊的那一段 → 聊天区清空、直接发消息 → 自动开新的一段（左栏出现新条目）；
4. 运行中（她正在回复）时，当前会话的删除按钮应为禁用。

**② 多人格**

1. 人格页 → 「新建」id `luna`、显示名 `Luna` → 保存 → 列表出现 Luna（未激活）；
2. 选中 Luna → 改 traits 与 behavior.rules → 保存 → 提示"重启后生效"；
3. 「切换为这一位」→ 确认重启 → 后端重启（遮罩），页面自动重连；
4. 重启后：对话页左栏**没有** Astra 的历史（新角色）；和 Luna 聊两句、让它记点东西；
5. 切回 `astra`（同样要重启）→ 历史、记忆、情绪状态都还是原来的；
6. 删除：使用中的那一位删除应被拒（409 提示）；删掉 Luna → 文件消失；
   再新建同名 `luna` → 旧对话与记忆**回来了**（预期行为，确认框里有说明）；
7. 「删除」在只剩一位时应被拒。

**③ 提示词编辑**

1. 人格页改 Astra 的 `behavior.rules`（比如删掉一条反模板规则）→ 保存并重启；
2. 对话区聊两轮，能感觉到语气变化（或至少 `/dev` 的原始 prompt 里能看到改动）。

---

## 七、与红线的对照

- 不改 `/health` 与 `/v1/config` 的 key 集合（人格切换只改这两个端点里值的来源）；
- `/dev` 不删不改；不新增 Python 依赖（TOML 序列化手写，前端零构建单文件）；
- 记忆 / 会话 / 人格删除都走显式确认；不静默删除任何用户数据；
- `create_app` 仍是唯一装配点；协议（`docs/04` 的帧与事件）一行未改。