# AGENTS.md

本文件是 Coding Agent 在本仓库的长期项目指令。来源：`docs/09_AGENT_INSTRUCTIONS.md`。

## Role

You are the implementation agent for `Astra`, a local-first AI companion application.
Your responsibility is to implement the system described by the repository specifications while preserving modularity, testability, and user control.

## Primary Source of Truth

Read these files before making architectural changes:

1. `docs/00_README.md`
2. `docs/01_REQUIREMENTS.md`
3. `docs/02_ARCHITECTURE.md`
4. `docs/03_DATA_MODEL.md`
5. `docs/04_API_AND_EVENTS.md`
6. `docs/05_PERSONA_AND_PROMPTS.md`
7. `docs/06_MEMORY.md`
8. `docs/07_AUDIO_AND_AVATAR.md`
9. `docs/08_IMPLEMENTATION_PLAN.md`
10. `docs/10_TESTING_AND_ACCEPTANCE.md`
11. `docs/11_SECURITY_AND_PRIVACY.md`

If implementation conflicts with documentation, do not silently choose. Update the documentation and code together, and record the decision in `docs/decisions/`.

## General Rules

- Prefer small, testable modules.
- Do not introduce a framework merely because it is popular.
- Do not make the LLM a hard dependency of domain logic.
- Do not make Unity a hard dependency of backend logic.
- Do not hard-code provider-specific assumptions outside adapters.
- Do not store memory only in vector indexes.
- Do not let natural-language LLM output directly control OS operations.
- Validate every structured LLM output.
- Treat tool requests as untrusted input.
- Do not claim a tool succeeded unless execution actually succeeded.
- Do not fabricate memory retrieval results.
- Do not silently delete user data.

## Development Workflow

For every task:

1. Read relevant docs.
2. Identify affected modules.
3. State the minimal implementation plan internally.
4. Implement the smallest correct change.
5. Add/modify tests.
6. Run formatter/linter/type checker/tests.
7. Update docs if behavior changed.
8. Report what changed, what was tested, and any remaining limitation.

## Code Style

- Python: type hints for public functions.
- Prefer async only when the operation is actually I/O-bound or streaming.
- Avoid hidden global mutable state.
- Use dependency injection for providers.
- Keep domain objects independent of HTTP/ORM code.
- Use structured logging.
- Never log raw sensitive audio or secret tokens by default.

## AI Provider Rules

LM Studio is the default local LLM provider, but code must depend on `LLMProvider`, not LM Studio-specific APIs.

Recommended local endpoint:

```text
http://localhost:1234/v1
```

Use Structured Output for assistant response contracts whenever the selected model supports it.

Use Tool Calling only through the Tool Registry.

## Memory Rules

A memory candidate is not automatically a permanent memory.

The memory pipeline must distinguish:

- candidate
- accepted
- archived
- deleted

Every permanent memory needs provenance.

## State Rules

LLM-generated state deltas must be clamped and validated.

Example:

```python
new_value = clamp(old_value + delta, 0.0, 1.0)
```

Never allow arbitrary LLM output to overwrite the entire state object.

## Tool Rules

Tool categories:

### safe

No side effects or trivial reversible side effects.

### confirm

Potentially meaningful side effects; user confirmation required.

### restricted

Dangerous, destructive, or privacy-sensitive operations; unavailable by default.

The agent must never add a tool capable of arbitrary shell execution as a default feature.

## Unity Rules

Backend sends semantic events. Unity decides animation implementation.

Do not embed Unity asset paths in Python domain code.

## Dependency Rules

Before adding a dependency:

- Check whether Python stdlib already solves the problem.
- Check existing dependencies.
- Verify maintenance status.
- Keep dependency surface small.

## Testing Rules

Every non-trivial feature needs tests for:

- happy path
- invalid input
- provider failure
- timeout/cancellation when relevant
- persistence/restart behavior when relevant

## 本仓库的工程约定

以下约定来自 `docs/decisions/` 中已记录的决策，属于硬约束：

- **分层依赖方向**：`Domain` 只定义 Protocol（端口），实现放 `infrastructure`。
  `src/astra/domain/**` 禁止 import `fastapi`、`starlette`、`sqlite3`、`httpx`、`astra.infrastructure`、`astra.providers`。
  该规则由 `tests/unit/test_architecture_rules.py` 作为门禁强制执行（ADR 0008）。
- **数据访问**：stdlib `sqlite3`，同步实现，仅在调用边界用 `asyncio.to_thread` 脱离事件循环。
  不引入 ORM / aiosqlite（ADR 0004）。
- **迁移**：手写 runner，每个迁移在单个显式事务内原子应用，禁止在迁移文件里写 `BEGIN`/`COMMIT`（ADR 0005）。
- **`/health` 协议**：固定输出 5 个组件 key，HTTP 状态码恒 200（ADR 0006）。
- **配置**：`Settings` 不得传入 `domain/`；`.env` 未声明的键会因 `extra="forbid"` 导致启动失败。

## Output Format to the User

After implementation, report:

```text
Implemented:
- ...

Tests:
- ...

Changed files:
- ...

Known limitations:
- ...

Next suggested task:
- ...
```

Do not dump large logs unless requested.
