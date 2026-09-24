# ADR 0014 — Turn Degradation and Honest Failure

## Decision

LLM 不可用时 **不返 5xx**：

```
HTTP 200
{
  "status": "failed",
  "failure": {"code": "llm_unavailable", "message": "...", "recoverable": true},
  "reply": "我这边的模型服务连不上了。你确认一下 LM Studio 在运行，然后再说一次好吗？"
}
```

- **`reply` 在任何情况下都非空**。失败时的文案**由代码拥有**——不能问 LLM，
  LLM 正是挂掉的那个。
- 人格文件可用同名错误码覆盖文案（`[errors] llm_unavailable = "..."`）。
- **用户消息仍然持久化**，助手那条以 `metadata_json.status="failed"` 落库。
- WS 路径推**独立事件 `turn.failed`**，而不是 `turn.completed{status:"failed"}`。
- 错误码是**闭集**（`domain/errors.py` 的 `ErrorCode`）：
  `llm_unavailable` / `llm_timeout` / `llm_disabled` / `llm_invalid_response` /
  `conversation_not_found` / `invalid_input` / `internal_error`。

## Reason

**为什么不返 5xx**：ADR 0006 已经确立本项目的哲学「HTTP 恒 200，严重程度只在
body 表达」；`docs/01_REQUIREMENTS.md` §3 要求任何外部依赖不可用都要降级继续
运行。返 5xx 会让客户端以为 API 本身坏了。

**为什么文案由代码拥有**：`docs/01_REQUIREMENTS.md` §4 说"当工具失败时，要自然地
告诉用户发生了什么，而不是编造成功结果"。让 LLM 生成失败文案是不可能的（它挂了），
所以只能是代码里的确定性文案——可测、不编造。

**为什么用户消息仍要落库**：否则重连的客户端会看到一条悬空的用户消息，无法解释。

**为什么 `turn.failed` 是独立事件**：客户端普遍用 `turn.completed` 收尾 UI，
让它收到一个失败状态会误导。命名与文档已有的 `tool.failed` 对齐。

## Consequences

- 健康探针的状态映射与之配套：可达即 `ok`、可达但模型未加载即 `degraded`、
  不可达即 `down`。`llm` 不在 `required` 集合里，所以整体变 `degraded` 而 HTTP 仍 200
  —— 精确对上 `docs/10_TESTING_AND_ACCEPTANCE.md` §4 的
  "LM Studio unavailable → health marks degraded"。
- **与计划的偏差**：错误码闭集里新增了 `invalid_input`。Orchestrator 可能从任何
  客户端收到纯空白输入，边界层不一定拦得住；没有这个码就只能报 `internal_error`，
  那是在撒谎。
