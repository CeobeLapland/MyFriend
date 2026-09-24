# ADR 0011 — Structured Output: M1 Profile and Normalization Policy

## Decision

### 1. M1 只发送 4 字段 profile

`{reply, emotion, action, voice}`，`additionalProperties: false`，`strict: true`。
`docs/schemas/assistant_response.schema.json` **保留 7 字段的完整定义**作为对外
契约描述，并注明 M1 只发送 profile 子集；`memory_candidates`（M2）、
`state_delta`（M3）、`tool_calls`（M6）在各自里程碑加回。

### 2. 字符串字段不加 `enum` 约束

改为「软约束（Layer 8 里写英文词表清单）+ 硬保证（`domain/vocabulary.py` 归一化）」。
加了 `enum` 会让归一化变成死代码却仍需维护，且词表演进（M5 加表情）要重测 prompt 契约。

### 3. 数值字段不加 `minimum`/`maximum`

范围由 `domain/response.py` 的常量 + `response_parser` 的 clamp 强制。

### 4. 重试矩阵

| 情况 | 处置 |
|---|---|
| 连接失败 / HTTP 非 2xx | **不重试**，立即 `llm_unavailable` |
| 超时（`ReadTimeout` 等） | **重试 1 次** |
| `content` 为空 | **重试 1 次并强制 `reasoning_effort=none`** |
| JSON 解析失败 / `reply` 为空 | **重试 1 次**（追加修复指令），仍失败则 turn 失败 |
| 未知枚举 / 越界数值 / 缺字段 / 多余键 / 结构错误 | 降级，**不重试** |

## Reason

**为什么裁 profile**：实测对照（两边 `reasoning_tokens` 均为 0）：

| 变体 | 延迟 | completion_tokens |
|---|---|---|
| 完整 7 字段 | 15.2s / 14.0s | 249 / 216 |
| 裁剪 4 字段 | **8.5s / 8.5s** | 115 / 91 |

M1 对另外三个字段没有任何消费者，生成即丢弃，却多付 1.7x 延迟。

**为什么不加 enum**：实测模型会在自由字符串字段里写中文
（`emotion.primary="欢迎"`、`action.name="闲聊模式"`）。只靠代码归一化会让
几乎所有情绪被降级成 neutral，人格表达直接消失；所以词表必须写进 prompt。
但反过来，加了 `enum` 之后归一化就成了永远不会触发的死代码。

**为什么不加 min/max**：实测 `strict: true` 的 JSON Schema **不约束**数值边界——
观察到 `voice.pitch` 返回 `200`（schema 限 `[-1, 1]`）。所以 clamp 是**必需逻辑**，
不是防御性代码。

**为什么 `content` 为空要重试**：这是"思考吃光 token 预算"的签名
（`finish_reason=length` + `content=""` + `reasoning_tokens>0`）。重试必须同时
把 `reasoning_effort` 压到 `none`，否则会以同样的方式失败第二次。

## Consequences

- `llm_max_tokens` 默认从 1024 降到 **400**（4 字段实测只用 ~90-115 token），
  给最坏延迟一个上界。
- `AssistantResponse` 是 4 字段的 frozen dataclass，**数值范围由
  `__post_init__` 强制**——实测模型真的返回过 `pitch=200`，领域类型不该能表示
  非法值。词表成员资格则由 parser 负责（它有明确的降级策略与日志）。
- 契约测试 `tests/contract/test_assistant_response_contract.py` 逐行覆盖上面
  的重试矩阵；`test_schema_alignment.py` 保证代码 profile 与文档契约不漂移。
- **与计划的偏差**：`apply_defaults` 返回 `(数据, 被整段替换的字段名)`，
  区分"字段缺失（补默认，不算降级）"与"字段存在但结构不对（算降级）"。
  后者若被静默替换，数据丢失就不可观测了。
