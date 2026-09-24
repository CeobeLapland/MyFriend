# ADR 0016 — M1 HTTP / WS Protocol Additions

> **状态**：已全部实现（M1a + M1b）。

## Decision

### 新增端点（已实现）

| 端点 | 语义 |
|---|---|
| `POST /v1/chat/turns` | **同步**跑完整个 turn，200 返回完整结构化响应 + `turn_id` + `conversation_id` + `timings` |
| `POST /v1/chat/messages` | `202 {turn_id, conversation_id, status:"accepted"}`，结果走 WS（实测 77ms 返回） |
| `GET /v1/config` | 非敏感运行配置 + 协议词表（`emotions` / `actions` / `voice_styles`） |
| `GET /v1/conversations/{id}/messages` | 历史（按 id 升序）；未知会话返回 **404** |
| `WS /ws/v1/session/{session_id}` | 实时事件流 |

### 新增事件类型（已实现）

- `assistant.text.completed` —— 承载回复正文与归一化后的 emotion/action/voice。
  **没有它 WS 客户端就没东西可显示**（M1b 验收时才发现最初漏了这条）。
- `turn.failed` —— 见 ADR 0014。
- `error.protocol` —— 客户端发来非法帧时回一条，**不关连接**。

### 对既有契约的补充

- `POST /v1/chat/messages` 的响应体加 `conversation_id`：请求里带了
  `conversation_id` 而响应不给回来，客户端无从续话。
- `ErrorCode` 闭集加 `invalid_input`（见 ADR 0014）。
- `docs/04_API_AND_EVENTS.md` §2/§4 已同步更新（本次一并改完）。

### 实现细节

- **WS 路由的依赖必须用 `HTTPConnection` 而不是 `Request`**：WS 作用域里没有
  `Request`，用它会报 `missing 1 required positional argument`。
- **每轮 turn 的事件序列**（新会话的第一轮会多一条 `conversation.created` 打头）：
  ```
  turn.started
  → conversation.message.created（user）
  → conversation.message.created（assistant）
  → assistant.text.completed
  → avatar.expression.set
  → avatar.action.play
  → turn.completed
  ```
- **turn 在 WS 路由里是内联 await 的**：同一连接上的消息天然保序；跨连接的顺序
  由 Orchestrator 的每会话锁保证。

## Reason

**为什么另加 `/v1/chat/turns` 而不是给 `/v1/chat/messages` 加 `?wait=true`**：
阻塞与非阻塞是**两种契约**（状态码、超时、失败语义都不同），塞进一个路径会让
响应模型变成联合类型，客户端也得靠参数分叉。新路径零歧义，而且它正好是
M1 的验收工具与调试入口——没有它就只能靠 WS 客户端验证，门槛高得多。

**为什么协议错误不关连接**：一个手滑（发错字段名）不该让用户掉线。
回一条 `error.protocol` 让客户端知道哪里错了，连接照常。

**为什么 202 端点要预先解析 `conversation_id`**：否则客户端得等到
`turn.started` 才知道该监听哪段会话。

## Consequences

- M1b 的 WS 集成测试跑**真实 uvicorn**，不用 `TestClient.websocket_connect()`：
  后者没有超时参数（CI 里卡住是挂死而不是失败），而且它自己实现 WS 协议、
  不经过 uvicorn，**会掩盖 `websockets` 依赖缺失**——实测确认本项目最初
  `websockets` 与 `wsproto` 都没装，`uvicorn[standard]` 之外的安装方式
  在 `/ws/...` 上根本握不上手。
- `websockets` 已是运行时依赖（v17.1）。`serve` 用协议层心跳
  （`ws_ping_interval=20, ws_ping_timeout=20`），不新增自定义 ping 事件。
