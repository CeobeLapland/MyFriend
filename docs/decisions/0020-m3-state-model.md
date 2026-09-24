# ADR 0020 — M3 State Model

## Decision

### 1. **不把 `state_delta` 加回 schema**

ADR 0011 说 `state_delta` 在 M3 加回。M3 **没有加**。

情绪增量改由两条**零 token 成本**的来源推导：

- **用户侧事件规则**（`docs/03` §5）：被称赞 / 被批评 / 久别重逢。
- **已表达的情绪**：模型每轮已经产出 `emotion.primary` + `emotion.intensity`，
  把它翻译成各维度的**目标水平**。

### 2. **事件规则也必须是比例推进**，不是加法

情绪有两条输入，**两条都必须是收敛的**：

```
事件规则:  v += s * (极值 - v)          # s = 0.20，极值是 0.0 或 1.0
表达情绪:  v += p * (目标 - v)          # p = 0.15，目标由 intensity 限定
```

**第一版不是这样写的，实机验收把它打回来了。** 第一版按 `docs/03` §5 的推荐值
把事件规则做成**加法**（`happiness +0.05`），结果：

- 每轮净变化 = `d - p*(h - T)`，均衡点在 `T + d/p`
- 代入 `d=0.05`、`p=0.15`、`T=0.80` → **1.13**，被 clamp 到 1.0
- 实机连夸 8 轮：`0.62 → 0.68 → 0.73 → 0.77 → 0.81 → 0.84 → 0.86 → 0.89 → 0.92`，
  **一路冲向 1.0 并会钉死在那里**

状态被钉死意味着它**不再携带信息**——"她永远是满值开心"。改成比例推进后，
规则本身也收敛，均衡点变成 `(p*T + s) / (p + s)`（≈ 0.91），**恒小于 1**。

**关系仍用加法**：那是**累积**语义，涨到 1.0 是应该的（`docs/03` §5 的
`affection +0.02` 逐字保留）。

回归测试：`test_state.py::test_push_never_reaches_the_extreme`、
`test_state_service.py::test_repeated_praise_does_not_saturate`（20 轮）、
`test_praise_gets_weaker_as_happiness_approaches_the_top`。

### 3. 情绪衰减，**关系不衰减**

- 情绪按半衰期（默认 120 分钟）拉向 `EMOTION_BASELINE`。
- 关系**只累加、不衰减**：`docs/06` §7 把它归入 L3 长期状态，
  "好感度随时间淡忘"不是这个产品的语义。
- 关系**没有增量时不刷新它的时间戳**——否则"最近更新"会撒谎。

### 4. 惰性衰减：读时计算，不起调度器

DB 里存的是"最后一次显式应用的状态"这个**锚点**，有效值 = f(锚点, now)。
读**不落库**，所以读多少次都不会重复衰减；写的时候用有效值加增量再落库，
锚点随之推进。不需要后台任务（`docs/01` §4.1 的同一精神）。

### 5. 事件名用 `docs/04` §4 的具体名

- 新增 `state.emotion.updated` / `state.relationship.updated`。
- **移除 `assistant.state.updated`**：它从 M1b 起就登记在 `EventType` 里但**从未被
  发布过**，而 `docs/04` §4 已经给了更具体的名字。留着是死登记。
- 事件带**新值 + 本轮增量 + 命中的规则名**，Unity 端才能既显示当前状态、
  又驱动瞬时反应。
- 事件用**该 turn 的** `correlation_id`（ADR 0012：一个 turn 的每个事件共用它）。
  没有 turn 上下文时（调试路径）回落到 `state:{companion_id}`。

### 6. 新增只读 `GET /v1/state`

`docs/04` §2 没有定义状态端点，但三处都需要它：M3 验收要能"看见" happiness 上升、
`docs/01` P1 要"角色状态 UI"、开发者控制台需要读取点。

**只读，没有写入端点**：`docs/03` §5 明文要求"状态变化应来自事件，而不是 LLM
自己直接写数据库"。

### 7. 只有 2 个字段进 `Settings`

`state_emotion_decay_half_life_minutes`（120）与 `state_emotion_pull`（0.15）。
基线、事件规则增量、情绪目标表都是 `domain/state.py` 的模块常量——
与 `docs/06` §3 的 importance 权重同样处理：**语义规则不是运维旋钮**。

### 8. 未做

- `state.activity.updated`：`docs/03` §2 没有 activity 实体，也没有验收项。
- `PersonaTrait`（`docs/03` §2）：`docs/08` 阶段 3 没列它，当前没有消费者。
- 人格 TOML 驱动的情绪基线：更贴合 `docs/02` §2.3，但 M3 用模块常量够了。

## Reason

**为什么不为 `state_delta` 付延迟**：实测（qwen3.5-9b / reasoning=none / median of 3）：

| 变体 | 每轮延迟 |
|---|---|
| 5 字段（M2 现状） | 8.39s |
| 6 字段（+ `state_delta` 7 个数） | **10.70s（+2.3s）** |

M2 已经为记忆付了 +2.4s。再付 2.3s 会把一轮推到 ~13s，而**换来的信息我们已经有**：
`emotion.primary` + `intensity` 每轮都在输出，它就是"这轮对话的情绪落点"。
让模型再复述一遍 7 个数字，是把已有的信息买第二次。

**为什么事件的语义比"让 LLM 写状态"更对**：`docs/03` §5 的第一句就是
"状态变化应来自事件，而不是 LLM 自己直接写数据库"。事件规则**确定、可单测、
可复现**；LLM 的数值既随机又要 clamp（M1 的 `pitch=200`、M2 的 `importance=10`
都是同一类问题）。

**为什么"比例推进"是硬要求**：这是本设计里唯一会**发散**的地方，而且第一版真的
发散了——见上面 §2 的实机数据。加法 + 比例靠拢在参数合适时看着没问题，
但均衡点 `T + d/p` 与"看起来合理"的数值只差一点点。比例推进把收敛性写进了
数学形式，不依赖调参运气。

## Consequences

- `STUB_STAGES` 变成**空集**——M1 起留下的三个空桩全部填完。
  「桩账本」测试**保留**，语义从"哪些是桩"变成"**还有没有桩**"（M6 还会响）。
- 每轮多两条潜在事件（`state.emotion.updated` / `state.relationship.updated`），
  只在**实际变化**时发——状态没变就不发，否则 WS 上全是噪声。
- M2 检索公式里恒为 0 的 `relationship` 分项**有值了**：
  `MemoryService.retrieve(..., relationship_relevance=...)`，
  由 Orchestrator 从 `RelationshipState.relevance()`（familiarity / trust / affection
  的均值）算出后传入。`MemoryService` 仍然不认识状态模块。
- Layer 4 渲染**全部 5 个关系维度**，而 `docs/05` §4 的示例只列了 3 个。
  示例是"格式建议"；`conflict` 不告诉模型的话，"吵过一架之后语气该怎么变"
  就没有依据了。
- **`_apply_state_delta` 的名字与实现有差异**：名字沿用 `docs/02` §2.2 的流水线
  命名，但它不消费 LLM 的 `state_delta` 字段（那个字段没加回）。差异记在这里。
- **半衰期 120 分钟缺乏依据**——它是手感值，需要 `docs/10` §5 的固定对话集来调参。
  不假装它是调好的。
- **事件规则是词面匹配**：`"你真是个小天才"` 这类变体会漏判。好在状态增量是
  **小 nudge**，漏判的代价远低于"写错记忆"（M2 的 no-store 是同类局限，
  但后果更重）。边界写进 README。
- 状态是**每个 companion 一行**，会被不同会话的 turn 同时读写。整个
  read-modify-write 在一把专用 `asyncio.Lock` 里，避免丢更新。
