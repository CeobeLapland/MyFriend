# ADR 0013 — In-Process Event Bus with Bounded Subscriber Queues

> **状态**：决策已定，实现在 **M1b**（M1a 不发布事件）。

## Decision

- 进程内 async pub/sub（`application/event_bus.py`），**不提前抽 `EventPublisher`
  端口**——ADR 0002 已定单进程，M7 真要拆进程时再抽。
- 每个订阅者一个**有界队列**（默认 128）。队满时**淘汰最旧**再放入新的，
  并累加 `dropped` 计数。
- `publish` **永不 await 消费者**，只用 `put_nowait`。
- 两层保护各管一件事：
  1. **Bus 侧**淘汰最旧 → 保证 turn 永不阻塞；
  2. **WS 侧** `asyncio.wait_for(ws.send_json(...), timeout=...)` 超时则关闭该连接
     → 保证一个卡死的 socket 只杀掉自己。
- **幂等性**（`docs/04` §6）：Bus 持一个 512 长的 `deque` + `set` 记录最近的
  `event_id`，重复则记 WARNING 并跳过。

## Reason

**为什么淘汰最旧而不是最新**：对实时 UI / Avatar 而言，**最新**状态才有意义。
`turn.completed` 绝不能被积压的旧事件挡住。

**为什么不能 `await queue.put()`**：那正是"一个连上就不读的 WS 客户端拖垮整个
turn"的成因——`publish` 会一直等这个消费者。而 `put_nowait` 在队满时抛
`QueueFull`，不显式捕获就会从 `publish` 冒出去把 turn 打挂。

**为什么要两层**：Bus 侧的淘汰保护的是 turn（业务不被拖慢），WS 侧的超时保护的
是进程（不无限堆积）。两者职责不同，缺一不可。

## Consequences

- `logging_setup.EXTRA_ALLOWLIST` 需要加 `dropped` 字段。
- 慢消费者只会看到"事件被丢弃"的计数增长，不会收到任何错误——这是刻意的：
  实时流允许丢帧，不允许阻塞。
- 持久化侧的幂等不在 Bus 上做，而在 `ConversationService.append_turn` 里：
  同一事务内先查 `(turn_id, role)` 是否已存在（SQLite serialized + 单进程 +
  每会话串行锁 → 无竞态）。
