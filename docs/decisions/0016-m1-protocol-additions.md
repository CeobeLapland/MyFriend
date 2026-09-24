# ADR 0016 — M1 HTTP / WS Protocol Additions

> **状态**：M1a 部分（`/v1/chat/turns`、`/v1/config`、
> `/v1/conversations/{id}/messages`）已实现；M1b 部分待实现。

## Decision

### M1a 新增（已实现）

| 端点 | 语义 |
|---|---|
| `POST /v1/chat/turns` | **同步**跑完整个 turn，200 返回完整结构化响应 + `turn_id` + `conversation_id` + `timings` |
| `GET /v1/config` | 非敏感运行配置 + 协议词表（`emotions` / `actions` / `voice_styles`） |
| `GET /v1/conversations/{id}/messages` | 历史（按 id 升序）；未知会话返回 **404** |

### M1b 新增（待实现）

| 端点 / 事件 | 语义 |
|---|---|
| `POST /v1/chat/messages` | `202 {turn_id, conversation_id, status:"accepted"}`，结果走 WS |
| `WS /ws/v1/session/{session_id}` | 实时事件流 |
| 事件类型 `turn.failed` | 见 ADR 0014 |
| 事件类型 `error.protocol` | 客户端发来非法帧时回一条，**不关连接** |

### 对既有契约的补充

- `POST /v1/chat/messages` 的响应体加 `conversation_id`：请求里带了
  `conversation_id` 而响应不给回来，客户端无从续话。
- `ErrorCode` 闭集加 `invalid_input`（见 ADR 0014）。
- `docs/04_API_AND_EVENTS.md` §4 的事件清单需要补 `turn.failed` 与 `error.protocol`。

## Reason

**为什么另加 `/v1/chat/turns` 而不是给 `/v1/chat/messages` 加 `?wait=true`**：
阻塞与非阻塞是**两种契约**（状态码、超时、失败语义都不同），塞进一个路径会让
响应模型变成联合类型，客户端也得靠参数分叉。新路径零歧义，而且它正好是
M1 的验收工具与调试入口——没有它就只能靠 WS 客户端验证，门槛高得多。

**为什么协议错误不关连接**：一个手滑（发错字段名）不该让用户掉线。
回一条 `error.protocol` 让客户端知道哪里错了，连接照常。

## Consequences

- `docs/04_API_AND_EVENTS.md` 需要同步更新（本 ADR 是那次更新的依据）。
- M1b 的 WS 集成测试必须跑**真实 uvicorn**，不能用 `TestClient.websocket_connect()`：
  后者没有超时参数（CI 里卡住是挂死而不是失败），而且它自己实现 WS 协议、
  不经过 uvicorn，**会掩盖 `websockets` 依赖缺失**——而实测确认本机
  `websockets` 与 `wsproto` 都还没装。
