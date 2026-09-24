# ADR 0004 — SQLite Without ORM

## Decision

数据访问层使用 stdlib `sqlite3`，**同步**实现，不引入 ORM / 异步驱动。

- 每个工作单元开一个新连接，用完即关。
- 连接建立时统一打 PRAGMA：`journal_mode=WAL`、`foreign_keys=ON`、
  `busy_timeout=5000`、`synchronous=NORMAL`。
- async 调用方通过 `asyncio.to_thread` 调用，避免阻塞事件循环。
- 领域端口（`02_ARCHITECTURE.md` §3）仍声明为 `async def`——这是对**调用方**的
  契约，实现内部可以是同步的。

## Reason

`09_AGENT_INSTRUCTIONS.md` 的依赖规则第一条是"先检查 stdlib 是否已解决"；
`02_ARCHITECTURE.md` §5 明确禁止 `Domain -> SQLite ORM specific implementation`。

SQLite 打开成本是微秒级，本地单用户场景 QPS 极低，"每工作单元一连接"可以
天然规避跨线程共享问题，实测 `sqlite3.threadsafety == 3`（serialized）。

`aiosqlite` 本质就是把 `sqlite3` 丢进线程池，多一个依赖、多一层 API，能力零增量；
`asyncio.to_thread` 是 Python 3.9+ 的 stdlib 等价物。

## Consequences

- 需要手写 SQL 与迁移（见 ADR 0005）。
- 仓库层需要自己处理行映射（用 `sqlite3.Row` + 显式转换）。
- 时间戳按 ISO-8601 UTC 字符串存取，不使用 `detect_types` 自动转换。
- 未来若迁移到 PostgreSQL，只需替换 `infrastructure` 层的适配器，端口不变。
