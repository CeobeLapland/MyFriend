# ADR 0019 — Memory Management API and User Control

## Decision

### 1. `/v1/memories` 全套端点（`docs/04` §2 + `docs/06` §9）

| 端点 | 语义 |
|---|---|
| `GET /v1/memories` | 列表；过滤 `type` / `min_importance` / `created_after` / `archived` / `limit`；带 `query` 时改走**语义检索** |
| `GET /v1/memories/{id}` | 单条 |
| `PATCH /v1/memories/{id}` | 编辑；`content` 变了就**重新向量化并更新索引** |
| `DELETE /v1/memories/{id}` | **硬删除**：事实 + embedding + 向量索引 |
| `POST /v1/memories/clear` | 清空该用户的全部记忆 |
| `GET /v1/memories/export` | 导出 JSON，**含已归档与已过期**（导出是备份，不是检索） |
| `GET` / `PUT /v1/memories/settings` | 读取 / 切换"记忆启用" |

### 2. 静态路径必须声明在 `/{memory_id}` 之前

FastAPI 按声明顺序匹配。否则 `/settings`、`/export`、`/clear` 会被当成 id 吞掉。
有测试（`test_static_routes_are_not_shadowed_by_the_id_route`）守着这条。

### 3. "禁用记忆"是**运行时开关**，存 `app_flags`

`Settings.memory_enabled` 只是**启动期默认值**；`app_flags` 里的
`memory.enabled` 记录存在时以它为准。这样用户不重启就能关掉记忆
（`docs/06` §9 的"禁用记忆"）。

禁用时**同时关掉读取与写入**——只关一边会留下"还在偷偷记"或"还在往外读"的
半吊子状态。

### 4. no-store 是短语匹配，边界写进 README

`docs/06` §9 要求"用户说'不要记住这件事'必须生效"。实现是正则匹配
（不要记住 / 别记住 / 不用记 / 忘掉这件事 / 不要记录 …），命中则**本轮完全不写入**，
并记 `event="memory.suppressed"` 日志。

**诚实边界**：这是短语匹配，不是语义理解——"帮我留个印象"这类变体会漏判。
README 的"已知限制"里写明，并给出事后删除的路径（`DELETE /v1/memories/{id}`）。

### 5. 未知 id 返回 404，不静默成功

`DELETE` / `PATCH` / `GET` 遇到不存在的 id 一律 404，body 形如
`{"detail": {"code": "memory_not_found", "message": "..."}}`——与
`conversations` 路由的 404 形状一致。

`ErrorCode` 是**闭集**（ADR 0006 起），所以这里不新增错误码：那套码是给
turn 失败协议用的，HTTP 资源级 404 用独立的 code 即可。

## Reason

**为什么删除必须在一个事务里清三处**：`docs/04` §2 与 `docs/11` §4 都要求
"删除 memory 时应同时处理 vector index"。漏删向量索引会导致"删了还能检索到"
——**直接违反 M2 验收第 5 步**，而且是个静默 bug（接口返回 204，行为却不对）。
`test_memory_repository.py` 里有断言直接查三张表的行数。

**为什么 `export` 含归档与过期**：导出的用途是备份与迁移，不是展示。
如果它和列表一样过滤掉归档项，用户就永远导不出完整数据，而这正是
`docs/06` §9 要的"导出"。

**为什么 PATCH 改内容要重新 embed**：不重新 embed 的话，文字改了但向量还是旧的，
后续检索会按旧语义命中——用户会看到"我明明改过它，怎么还按老样子被想起来"。

**为什么禁用开关不放进 `Settings`**：`Settings` 是启动期快照（`extra="forbid"` +
`lru_cache`），改它要重启。而"禁用记忆"是个隐私开关，必须**立即**生效。

## Consequences

- `GET /v1/config` 增加 `memory_enabled`（读运行时状态，不是 Settings）与
  `memory_types` 词表（与 emotions/actions 同理属于协议契约）。
- `TurnResponse` 增加 `memories_retrieved` / `memories_created`——没有这两个数，
  "记忆到底有没有在工作"就只能靠查库。
- 开发者控制台（ADR 0017）加一个记忆面板：列表、单条删除、清空、启用开关。
  仍然是**零构建、零 npm、零新增 Python 依赖**。
- `?type=goal` 这类不认识的类型名返回 **422**，而不是静默返回空列表——
  "过滤条件写错了"和"确实没有这类记忆"必须能区分。
