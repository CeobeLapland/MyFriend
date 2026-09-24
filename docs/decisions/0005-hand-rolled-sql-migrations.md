# ADR 0005 — Hand-Rolled SQL Migrations

## Decision

手写迁移 runner（约 100 行，零依赖），不使用 Alembic。

- 迁移文件为 `src/astra/infrastructure/db/migrations/NNNN_name.sql`。
  版本号即文件名前缀并保留零填充（如 `0001`），字典序即版本序。
- `schema_migrations(version, checksum, applied_at)` 记录已应用版本。
- **每个迁移在单个显式事务内原子应用**：DDL 与版本记录要么都成功，要么都回滚。
- SHA-256 checksum 校验；已应用的迁移被改动时报错，而不是静默跳过。
- 迁移文件内禁止出现 `BEGIN` / `COMMIT` / `ROLLBACK` / `SAVEPOINT` / `RELEASE`，
  由 `discover()` 拦截。

## Reason

`03_DATA_MODEL.md` §3 给出的 DDL 已经是原生 SQL，Alembic 的核心价值
（autogenerate、SQLAlchemy 类型映射）全部用不上，却必然拖入 SQLAlchemy——
与 ADR 0004 直接冲突。

## 关键实现约束

`sqlite3.Connection.executescript()` 的语义是：

> 若当前存在未提交事务，先隐式执行一次 COMMIT；除此之外**不做任何事务控制**。

因此**不能**在外层 `BEGIN` 之后再调用它，必须把 `BEGIN` / `COMMIT` 拼进传给它的
脚本文本里。失败时脚本会抛异常并把事务留在打开状态，runner 需要显式 `ROLLBACK`；
注意无事务时执行 `ROLLBACK` 会抛 `OperationalError`，所以必须先判断
`conn.in_transaction`。

## Consequences

- 需要自己维护"迁移文件不可变"的纪律——checksum 校验会在违反时阻止启动。
- 不支持 `down` 迁移。本地单用户项目里回滚靠备份数据库文件，不靠逆向迁移。
- 新增迁移就是新增一个 `.sql` 文件，无需改动 Python 代码。
