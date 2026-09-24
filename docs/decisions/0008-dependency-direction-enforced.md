# ADR 0008 — Dependency Direction Enforced by Test

## Decision

`src/astra/domain/` 只定义**纯数据与 Protocol（端口）**，实现一律放在
`infrastructure` / `providers` / `api`。

禁止 `domain` 出现以下 import：

- 第三方：`fastapi`、`starlette`、`uvicorn`、`pydantic`、`pydantic_settings`、
  `sqlite3`、`httpx`、`requests`、`aiohttp`、`sqlalchemy`、`alembic`
- 本项目外层包：`astra.api`、`astra.application`、`astra.infrastructure`、
  `astra.providers`、`astra.settings`

该规则由 `tests/unit/test_architecture_rules.py` 用 AST 扫描强制执行，
并附带一个自检用例确保门禁本身有效（不是永远通过的空检查）。

## Reason

`02_ARCHITECTURE.md` §5 已经写明依赖方向规则，但写在文档里的规则会被违反。
用约 60 行 stdlib `ast` 代码把它变成可执行的测试，成本极低，收益是：
更换基础设施（SQLite → PostgreSQL、LM Studio → 其他 LLM）时业务层不受污染。

不引入 `import-linter` 之类的额外依赖——AST 扫描已经够用。

## Consequences

- 领域层不能直接使用 Pydantic 做校验，只能使用 `dataclass` + 显式不变量。
- 端口方法声明为 `async def`（对调用方的契约），同步实现内部用
  `asyncio.to_thread` 脱离事件循环——这不违反本规则。
- 新增领域模块时，若确实需要引入被禁模块，必须修改本 ADR 而不是绕过测试。
