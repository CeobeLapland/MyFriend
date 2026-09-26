# M1 Text Brain — 实施计划

## Context

M0 已交付并验收通过（96 个测试、`/health` 200、空库可从零初始化）。现在要实现 **M1「文本大脑」**——这是整个项目的心脏：turn 生命周期、结构化输出、事件总线一旦定错，M2–M5 全都要返工。

M1 的目标（`docs/08_IMPLEMENTATION_PLAN.md` 阶段 1）：LM Studio adapter、Conversation manager、Persona manager、Structured Response schema、Event Bus、SQLite persistence。

验收（文档原文）：`text in -> LLM -> structured response -> text out`，且不接 Unity、不接语音。

**已由用户拍板**：M1 不做流式（留 P1）；人格配置用 TOML（stdlib `tomllib`，零新增依赖）。

---

## 一、已实测确认的环境事实

这些不是推测，是本机实测结果，**直接决定下面的设计**：

| # | 事实 | 影响 |
|---|---|---|
| 1 | `qwen/qwen3.5-9b` **默认开启思考**：返回 `reasoning_content`，`content` 可能为空 | 必须默认 `reasoning_effort="none"` |
| 2 | **`reasoning_effort: "none"` 是唯一有效开关**（15.4s → 3.4s，`reasoning_tokens: 0`）。`chat_template_kwargs.enable_thinking=false`、`/no_think`、`chat_template_kwargs.thinking=false` **全部无效** | 适配器必须显式发这个参数 |
| 3 | `response_format` 的 `json_schema` + `strict:true` 工作良好，`$defs`/`$ref` 被接受（无需 flatten），键顺序跟随 schema | 可直接发 Pydantic 生成的 schema |
| 4 | **`strict:true` 不约束 `minimum`/`maximum`**：实测 `voice.pitch` 返回过 `200`（schema 限 `[-1,1]`） | clamp 是**必需**逻辑，不是防御性代码 |
| 5 | 模型会在自由字符串字段里写中文：实测 `emotion.primary="欢迎"`、`action.name="闲聊模式"` | Layer 8 必须给**英文词表清单**，否则情绪全被归一化成 neutral |
| 6 | **schema 裁剪实测收益**（对照组，两边 `reasoning_tokens` 均为 0）：完整 7 字段 **15.2s / 14.0s**（249/216 tok）↔ 裁剪 4 字段 **8.5s / 8.5s**（115/91 tok） | M1 只发 4 字段 profile |
| 7 | **`websockets` 与 `wsproto` 均未安装**（uvicorn 0.53 基础依赖只有 click + h11） | `/ws/...` 在生产路径上握不上手，必须显式加依赖 |
| 8 | Windows 时钟分辨率 **15.625ms**：3000 次 `datetime.now()` 只有 **1** 个唯一值 | `ORDER BY created_at` 无区分度，需要有序 ID |
| 9 | `json.loads` **默认接受** `NaN` / `Infinity` / `-Infinity` | 必须 `parse_constant=` 拦掉 + `math.isfinite` 兜底 |
| 10 | `httpx` 0.28.1 已在 `uv.lock`（dev 组）；`/v1/responses` 端点存在 | httpx 提升为运行时依赖，零新增包 |

---

## 二、技术选型结论

### 2.1 LLM 端口（`domain/llm.py`，纯 dataclass）

```
Role(StrEnum): system | user | assistant
ReasoningEffort(StrEnum): none | low | medium | high

LLMRequest: system_prompt, messages: tuple[LLMMessage,...], response_format: JsonSchemaFormat|None,
            temperature, max_tokens, timeout_s, reasoning_effort, correlation_id
LLMResponse: text, model, finish_reason, usage: LLMUsage|None, latency_ms,
             reasoning_present: bool, response_id
```

- **`reasoning_present: bool` 而不是 `reasoning: str`**：`logging_setup.SENSITIVE_KEYS` 里**没有** `reasoning`，一旦有人写成 `extra={"reasoning": ...}` 就会原样落盘。让它在类型层面不可能更便宜。**同时加一条测试断言 `"reasoning" not in EXTRA_ALLOWLIST`**。
- 端口加进 `domain/ports.py`：`@runtime_checkable class LLMProvider(Protocol)`，测试里 `isinstance(FakeLLMProvider(), LLMProvider)` 只查方法存在性。
- **失败用异常**：`domain/errors.py` 定义 `ErrorCode` 闭集 + `LLMUnavailableError / LLMTimeoutError / LLMResponseError`（带 `retryable: bool`）。Orchestrator 的 `try/except` 是唯一降级点，与 M0 `HealthService._run_probe` 的异常隔离同构。

### 2.2 结构化输出：六段流水线（`application/response_parser.py`）

| 段 | 动作 | 失败归类 |
|---|---|---|
| 1 | strip → 去 ```json 围栏 → 取首个平衡 `{...}` → `json.loads(parse_constant=_reject)` | **可重试** |
| 2 | 未知键丢弃（WARNING 只记键名，不记值） | 降级 |
| 3 | Pydantic 校验（`extra="ignore"`） | 缺 `reply` → 可重试；其余缺失 → 补默认 |
| 4 | 枚举归一化：`emotion.primary ∉ EMOTIONS → "neutral"`、`action.name ∉ ACTIONS → "idle"`、`voice.style ∉ VOICE_STYLES → "warm"` | 降级 |
| 5 | clamp + `math.isfinite`：intensity→[0,1]、speed→[0.5,2.0]、pitch→[-1,1] | 降级 |
| 6 | 语义丢弃：`tool_calls` 非空 → 丢弃 + WARNING（M1 Tool Registry 为空）；`memory_candidates` → 丢弃（M2 消费） | 降级 |

**重试矩阵**（写进 ADR 0011，作为契约测试的驱动表）

| 情况 | 处置 |
|---|---|
| 连接失败 | **不重试**，立即 `llm_unavailable` |
| `ReadTimeout` | 重试 1 次 |
| `finish_reason=="length"` 且 `content` 空 | 重试 1 次并强制 `reasoning_effort=none`（思考吃光 token 的签名） |
| JSON 解析失败 / `reply` 空 | 重试 1 次（追加修复指令），仍失败则 turn 失败 |
| 未知枚举 / 越界数值 / 缺字段 / 多余键 | 降级，**不重试** |

### 2.3 schema profile：M1 只发 4 字段

M1 对 `state_delta`（M3）、`memory_candidates`（M2）、`tool_calls`（M6）**没有任何消费者**，生成即丢弃，却实测多付 1.7x 延迟。

- **M1 发送**：`{reply, emotion, action, voice}`，`additionalProperties:false`，`strict:true`
- **`docs/schemas/assistant_response.schema.json` 保留 7 字段完整定义**，加一句备注说明 M1 只发 profile 子集，M2/M3/M6 逐阶段启用。对外契约描述仍完整，且只有一份文件（符合 ADR 0007）
- **不加 `enum` 约束**：加了会让第 4 段归一化变成死代码却仍需维护；改为**词表写进 Layer 8 提示文本（软约束）+ 代码白名单归一化（硬保证）**
- `llm_max_tokens` 默认 **1024 → 400**（4 字段实测只用 ~90-115 tok）

### 2.4 Event Bus：进程内 async pub/sub + 有界队列淘汰最旧

- `EventBus.subscribe(maxsize=128) -> Subscription`；`publish` 用 `put_nowait`，`QueueFull` 时**淘汰最旧**再放入新的，`dropped += 1`
- **淘汰最旧而非最新**：对实时 UI/Avatar，最新状态才有意义，`turn.completed` 绝不能被积压挡住
- **两层保护各管一件事**：Bus 侧淘汰最旧（保证 turn 永不阻塞）；WS 侧 `wait_for(ws.send_json(...), timeout=ws_send_timeout_s)` 超时则关闭该连接（保证一个卡死的 socket 只杀掉自己）。sender 是独立 task
- **幂等性**（`docs/04` §6）：Bus 持 512 长 `deque`+`set` 记最近 `event_id`，重复则 WARNING 跳过。持久化侧幂等在 `ConversationService.append_turn` 内做：同一事务里先查 `turn_id + role` 是否存在
- **不提前抽 `EventPublisher` 端口**：ADR 0002 已定单进程，M7 拆进程时再抽

### 2.5 ConnectionManager

- `connect(session_id, ws)` → accept → subscribe → 起 sender task；`disconnect` **双侧触发必须幂等**（receiver 的 `finally` + sender 异常退出都会调）
- **同一 session 允许多连接**：M5 场景下 Unity 客户端 + 文本调试客户端会同时在线
- **死连接检测用 uvicorn 协议层 ping**（`ws_ping_interval=20, ws_ping_timeout=20`），**不新增 `system.ping` 事件类型**（不擅自扩协议）
- **每会话串行化**：`dict[conversation_id, asyncio.Lock]`，保证消息保序（M3 的 state_delta 应用天然需要串行）。已知代价：一次长 LLM 调用会阻塞同会话下一轮，M4 做 interruption 时重新评估
- **协议错误不关连接**：回一条 `error.protocol`，一个手滑不该掉会话

### 2.6 HTTP 与 WS 的分工

| 端点 | 语义 | 里程碑 |
|---|---|---|
| `POST /v1/chat/turns` | **同步跑完整个 turn**，200 返回完整结构化响应 + `turn_id` + `conversation_id` + `timings` | M1a |
| `POST /v1/chat/messages` | `202 {turn_id, conversation_id, status:"accepted"}`，结果走 WS | M1b |
| `GET /v1/config` | 非敏感配置 + 词表（emotions/actions/voice_styles） | M1a |
| `GET /v1/conversations/{id}/messages` | 历史（重启验收需要） | M1a |
| `WS /ws/v1/session/{session_id}` | 实时事件流 | M1b |

**为什么另加 `/v1/chat/turns` 而不是给现有端点加 `?wait=true`**：阻塞与非阻塞是两种契约（状态码、超时、失败语义都不同），塞进一个路径会让响应模型变成联合类型。新路径零歧义，且正好是 M1a 的验收工具。

### 2.7 Persona（TOML）+ Prompt 8 层

**文件**：`personas/astra.toml`（随仓库分发；不是 `data/`，那不是用户数据）。
**加载**：`infrastructure/persona/toml_loader.py`，stdlib `tomllib`（**必须 `open(path,"rb")`**）→ `domain.Persona` frozen dataclass。
**时机**：`create_app()` 装配期同步加载，**缺文件直接启动失败**（人格是配置不是外部服务，缺失等于应用没有身份）。

TOML 结构：`[identity]`（name/display_name/version/pronouns/self_description）、`[traits]`（自由键 → 0..1 浮点）、`[communication]`（sentence_length/emoji_frequency/rhetorical_questions/language）、`[behavior] rules = [...]`、`[prompt] hard_constraints = [...]`、`[errors]`（可选，覆盖降级话术）。

**情绪/动作/风格白名单放代码不放 TOML**：白名单是**协议契约**（Unity 要按同一份词表实现表情映射），放 TOML 会产生两份真相。放 `domain/vocabulary.py` 的 `frozenset`，通过 `GET /v1/config` 分发。

**Prompt 8 层**（`domain/prompt.py`，纯函数 `build_system_prompt(persona, *, state=None, memories=(), tools=())`）：

| 层 | M1 状态 |
|---|---|
| 0 Safety / hard constraints | 实现（常量 + persona 覆盖，含"不编造记忆/工具结果/观察"） |
| 1 Identity | 实现 |
| 2 Stable personality | 实现（traits + behavior rules） |
| 3 Speech style | 实现 |
| 4 Current state | **空实现**：`state=None` → **整段省略**（不输出空标题误导模型） |
| 5 Relevant memories | **空实现**：空则整段省略 |
| 6 Current conversation | 实现（走 `LLMRequest.messages`，不进 system prompt） |
| 7 Tool definitions | **空实现**：空则省略 |
| 8 Output schema | 实现（**只放人话版 + 英文词表清单，不贴整段 JSON Schema**） |

- **确定性**（`docs/10` §2 要求）：固定层序、traits 用 `sorted()` 遍历。测试断言"同输入 → 逐字节相同"
- **否决**用 `str.format` 拼大模板——层边界不可测

### 2.8 Conversation / Session 模型

| 概念 | 性质 | 生命周期 |
|---|---|---|
| `session_id` | **传输层临时概念，不落库** | WS 连接到断开 |
| `conversation_id` | 持久线程（`conversations` 表一行） | 长期；空闲超时后 `status='ended'` |
| `turn_id` | 一次输入→一次回复 | 随 `messages.turn_id` 持久化 |
| `message_id` | 单条消息 | 永久 |

**session 与 conversation 的关系**：session 在**首次 `input.user_text` 时惰性解析**出 conversation（与 HTTP 走同一用例），结果通过 `turn.started.payload.conversation_id` 告知客户端。

**`resolve_or_create` 规则**：
1. 显式给了 `conversation_id` 且存在、属 `(user_id, companion_id)`、`status='active'` → 用它
2. 显式给了但不存在/已结束 → **拒绝**（HTTP 404 / WS `error.protocol`），**不静默新建**（会掩盖客户端 bug）
3. 未给 → 取最近一个 `status='active'`；若其最后一条消息早于 `conversation_idle_minutes`（默认 60）→ 置 ended 并新建；否则复用
   - **为什么需要空闲切分**：否则所有对话永远堆在一个 conversation 里，M2 的上下文窗口只能机械截断；有了边界 M2 才能对已结束的 conversation 做摘要
4. `user_id`：M1 无 `users` 表，新增 `ASTRA_USER_ID`（默认 `"local"`）

**上下文窗口**：`llm_history_max_messages=20` + `llm_history_max_chars=12000`，从**最旧**开始按**整条消息**丢弃，当前用户消息永不丢弃，system prompt 永不裁剪。**按字符而非 token**：本地没有 qwen 的 tokenizer，`tiktoken` 对 qwen 是错的词表——用错的 tokenizer 做预算比用字符更不诚实。裁剪时记 `event="llm.history.trimmed"`。

### 2.9 turn 生命周期与空桩

| 步骤 | M1 |
|---|---|
| input_received / normalize_input / build_context / ask_llm / validate_response / persist_conversation | 实现 |
| dispatch_avatar（仅事件） | 实现（M1b）——**不发任何 `tts.*`**（M1 没有真实合成，发假的 `tts.completed` 是撒谎） |
| retrieve_memory / apply_state_delta / enqueue_memory_extraction | **空桩** |

**空桩的具体形态**（关键）：
```python
async def _retrieve_memory(self, req: TurnRequest) -> tuple[Memory, ...]:
    """M2 实现。M1 恒返回空元组——这是刻意的降级行为，不是 TODO。"""
    return ()
```
- 必须**返回合法空值让下游正常走完**，绝不 `raise NotImplementedError`、绝不 `pass`（后者会让"忘了实现"和"刻意留空"长得一样）
- **加「桩账本」测试**：断言 `TurnResult.stubs_used == frozenset({"retrieve_memory","apply_state_delta","enqueue_memory_extraction"})`。M2 实现一个就会红，强制显式更新

`TurnResult`：`turn_id, conversation_id, response: AssistantResponse|None, failure: TurnFailure|None, stubs_used, timings, model, retries, degraded`

**否决**现在就为 `MemoryRetriever` / `StateManager` 定义 Protocol——`AGENT_KICKOFF` 明确要求 "Prefer a minimal implementation over speculative abstractions"，M2 才知道形状。

### 2.10 ID 生成：统一 ULID

- `domain/ids.py`：stdlib 实现 ULID（48-bit 毫秒 + 80-bit 随机 → Crockford Base32，26 字符），时钟与随机源可注入。约 35 行，**零依赖**
- 统一用于 `event_id` / `correlation_id` / `turn_id` / `message_id` / `conversation_id` / `session_id`
- **排序一律 `ORDER BY id`**，`created_at` 只作展示（实测时钟分辨率 15.6ms，同 turn 两条消息时间戳必然相同）
- 额外收益：`docs/04` §1 示例 `"event_id": "01J..."` **正是 ULID**，实现与文档一致
- **推翻 M0 的决定**：M0 ADR 说"UUID4 已满足，顺序由 created_at 索引保证"——实测证明 created_at 保证不了，需在 ADR 0012 显式更正
- **`correlation_id` 传播规则**：**一个 turn 一个**，在被接受时生成，复制进该 turn 的每个事件、两条消息的 `metadata_json`、每条日志、`LLMRequest`。无 turn 上下文的事件 → `correlation_id = event_id`。M1 里两者相等但**保留两个字段**（M7 Scheduler 需要跨 turn 相关性）
- **否决** `python-ulid`（35 行换一个依赖）、`uuid7`（Python 3.13+，本机 3.12 无）

### 2.11 LLM 健康探针

- **探**：`GET {base_url}/models`（免费、瞬时、同时证明可达性与鉴权）
- 可达但 `settings.llm_model` 不在列表 → **`DEGRADED`**，`detail={"error":"model_not_loaded"}`（这正是"8GB 显存装错模型"的有用信号）
- 不可达 → **`DOWN`**（可达性是二值的；`DEGRADED` 留给"可达但不能用"）
- 超时复用现有 `health_probe_timeout_s`，**不加新旋钮**
- `ASTRA_LLM_PROVIDER=none` 时**不注册探针** → 保持 M0 的 `not_configured`，现有测试自动继续通过
- **turn 里不预检 health**（TOCTOU 竞态 + 额外往返），直接调用、直接处理异常

### 2.12 Provider 不可用时的降级

**不返 5xx。返 200 + `status:"failed"` + 稳定 `error.code` + 诚实的中文回复。**

- **理由**：ADR 0006 已确立"HTTP 恒 200，严重程度只在 body"；`docs/01` §3 要求任何外部依赖不可用都降级运行
- **错误码闭集**：`llm_unavailable` / `llm_timeout` / `llm_disabled` / `llm_invalid_response` / `conversation_not_found` / `internal_error`
- **诚实回复文本由代码拥有**（不能问 LLM，LLM 正挂着），例：「我这边的模型服务连不上了（lmstudio / http://localhost:1234）。你确认一下 LM Studio 在运行，然后再说一次好吗？」——确定、可测、不编造（`docs/01` §4）。允许 `persona.errors.llm_unavailable` 覆盖
- **WS 路径**推独立事件 **`turn.failed`**（不用 `turn.completed{status:"failed"}`，客户端普遍用它收尾 UI，让它收到失败会误导）
- **用户消息仍然持久化**，助手那条以 `metadata_json.status="failed"` 落库（否则重连客户端会看到悬空的用户消息）

### 2.13 测试策略

**不引入 `pytest-asyncio`**（与 M0 一致）：async 场景用 `asyncio.run()`；WS 集成测试走**真实 uvicorn 后台线程 + 端口 0** + `websockets` 客户端 + `asyncio.wait_for` 超时。

**否决 `TestClient.websocket_connect()` 作为主路径**：它没有超时参数（CI 里卡住是挂死而不是失败），且**会掩盖 `websockets` 依赖缺失**（TestClient 自己实现 WS 协议，不经过 uvicorn）。用真实 uvicorn 是唯一能验证"生产路径真的能握手"的方式。

**新增测试文件**
```
tests/fakes.py                                   # FakeLLMProvider（脚本化响应/异常/延迟）
tests/unit/       test_ids / test_vocabulary / test_persona_loader / test_prompt_builder /
                  test_conversation_repository / test_conversation_service / test_orchestrator /
                  test_event_bus / test_llm_health_probe
tests/contract/   test_assistant_response_contract（表驱动，逐行覆盖重试矩阵）/
                  test_schema_alignment / test_event_envelope_contract
tests/integration/ conftest（真实 uvicorn fixture）/ test_chat_api / test_chat_ws /
                   test_persistence_restart / test_llm_unavailable
```

**schema 对齐机制**（ADR 0007 要求 M1 建立）：不用 `jsonschema`（为一个测试加依赖不值），而是**结构化比对**——`AssistantResponsePayload.model_json_schema()` 与 `docs/schemas/assistant_response.schema.json` 逐项比较 `properties` 键集合、`required` 集合、数值字段的 `minimum`/`maximum`。

---

## 三、目标文件树

```
personas/astra.toml                                 # 人格配置（随仓库分发）

src/astra/
├─ settings.py            # 改：+persona_path / user_id / conversation_idle_minutes
│                         #     / llm_reasoning_effort / llm_history_max_* / ws_send_timeout_s
├─ cli.py                 # 改：+`astra chat`（WS REPL / --once）；serve 传 ws ping 参数
├─ domain/
│  ├─ ids.py              # 新：ULID（stdlib）+ 可注入时钟/随机源
│  ├─ errors.py           # 新：ErrorCode 闭集 + 三个 LLM 异常
│  ├─ llm.py              # 新：LLMRequest/LLMResponse/LLMMessage/LLMUsage/JsonSchemaFormat/ReasoningEffort
│  ├─ response.py         # 新：AssistantResponse/Emotion/Action/Voice（frozen dataclass，4 字段）
│  ├─ vocabulary.py       # 新：EMOTIONS/ACTIONS/VOICE_STYLES + normalize_* 纯函数
│  ├─ persona.py          # 新：Persona/Identity/Communication 值对象
│  ├─ prompt.py           # 新：8 层 system prompt 组装（层 4/5/7 省略）
│  ├─ events.py           # 新：EventType 全量登记 + EventEnvelope + 事件工厂
│  ├─ conversation.py     # 新：Conversation/Message/Role
│  ├─ ports.py            # 改：+LLMProvider Protocol
│  └─ health.py           # 不变
├─ application/
│  ├─ event_bus.py        # 新：EventBus + Subscription（有界队列 + 淘汰最旧 + 去重）
│  ├─ assistant_schema.py # 新：Pydantic 契约模型 + build_response_format()
│  ├─ response_parser.py  # 新：六段流水线
│  ├─ conversation_service.py  # 新：resolve_or_create / append_turn（幂等）/ load_history
│  ├─ prompt_builder.py   # 新：Persona + 历史 → LLMRequest
│  ├─ orchestrator.py     # 新：turn 生命周期 + 3 个显式空桩 + 每会话锁
│  └─ health_service.py   # 不变
├─ api/
│  ├─ app.py              # 改：装配 provider/探针/bus/orchestrator/connection_manager
│  ├─ schemas.py          # 新：HTTP 请求响应 Pydantic 模型
│  ├─ connection_manager.py    # 新：WS 连接注册表 + 每连接 sender task
│  └─ routes/
│     ├─ health.py        # 不变
│     ├─ config.py        # 新：GET /v1/config（含词表）
│     ├─ chat.py          # 新：POST /v1/chat/turns、POST /v1/chat/messages
│     ├─ conversations.py # 新：GET /v1/conversations/{id}/messages
│     └─ chat_ws.py       # 新：WS /ws/v1/session/{session_id}
├─ infrastructure/
│  ├─ db/
│  │  ├─ conversation_repository.py              # 新：conversations/messages 读写
│  │  └─ migrations/0002_message_turn_index.sql  # 新：idx_messages_turn（支撑幂等查询）
│  └─ persona/toml_loader.py                     # 新：tomllib（二进制模式）→ domain.Persona
└─ providers/lmstudio/
   ├─ client.py          # 新：LMStudioProvider（httpx.AsyncClient，唯一允许用 httpx 的层）
   ├─ health.py          # 新：LMStudioHealthProbe
   └─ errors.py          # 新：httpx 异常 → domain.errors 映射
```

---

## 四、M1a / M1b 拆分与验收

### M1a — 文本大脑核心（无事件总线、无 WS）

**验收标准**
```
1. uv run pytest -q                        全绿（M0 96 条不回归 + 新增）
2. ruff check / ruff format --check / mypy src   全绿
3. POST /v1/chat/turns → 200：status=ok、reply 非空、
   emotion.primary ∈ 词表、voice.pitch ∈ [-1,1]、有 turn_id 与 conversation_id
4. GET /v1/conversations/{id}/messages → user+assistant 两条，按 id 升序
5. /health：LM Studio 在跑 → llm=ok、overall=ok；
   关掉 → llm=down、overall=degraded、HTTP 仍 200
6. 关掉 LM Studio 时 POST /v1/chat/turns → 200 + status=failed +
   error.code=llm_unavailable + 非空诚实中文 reply
7. 重启 serve 后 GET 历史，两条消息仍在
8. 桩账本测试断言 stubs_used == {retrieve_memory, apply_state_delta, enqueue_memory_extraction}
```

### M1b — 事件总线 + WebSocket 通道

**验收标准**
```
1. uv sync && uv run pytest -q              全绿
2. POST /v1/chat/messages 在 <100ms 内返回 {turn_id, conversation_id, status:"accepted"}
3. astra chat --once "你好呀" 依次打印：
   turn.started → conversation.message.created → assistant.text.completed
   → avatar.expression.set → avatar.action.play → turn.completed
4. 每个 envelope 含且仅含 6 个必备字段；occurred_at 带时区；
   correlation_id 同 turn 内恒定、跨 turn 不同；event_id != correlation_id
5. 同一 session 两个连接都收到同一批事件
6. 背压：一个连上就不读的客户端 + 一个正常客户端 → 正常客户端仍完整收到 turn.completed，
   turn 总耗时不受影响
7. 客户端发非法帧 → 收到 error.protocol，连接不关闭
8. 客户端断开 → 连接数回落，无异常日志，turn 照常完成落库
```

---

## 五、实施顺序

前置（Windows）：
```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

### M1a

| # | 步骤 | 验证 |
|---|---|---|
| 1 | `domain/ids.py` + `tests/unit/test_ids.py` | `pytest tests/unit/test_ids.py -q` |
| 2 | `domain/errors.py`、`domain/vocabulary.py` + tests | `pytest tests/unit/test_vocabulary.py -q` |
| 3 | `domain/llm.py`、`domain/ports.py`(+LLMProvider)、`domain/conversation.py` | `mypy src` + `pytest tests/unit/test_architecture_rules.py -q` |
| 4 | `domain/response.py`、`personas/astra.toml`、`domain/persona.py`、`infrastructure/persona/toml_loader.py` + tests | `pytest tests/unit/test_persona_loader.py -q` |
| 5 | `domain/prompt.py` 8 层 + tests | `pytest tests/unit/test_prompt_builder.py -q` |
| 6 | `domain/events.py`（值对象）+ `application/assistant_schema.py` + `tests/contract/` 骨架 | `pytest tests/contract -q` |
| 7 | `application/response_parser.py` + 契约测试 | `pytest tests/contract/test_assistant_response_contract.py -q` |
| 8 | `0002_message_turn_index.sql`、`conversation_repository.py` + tests | `pytest tests/unit/test_migrations.py tests/unit/test_conversation_repository.py -q` |
| 9 | `application/conversation_service.py` + tests | `pytest tests/unit/test_conversation_service.py -q` |
| 10 | `application/prompt_builder.py` | `pytest tests/unit/test_prompt_builder.py -q` |
| 11 | `settings.py` + `.env.example` **同步改** | `pytest tests/unit/test_settings.py -q` |
| 12 | `providers/lmstudio/{errors,client,health}.py` + 探针测试（`httpx.MockTransport`） | `pytest tests/unit/test_llm_health_probe.py -q` |
| 13 | `application/orchestrator.py` + `tests/fakes.py` + tests | `pytest tests/unit/test_orchestrator.py -q` |
| 14 | `api/schemas.py`、`routes/{config,chat,conversations}.py`、`api/app.py` 装配 | `pytest tests/integration -q` |
| 15 | 三个集成测试 | `pytest -q` |
| 16 | ADR 0010–0015 落盘 + README 更新 | 人工检查 |

### M1b

| # | 步骤 | 验证 |
|---|---|---|
| 17 | `pyproject.toml` 加 `websockets`，`uv sync` | `python -c "import websockets"` |
| 18 | `domain/events.py` 补全 `EventType` + `application/event_bus.py` + tests | `pytest tests/unit/test_event_bus.py -q` |
| 19 | orchestrator 改为发布事件（含 `avatar.*`）+ envelope 契约测试 | `pytest tests/contract -q` |
| 20 | `api/connection_manager.py`、`routes/chat_ws.py`、`routes/chat.py` 加 202 端点 | `pytest tests/integration/test_chat_ws.py -q` |
| 21 | `tests/integration/conftest.py`（真实 uvicorn + 端口 0）+ WS 集成测试 | `pytest -q` |
| 22 | `cli.py` 加 `astra chat`；serve 传 ws ping 参数 | 手工 |
| 23 | ADR 0016 + README | 人工检查 |

---

## 六、依赖变更

| 包 | 变更 | 为什么 stdlib 不能替代 |
|---|---|---|
| `httpx>=0.28.1` | **dev → runtime** | `urllib.request` 纯阻塞必须每次 `to_thread`；`urlopen(timeout=)` 是 connect+read 合并超时；P1 流式要手拆 `http.client`。**已在 `uv.lock`，零新增包** |
| `websockets>=15` | **runtime 新增** | **实测 `websockets` 与 `wsproto` 均未安装**，uvicorn 0.53 基础依赖只有 click + h11 → `/ws/...` 在生产路径上根本握不上手。stdlib 没有任何 WS 实现。选它而非 `uvicorn[standard]`：后者额外拉 httptools/watchfiles/uvloop/python-dotenv，其中 uvloop 在 Windows 上不支持 |

**明确否决**：`pytest-asyncio`（用 `asyncio.run()`）、`jsonschema`（结构化比对足够）、`python-ulid`（35 行自实现）、`tiktoken`（qwen 词表不对）、`orjson`、`tenacity`（只重试 1 次，手写更透明）、`structlog`（M0 已有 JsonFormatter）。

---

## 七、新增 ADR

| 编号 | 结论 |
|---|---|
| **0010** | 运行时 HTTP 客户端用 httpx，且 **httpx 只允许出现在 `astra/providers/**`**；provider 专有参数只出现在 adapter，`domain` 只暴露中立的 `LLMRequest`/`LLMResponse` |
| **0011** | M1 只发送 4 字段 profile（实测 15.2s→8.5s）；`strict:true` **不保证**数值边界（实测 `pitch=200`），因此未知枚举降级、越界 clamp、缺字段补默认、多余键丢弃一律不算失败，只有「不可解析 / reply 空 / provider 不可达」才失败并重试一次 |
| **0012** | 所有 ID 统一为 stdlib ULID（实测本机时钟分辨率 15.6ms，`created_at` 无法排序——**更正 M0 ADR 中"顺序由 created_at 保证"的说法**）；`correlation_id` = 触发该工作流的 `turn_id`；`session` 是传输层临时概念不落库；`conversation` 空闲 60 分钟后自动 end |
| **0013** | 进程内 async pub/sub，每订阅者有界队列满时**淘汰最旧**并计数，`publish` 永不 await 消费者；慢消费者只影响自己的 WS 连接，绝不阻塞 turn |
| **0014** | LLM 不可用**不返 5xx**（沿用 ADR 0006），返回 `status:"failed"` + 稳定 `error.code` + 代码拥有的诚实中文回复；WS 推 `turn.failed`；用户消息仍持久化；health 探针可达即 ok、模型未加载即 degraded、不可达即 down |
| **0015** | 人格用 stdlib `tomllib` 从 `personas/*.toml` 加载并在 `create_app()` 期校验（缺文件快速失败）；情绪/动作/风格白名单留在 `domain/vocabulary.py` 并通过 `GET /v1/config` 分发，避免与 Unity 侧产生第二份真相 |
| **0016** | 新增 `POST /v1/chat/turns`（同步）、`GET /v1/conversations/{id}/messages`、`GET /v1/config`；`POST /v1/chat/messages` 响应体加 `conversation_id`；事件类型加 `turn.failed` 与 `error.protocol`；`docs/04` 同步更新 |

---

## 八、风险点

1. **`websockets` 未安装（最高优先）**——已实测。`/ws/...` 在生产路径直接失败，而 `TestClient` 会**掩盖**它。所以 WS 集成测试必须跑真实 uvicorn。
2. **`reasoning_content` 吃光 token**——表现为 `content` 空 + `finish_reason=="length"`。必须：默认 `reasoning_effort="none"`；把"content 空"当**可重试失败**；用 `reasoning_tokens` 做可观测性。**绝不把 `reasoning_content` 放进 `extra`**——`SENSITIVE_KEYS` 里没有 `reasoning`。加测试断言 `"reasoning" not in EXTRA_ALLOWLIST` 锁死。
3. **`strict:true` 不约束数值边界**——实测 `pitch=200`。clamp 是必需逻辑，必须有测试覆盖。
4. **模型在自由字符串字段里写中文**——实测 `emotion.primary="欢迎"`。Layer 8 必须给英文词表清单，否则情绪全被降级成 neutral，人格表达直接消失。
5. **`json.loads` 默认接受 NaN/Infinity**——已实测。必须 `parse_constant=` 拦掉 + `math.isfinite` 兜底。
6. **Windows 时钟粒度 15.6ms**——已实测 3000 次调用只有 1 个唯一值。**排序一律 `ORDER BY id`**。
7. **httpx 没有"总超时"**——`timeout=` 是各阶段共用默认值，不是总预算。正确做法：`httpx.Timeout(connect=..., read=llm_request_timeout_s, write=..., pool=...)` + 外层 `asyncio.wait_for` 兜总预算。这里最容易写出"设了 120s 却 10 分钟不返回"。
8. **`asyncio.Queue` 满时 `put_nowait` 抛 `QueueFull`**——不显式捕获就会从 `publish` 冒出去把 turn 打挂。这正是"一个卡住的 WS 客户端拖垮 turn"的成因。
9. **`httpx` 必须被限制在 `providers/`**——现有 AST 门禁只扫 `domain/`，要扩到"`api/`、`application/` 不得 import httpx"。
10. **`extra="forbid"` + 新增 Settings 字段**——必须同步改 `.env.example`，否则 `Copy-Item .env.example .env` 后启动直接失败（M0 已踩过）。M4/M5 的预留键继续保持注释状态。
11. **`/health` 契约不可改**——注册 LLM 探针只改 `components.llm.status` 的**值**；`test_llm_stt_tts_are_not_configured` 依赖默认 `provider=none`。
12. **`GET /v1/config` 的 `stt_provider`/`tts_provider`/`avatar_enabled` 不要新增 Settings 字段**——路由里写成常量并注释"M4/M5 接入时改为读 Settings"。提前加未使用字段违反"不做投机抽象"，还牵连 `.env.example`。
13. **`tomllib` 必须二进制模式**——`open(path, "rb")`，文本模式直接报错。
14. **每会话串行锁的代价**——一次长 LLM 调用阻塞同会话下一轮。M1 单用户可接受，写进 ADR 和 README，M4 做 interruption 时重新评估。
15. **客户端断开时不取消 LLM 请求**——M1 不取消（turn 照常完成落库）。真取消会把 `CancelledError` 传播路径引进来，那是 M4 的活。但 `ConnectionManager` 必须保证"向已关闭连接发送"不抛异常打断 sender 循环。
16. **`astra chat` 与 `astra serve` 同时跑会争 SQLite**——WAL + `busy_timeout=5000` 可接受，README 要提醒。
17. **prompt 里不要贴整段 JSON Schema**——token 成本 + 9B 更容易跑偏；硬约束交给 `response_format`。
18. **`avatar.expression.set` 的 emotion 必须是归一化后的值**——原始 LLM 字符串绝不能流到 Unity（`docs/04` §5 明文要求）。
19. **新增集成测试必须用 `tmp_path`**——`tests/conftest.py` 的 `_guard_default_database` 会在 session 结束时断言默认库未被创建/改写。
20. **`llm_max_tokens` 降到 400**——4 字段实测只用 ~90-115 tok，1024 会让最坏延迟无谓翻倍。

---

## 九、端到端验收命令

### M1a
```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
# 先启动 LM Studio 并加载 qwen/qwen3.5-9b
uv run astra serve

# 另开终端
$body = '{"content":"你好，今天陪我聊聊天吧。","input_type":"text"}'
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
     -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$r | ConvertTo-Json -Depth 8
Invoke-RestMethod "http://127.0.0.1:8000/v1/conversations/$($r.conversation_id)/messages" |
  ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 6

# 负向：关掉 LM Studio 后重跑 → /health 应 llm=down / overall=degraded / HTTP 200
#       /v1/chat/turns 应 status=failed / error.code=llm_unavailable / reply 非空中文
```

### M1b
```powershell
uv run astra serve
uv run astra chat                    # 交互 REPL
uv run astra chat --once "你好呀"     # 打印完整事件序列
# 验证 202 语义（应 <100ms 返回）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/messages `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"input_type":"text","content":"在吗"}')) | ConvertTo-Json
```
