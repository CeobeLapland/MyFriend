# ADR 0007 — Schema Location and Deferred Directories

## Decision

- JSON Schema **继续留在 `docs/schemas/`**，不在代码侧复制，也不建立根级 `schemas/`。
- `02_ARCHITECTURE.md` §6 建议的根级 `unity/` 目录延后到 M5。
- `tests/contract/` 延后到 M1（那时才有第一个对外协议契约）。
- `src/astra/providers/` 在 M0 只是一个空包（文档要求该层存在）。

## Reason

`docs/schemas/` 里的 schema 是**协议规格**，属于文档的一部分；把它复制到根级
`schemas/` 会产生两个真相来源，必然漂移。代码侧的契约由 Pydantic 模型承担
（`api/routes/health.py` 的 `HealthResponse`），并在需要时用测试与文档里的
schema 对齐。

空目录 + `.gitkeep` 是纯噪声。目录应该在第一次有内容时创建。

## Consequences

- 新增对外协议时，需要在 `docs/schemas/` 加 schema，并在代码里加对应的
  Pydantic 模型 + 契约测试（`08_IMPLEMENTATION_PLAN.md` 的 Definition of Done 要求
  "新增协议有 schema"）。
- M1 需要建立 `tests/contract/` 与 `docs/schemas/` 的对齐机制。
