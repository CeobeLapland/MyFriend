# ADR 0015 — Persona as TOML with Code-Owned Vocabulary

## Decision

- 人格配置是 **`personas/*.toml`**（随仓库分发，**不是** `data/`——那不是用户数据）。
- 用 stdlib `tomllib` 加载（**必须二进制模式**），Pydantic 校验结构，
  转成 `domain.Persona` frozen dataclass。
- **在 `create_app()` 装配期同步加载，缺文件直接启动失败**。
- **情绪 / 动作 / 音色风格的白名单留在 `domain/vocabulary.py`**，
  通过 `GET /v1/config` 分发给客户端。

## Reason

**为什么用 TOML**：stdlib `tomllib` 零新增依赖（`docs/09_AGENT_INSTRUCTIONS.md`
要求"先检查 stdlib 是否已解决"），支持注释与多行字符串，适合写中文行为规则。
`docs/05_PERSONA_AND_PROMPTS.md` §3 的 YAML 示例描述的是**内容形状**，不是文件格式。

**为什么白名单不放 TOML**：它是**协议契约**——Unity 端要按同一份词表实现表情与
动作映射。放进 TOML 会产生两份真相（Python 一份、Unity 一份），必然漂移。
放在代码里并通过 `/v1/config` 分发，保持单一分发点。

**为什么缺文件要快速失败**：人格是配置不是外部服务。缺了它应用就没有身份，
降级启动只会得到一个"不知道自己是谁"的进程。

## Consequences

- `.env` 的 `extra="forbid"` 与人格文件的 `extra="forbid"` 一致：
  拼错的键报错，而不是静默忽略。
- `personas/astra.toml` 的 `[errors]` 段可以覆盖 ADR 0014 的降级话术。
- 改人格只需重启 `astra serve`（无热重载）。M1 不做热重载——那需要一个文件
  监听器与并发安全的状态替换，而收益只是省一次重启。
- **与计划的偏差**：`build_system_prompt(persona)` 只接受 persona 一个参数，
  **没有**预先加上 `state` / `memories` / `tools` 形参。原因：M1 还没有
  `EmotionState` / `Memory` / `ToolSpec` 这些类型，加 `object` 类型的形参等于
  接受一个被静默忽略的参数——调用方在 M2 传了 memories 却看不到效果会更困惑。
  插入点已在模块文档字符串里标明（Layer 3 与 Layer 8 之间），M2 加参数是局部改动。
