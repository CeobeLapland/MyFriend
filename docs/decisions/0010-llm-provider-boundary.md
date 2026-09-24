# ADR 0010 — LLM Provider Boundary

## Decision

- 运行时 HTTP 客户端使用 **`httpx`**（从 dev 组提升为运行时依赖）。
- **`httpx` 只允许出现在 `astra/providers/**`**。`domain/`、`application/`、`api/`
  都不得 import 它，由 `tests/unit/test_architecture_rules.py` 强制执行。
- `domain` 只暴露中立的 `LLMRequest` / `LLMResponse`；provider 专有参数
  （例如"怎么把 `reasoning_effort` 发给某个后端"）只出现在 adapter 里。
- provider 失败时抛 `domain.errors` 里的异常，不返回错误对象——Orchestrator 的
  `try/except` 是唯一的降级点，与 `HealthService` 隔离探针异常同构。

## Reason

`urllib.request` 是纯阻塞的，每次调用都得 `asyncio.to_thread`；`urlopen(timeout=)`
是 connect + read 的合并超时，无法区分；P1 做流式时还要自己拆 `http.client`。
`httpx` 已经在 `uv.lock` 里（是 starlette `TestClient` 的底座），提升为运行时
依赖是零新增包。

限制 import 位置的理由：ADR 0001 承诺"provider 可替换"。如果某处直接 import
httpx 发请求，几个月后换后端时就会漏掉那一处，承诺会悄悄失效。

## Consequences

- 新增 `domain/errors.py` 的 `ErrorCode` 闭集与 `LLMUnavailableError` /
  `LLMTimeoutError` / `LLMDisabledError` / `LLMResponseError`。
- 端口 `LLMProvider` 用 `@runtime_checkable` Protocol 声明，测试里可以
  `isinstance(FakeLLMProvider(), LLMProvider)`。
- **异常映射的细节**：`httpx.ConnectTimeout` 同时继承 `ConnectError` 与
  `TimeoutException`，必须先判断前者——"连不上"和"模型太慢"是两种处境，
  前者重试没有意义。Windows 上连一个已关闭的端口得到的是 `ConnectTimeout`
  而不是 `ConnectError`，这条顺序是被实测逼出来的。
