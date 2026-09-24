# ADR 0023 — 显式开新会话（`POST /v1/conversations`）

## Decision

新增 `POST /v1/conversations`：**结束当前 active 会话并立即开一段新的**，
返回 `{conversation_id, started_at}`（201）。

- 旧会话只被置为 `ended`，**不删除任何数据**——历史、记忆、状态都还在，
  `GET /v1/conversations/{id}/messages` 照常可读。
- 开发者控制台加了「新会话」按钮。
- `ConversationService.start_new()` 与 `resolve_or_create` 共用同一个私有
  `_create_active()`，避免两份"怎么建会话"的逻辑漂移。

## Reason

**这是 M4 真机验收暴露出来的缺口。** 在此之前，新会话只有一条路：
**空闲 60 分钟自动切分**（ADR 0012）。用户没有任何显式手段脱身。

踩到的具体场景：早期 `llm_provider=none` 时，降级回复
（"我现在还没有接上语言模型，所以没法回话。"）被当成正常 assistant 消息
**写进了历史**。用户配好 LLM 之后，nemotron-4B 在 CPU、关掉思考、上下文里又堆了
十几条同一句话——它开始**照抄历史里出现最多的那句回复**。

表现极具误导性：`/health` 显示 `llm: ok`、真实模型调用要 19.8 秒、
`turn.completed` 一切正常，但回复就是那句降级文案。看起来像"LLM 没跑起来"，
实际是上下文被污染。

`resolve_or_create` 对显式给出的未知 `conversation_id` 是**拒绝**的
（ADR 0012，不静默新建），所以客户端也无法用"随便传个新 id"来绕开。

**为什么不是"清空历史"**：用户想摆脱的是*这段对话的上下文*，不是自己说过的话。
把它置为 `ended` 既解决了问题，又保留了可追溯性——而且与空闲切分走的是
同一条状态迁移（`active → ended`），没有引入新的会话状态。

**为什么不是"删库重来"**：`data/astra.db` 里同时装着记忆与情绪/关系状态
（迁移 0003 / 0004）。为了换一段对话而丢掉长期记忆，代价完全不成比例。

## Consequences

- `ConversationService.start_new()` 会**结束当前 active 会话**。如果调用时正好
  有一轮 turn 在跑，那一轮会继续用它自己已经解析好的 `conversation_id`
  写下去——不会串到新会话里（每轮在开始时解析一次会话，见 `run_turn`）。
- 与空闲切分**语义一致**：两者都只做 `active → ended` + 建新会话。
  区别只是触发者（时钟 vs. 客户端）。
- Unity 端将来也需要这个能力（"开一段新对话"按钮），所以它放在正式协议里，
  而不是做成 dev-only 端点。
- 新增集成测试：开新会话后 `POST /v1/chat/turns` 落在新会话里；旧会话历史仍可读。
  新增单元测试：`start_new` 结束旧会话、保留旧历史、不改 `(user_id, companion_id)`
  这两个记忆/状态的键。
- **文档同步**：`docs/04` §2 增加该端点。
