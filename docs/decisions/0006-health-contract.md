# ADR 0006 — `/health` Contract

## Decision

`/health` 的响应体在 M0 一次定死，后续里程碑只改**值**、不改**结构**：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "schema_version": 1,
  "uptime_s": 3.42,
  "components": {
    "api":      {"status": "ok",             "latency_ms": 0.0,  "detail": null},
    "database": {"status": "ok",             "latency_ms": 1.8,  "detail": {"schema_version": 1}},
    "llm":      {"status": "not_configured", "latency_ms": null, "detail": null},
    "stt":      {"status": "not_configured", "latency_ms": null, "detail": null},
    "tts":      {"status": "not_configured", "latency_ms": null, "detail": null}
  }
}
```

- 组件状态枚举：`ok | degraded | down | not_configured | disabled`。
- **5 个组件 key 现在就全部输出**，未接入的填 `not_configured`。
  这样 Unity / 前端从 M0 起就能写死解析逻辑。
- **HTTP 状态码恒为 200**（只要进程还能响应），严重程度只在 body 的 `status` 表达。
- 每个组件固定三字段 `status` / `latency_ms` / `detail`。

聚合规则（`astra.domain.health.aggregate_status`）：

1. 任一 **required** 组件 `down` → 整体 `down`
2. 任一组件 `down`（非 required）→ 整体 `degraded`
3. 任一组件 `degraded` → 整体 `degraded`
4. 其余 → `ok`

`not_configured` / `disabled` 是中性状态，不参与判定。

## Reason

**为什么恒 200**：`01_REQUIREMENTS.md` §3 要求任何外部依赖（含 Memory DB）不可用时
系统都要降级继续运行，因此不存在"某个依赖挂了就该返 503"的语义；M0 的验收标准
也明确是 "health returns 200"。若将来需要 readiness 探针，另加 `/health/ready`，
`/health` 不受影响。

**为什么要区分 `not_configured` 与 `down`**：M0 的 `ASTRA_LLM_PROVIDER=none` 必须
诚实地映射为 `not_configured` 而不是 `ok`，否则 M1 的 degraded 验收会失去基准。

**为什么 `schema_version` 提到顶层**：客户端最关心的是"库 schema 对不对得上"。
它由 `components.database.detail.schema_version` 提升而来，便于直接比较。

## Consequences

- M1 注册 LLM 探针、M4 注册 STT/TTS 探针时，协议零改动。
- 探针抛异常不会让 `/health` 失败——`HealthService` 会把该组件记为 `down`。
- `api` 组件由 `HealthService` 直接合成：能产生这份报告即说明 API 正在服务。
