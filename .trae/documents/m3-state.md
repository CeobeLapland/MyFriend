# M3 State — 实施计划

## Context

M2（记忆）已交付并真机验收通过。当前质量门：**795 tests passed / ruff check+format 全绿 / mypy --strict 全绿**。

现在实现 **M3「状态」**——`docs/08_IMPLEMENTATION_PLAN.md` 阶段 3：Emotion State、Relationship State、State transition rules、Decay、State snapshot。

**验收标准（文档原文）**：
```
user praises companion
 -> happiness increases
 -> assistant response style changes
 -> avatar event can reflect the new state
```

**已由用户拍板**（本轮）：
- **情绪增量走「事件驱动 + 复用 `emotion` 字段」**：不把 `state_delta` 加回 schema。
  增量来自 (a) 用户消息的事件规则，(b) 模型**已经免费产出**的
  `emotion.primary` / `intensity`。
- **EmotionState 与 RelationshipState 都做**（`docs/01` §2 把 Relationship State 标为
  P1，但用户确认一并做；顺带能填上 M2 检索公式里那个恒为 0 的 `relationship` 分项）。

**M1 留下的最后一个空桩**：`_apply_state_delta`。M3 填掉它，
`STUB_STAGES` 变成**空集**，`test_orchestrator.py::test_stub_ledger` 会因此变红——
那是设计好的信号，必须显式改成"没有空桩了"，并让这条测试继续守着"不许再偷偷留桩"。

---

## 一、已实测确认的环境事实

| # | 事实 | 影响 |
|---|---|---|
| 1 | **把 `state_delta` 加回 schema 的代价**：5 字段 median **8.39s** ↔ 6 字段（+7 个数）median **10.70s**，即 **+2.3s/轮**（qwen3.5-9b / reasoning=none / median of 3） | 不采用。M2 已加了 +2.4s，再加一轮会到 ~13s |
| 2 | 模型**每轮都已经产出** `emotion.primary`（离散词）+ `emotion.intensity`（0..1） | 这是免费的"这轮对话的情绪落点"，可直接作为情绪状态的目标水平 |
| 3 | `docs/03` §2 的 `EmotionState` 有 7 维、`RelationshipState` 有 5 维；`docs/schemas` 的 `state_delta` 恰好就是那 7 个情绪维度 | 领域模型照抄，不自造维度 |
| 4 | `docs/01` §2：Emotion State = **P0**，Relationship State = **P1** | 用户已确认两者都做 |
| 5 | `EventType.ASSISTANT_STATE_UPDATED` 已登记但**从未发布**；`docs/04` §4 推荐的具体名是 `state.emotion.updated` / `state.relationship.updated` | 移除前者，新增后两者（协议变更，记 ADR） |
| 6 | `docs/03` §5 的状态规则全部是**用户侧事件**（被称赞 / 久别重逢 / 共同完成任务） | 事件规则的方向有文档依据；具体数值是"建议值"，作为模块常量 |

---

## 二、技术选型结论

### 2.1 领域层（`domain/state.py`，新）

```
EMOTION_DIMS       = (happiness, energy, curiosity, social_need, stress, embarrassment, excitement)
RELATIONSHIP_DIMS  = (familiarity, trust, affection, reliance, conflict)   # docs/03 §2

EmotionState(frozen)      : 7 个 float，值域 [0,1]
RelationshipState(frozen) : 5 个 float，值域 [0,1]
StateSnapshot(frozen)     : emotion, relationship, updated_at
```

- **值域由 `__post_init__` 强制**（与 `domain/response.py` / `domain/memory.py` 同一模式）：
  越界即 `ValueError`。docs/03 §2 明文要求"值域固定为 0..1。不要允许任意文本覆盖状态"。
- 用 `Mapping[str, float]` 还是 7 个具名字段？**用具名字段**——`docs/03` §2 逐字段列了它们，
  具名字段能在类型层面防止拼错维度名，也让 `dataclasses.replace` 的用法一目了然。
  同时提供 `as_dict()` 供事件 payload 与 prompt 渲染使用。

### 2.2 衰减：**情绪衰减，关系不衰减**

- **情绪**：按半衰期拉向 `EMOTION_BASELINE`（模块常量，见 §2.5）。
- **关系**：**不衰减**。`docs/06` §7 把 relationship 归入 L3（长期状态），
  "好感度会随时间淡忘"不是这个产品的语义——它应该只由事件推动、长期累积。
- **惰性衰减**：在**读快照时**按 `now - updated_at` 计算，不引入调度器
  （`docs/01` §4.1 "不要每个动作都调用 LLM" 的同一精神：能算就别起后台任务）。

### 2.3 增量来源：两条，都是零 token 成本

**(a) 用户消息的事件规则**（`docs/03` §5 的推荐值，落为模块常量表）：

```
被称赞（谢谢你 / 你真棒 / 厉害 …）  -> happiness +0.05, affection +0.02
被批评（你错了 / 真没用 / 太笨 …）  -> happiness -0.06, stress +0.05, conflict +0.03
久别重逢（距上次对话 > N 小时）      -> happiness +0.08, familiarity +0.01
```

**(b) 已表达情绪定义「目标水平」，状态按比例向它靠拢**——**不是加法**：

```
target_happiness = 0.5 + intensity * 0.4          # happy@0.75 -> 0.80
delta            = pull * (target - current)      # pull ≈ 0.15
```

**为什么必须是"比例靠拢"而不是加法**：加法会形成
"模型说开心 → 更开心 → prompt 更开心 → 模型更说开心"的正反馈，
在衰减半衰期较长时会把值**顶到 1.0 并钉住**（粗算：+0.03/轮 vs 衰减 0.003/轮，
均衡点在 >1，被 clamp）。比例靠拢天然收敛到目标值，且目标本身由
`intensity` 限定在 0.5–0.9 区间，不会饱和。

### 2.4 `StateService`（`application/state_service.py`，新）

```
load_snapshot()                       -> StateSnapshot（含惰性衰减，无记录则用 baseline）
apply_turn(*, user_text, emotion)     -> StateSnapshot（事件规则 + 情绪靠拢 + 衰减 → 落库 + 发事件）
snapshot()                            -> StateSnapshot（只读，给 API / 控制台）
reset()                               -> None（把状态恢复到 baseline，调试用）
```

- **并发**：状态是**每个 companion 一行**，会被不同会话的 turn 同时读写。用一个
  **专用的 `asyncio.Lock`** 把整个 read-modify-write 包起来，写入时再 clamp 一次。
  单进程假设见 ADR 0002。
- **失败即降级**：状态读写失败只记 `event="state.failed"`，**绝不让 turn 失败**
  （与记忆子系统同一模式，ADR 0014）。

### 2.5 基线常量（`EMOTION_BASELINE`）

`domain/state.py` 的模块常量，初值取"平静但有点好奇"：

```
happiness 0.55  energy 0.60  curiosity 0.65  social_need 0.45
stress 0.15     embarrassment 0.10  excitement 0.35
```

**为什么不进 `Settings`**：与 `docs/06` §3 的 importance 权重同样的处理——它是
"角色的性格底色"，不是运维旋钮；放代码里只有一处真相，也不让已经 ~47 个字段的
Settings 再涨 7 个。**人格 TOML 驱动的基线**是更贴合 `docs/02` §2.3 的做法，留作后续。

### 2.6 Prompt 第 4 层（改 `domain/prompt.py`）

- `build_system_prompt(persona, *, state=None, memories=(), ...)`：
  在 **Speech style 之后、Relevant memories 之前**插入 `CURRENT STATE` 层
  （`docs/05` §1 的层序）。
- 渲染格式照 `docs/05` §4（Mood 七维 + Relationship 三维 + 当前活动）。
- **空则整段省略**（沿用既有约定）。
- **`docs/05` §4 的硬约束已经在 Layer 0 里**："不要在回复里直接说出你的内部状态数值"——
  这条 M1 就写好了，M3 只是让状态真的存在。
- **确定性**：维度按固定顺序渲染，同输入逐字节相同。

### 2.7 事件（改 `domain/events.py`）

- 新增 `STATE_EMOTION_UPDATED = "state.emotion.updated"`、
  `STATE_RELATIONSHIP_UPDATED = "state.relationship.updated"`（`docs/04` §4）。
- **移除 `ASSISTANT_STATE_UPDATED`**：它从未被发布过，而 `docs/04` §4 已经给了更具体的
  名字。留着是死登记。`docs/04` §3 的泛化名由 §4 取代（记 ADR）。
- payload 带**新值 + 本轮增量**，Unity 端才能既显示当前状态、又驱动瞬时反应。
- **`state.activity.updated` 不做**：`docs/03` §2 没有 activity 实体，也没有验收项。
  记进 ADR 的"未做"。

### 2.8 迁移 `0004_state.sql`

```sql
CREATE TABLE emotion_states (
  companion_id TEXT PRIMARY KEY,
  happiness REAL NOT NULL CHECK (happiness >= 0 AND happiness <= 1),
  ... 7 维，全部带 CHECK
  updated_at TEXT NOT NULL
);

CREATE TABLE relationship_states (
  companion_id TEXT PRIMARY KEY,
  familiarity REAL NOT NULL CHECK (...),
  ... 5 维
  updated_at TEXT NOT NULL
);
```

- **不改 0001–0003**（checksum 会报错）。
- CHECK 约束是**纵深防御**：真正的强制点在 `__post_init__` 与 `StateService`。
- `PersonaTrait`（`docs/03` §2）**不做**：`docs/08` 阶段 3 没列它，也没有消费者。

### 2.9 API（`api/routes/state.py`，新）

| 端点 | 语义 |
|---|---|
| `GET /v1/state` | 只读快照：emotion / relationship / updated_at |

`docs/04` §2 没有定义状态端点，但：
- 验收要能"看见" happiness 上升；
- `docs/01` P1 明确要"角色状态 UI（情绪、当前行为、设备连接状态）"；
- 开发者控制台需要一个读取点。

所以新增一个**只读**端点（不做写入——状态只能由事件驱动，`docs/03` §5 的硬约束）。
记 ADR。

### 2.10 填上 M2 的 `relationship` 分项

`MemoryService.retrieve` 增加 `relationship_relevance: float = 0.0` 参数；
`MemoryService._score` 用它替换现在硬编码的 `0.0`。

- **谁算这个值**：`Orchestrator` 从快照算（`RelationshipState.relevance()` =
  `mean(familiarity, trust, affection)`），再传进 `retrieve`。
  这样 `MemoryService` 仍然不认识状态模块。
- 参数默认 `0.0`，所以不传时的行为与 M2 完全一致（现有测试不受影响）。

### 2.11 Settings（新增 2 个字段）

```
# ── State（M3）──────────────────────────────────────
state_emotion_decay_half_life_minutes: int = Field(default=120, gt=0)
state_emotion_pull: float = Field(default=0.15, ge=0.0, le=1.0)
```

- 只加这两个：其余（基线、事件规则增量）是模块常量（§2.2 / §2.3 已说明理由）。
- `.env.example` **必须同步改**（`extra="forbid"` 下不同步会直接启动失败）。
- 新日志 extra 字段加进 `logging_setup.EXTRA_ALLOWLIST`。

### 2.12 Orchestrator 填桩（改 `application/orchestrator.py`）

| 步骤 | M3 落点 |
|---|---|
| `_load_state()` | 新增。读快照（含惰性衰减），供 Layer 4 与 memory 的 relationship 分项 |
| `_build_context(...)` | 多收一个 `state=` 参数，填进 Layer 4 |
| `_apply_state_delta(outcome, user_text=...)` | **填掉空桩**：事件规则 + 情绪靠拢 + 衰减 → 落库 + 发事件 |

- **顺序不能错**：快照必须在 `ask_llm` **之前**读（Layer 4 要用），
  在 `validate_response` **之后**写（要用到 `emotion`）。这与 `docs/02` §2.2 的
  流水线顺序一致。
- **写状态排在消息事件之后**（与记忆提取同样处理）：回复要尽快到达用户，
  状态落库是事后记账。
- `STUB_STAGES` → **`frozenset()`**；`TurnResult.stubs_used` 恒为空。
  `test_stub_ledger` 改成断言"空集"，并**保留**这条测试作为"不许再留桩"的守门人。
- 方法名保留 `_apply_state_delta`（对齐流水线命名），但它现在**不消费 LLM 的
  `state_delta`**（那个字段没加回）——ADR 0020 里写明这个命名与实现的差异。

### 2.13 开发者控制台（改 `api/static/dev.html`）

加「状态」面板：7 维情绪 + 5 维关系的条形显示、`updated_at`、刷新按钮。
**保持零构建、零 npm、零新增 Python 依赖**（ADR 0017）。

### 2.14 测试策略

沿用既有约定：**不引入 `pytest-asyncio`**（async 用 `asyncio.run()`）；
集成测试用 `tmp_path`。

```
tests/unit/    test_state（领域不变量 / 衰减 / 靠拢收敛 / 事件规则）/ test_state_service
               （并发 / 降级 / reset）/ test_state_repository（原子写 + clamp）
tests/integration/ test_state_api（GET /v1/state + 控制台端点）
                   test_state_acceptance（称赞 → happiness 上升 → prompt 里出现状态 → 事件序列）
```

- **衰减与靠拢必须用性质测试**，而不是只测一个数：
  - 反复施加同一情绪 → 值收敛到目标附近，**不越过 1.0**（正反馈回归测试）；
  - 时间推进若干半衰期 → 值回到 baseline 附近；
  - 极端输入（intensity=1.0、连续 50 轮）→ 仍然在 [0,1] 且不发散。
- `test_orchestrator` 的桩账本改成空集；另加"状态失败不影响 turn 成功"的用例。

---

## 三、目标文件树

```
src/astra/
├─ settings.py                    # 改：+state_emotion_decay_half_life_minutes / state_emotion_pull
├─ logging_setup.py               # 改：EXTRA_ALLOWLIST += dimension
├─ domain/
│  ├─ state.py                    # 新：EmotionState / RelationshipState / StateSnapshot /
│  │                              #     EMOTION_DIMS / RELATIONSHIP_DIMS / EMOTION_BASELINE /
│  │                              #     TRANSITION_RULES
│  ├─ ports.py                    # 改：+StateStore Protocol
│  ├─ events.py                   # 改：+STATE_EMOTION_UPDATED / STATE_RELATIONSHIP_UPDATED；
│  │                              #     移除未使用的 ASSISTANT_STATE_UPDATED
│  └─ prompt.py                   # 改：+Layer 4 CURRENT STATE
├─ application/
│  ├─ state_service.py            # 新：快照 / 衰减 / 事件规则 / 情绪靠拢 / 专用锁
│  ├─ prompt_builder.py           # 改：build_conversation_context(..., state=...)
│  ├─ memory_service.py           # 改：retrieve(..., relationship_relevance=0.0)
│  └─ orchestrator.py             # 改：+_load_state；填 _apply_state_delta；STUB_STAGES → 空集
├─ api/
│  ├─ app.py                      # 改：装配 StateStore / StateService
│  ├─ schemas.py                  # 改：+StateBody
│  ├─ static/dev.html             # 改：+状态面板
│  └─ routes/state.py             # 新：GET /v1/state
├─ infrastructure/db/
│  ├─ state_repository.py         # 新：SQLiteStateStore
│  └─ migrations/0004_state.sql   # 新

docs/decisions/0020-m3-state-model.md   # 新
```

---

## 四、实施顺序

前置（Windows）：
```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

| # | 步骤 | 验证 |
|---|---|---|
| 1 | `0004_state.sql` + 迁移测试（含 CHECK 约束） | `pytest tests/unit/test_migrations.py -q` |
| 2 | `domain/state.py`（不变量 / 基线 / 规则表）+ tests | `pytest tests/unit/test_state.py -q` |
| 3 | `domain/ports.py`(+StateStore)；`test_architecture_rules` 不破 | `mypy src` + `pytest tests/unit/test_architecture_rules.py -q` |
| 4 | `infrastructure/db/state_repository.py` + tests | `pytest tests/unit/test_state_repository.py -q` |
| 5 | `application/state_service.py`（衰减 / 靠拢 / 事件规则 / 锁）+ tests | `pytest tests/unit/test_state_service.py -q` |
| 6 | `domain/events.py` 事件增删 + envelope 契约测试 | `pytest tests/contract -q` |
| 7 | `domain/prompt.py` Layer 4 + `prompt_builder.py` + tests | `pytest tests/unit/test_prompt_builder.py -q` |
| 8 | `settings.py` + `.env.example` **同步改** | `pytest tests/unit/test_settings.py -q` |
| 9 | `memory_service.py` 的 `relationship_relevance` 参数 + tests | `pytest tests/unit/test_memory_service.py -q` |
| 10 | `orchestrator.py` 填桩 + 桩账本改空集 + 失败隔离测试 | `pytest tests/unit/test_orchestrator.py -q` |
| 11 | `api/schemas.py` + `routes/state.py` + `app.py` 装配 | `pytest tests/integration -q` |
| 12 | 集成测试：`GET /v1/state` + 验收场景 | `pytest -q` |
| 13 | `dev.html` 状态面板 | 手工 |
| 14 | ADR 0020 + docs/04 §3/§4 对齐 + docs/08 阶段 3 落点 + README | 人工检查 |
| 15 | 真机端到端验收 | 见 §六 |

---

## 五、依赖变更

**无新增依赖。** 状态是纯 Python + 既有 SQLite 仓储。

---

## 六、验收标准与命令

### 自动化

```
1. uv run pytest -q                                    全绿（M2 795 条不回归 + 新增）
2. ruff check / ruff format --check / mypy src          全绿
3. 桩账本测试断言 stubs_used == frozenset()（没有空桩了）
4. 性质测试：连续 50 轮同一情绪 → 值收敛且不越过 1.0；推进若干半衰期 → 回到 baseline
5. 并发测试：两个会话同时跑 turn → 状态更新不丢失（专用锁生效）
6. test_orchestrator：状态子系统抛异常时 turn 仍 status=ok 且回复非空
7. test_state_acceptance：称赞 → happiness 上升 → prompt 的 CURRENT STATE 里出现它
   → 事件序列含 state.emotion.updated
```

### 真机端到端（验收 4 步）

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
uv run astra serve

# 0) 记下初始状态
Invoke-RestMethod http://127.0.0.1:8000/v1/state | ConvertTo-Json -Depth 4

# 1) 称赞她（可连说几次，让效果明显）
$b='{"content":"谢谢你，你真的太厉害了，我很喜欢你。"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b))

# 2) happiness 应上升（对比第 0 步）
Invoke-RestMethod http://127.0.0.1:8000/v1/state | ConvertTo-Json -Depth 4

# 3) 回复风格应随之变化 —— 看 /dev 的原始事件流里：
#    state.emotion.updated（带新值与增量）与 avatar.expression.set

# 4) 隔一段时间再问一次：情绪应已向 baseline 回落（惰性衰减）
```

---

## 七、新增 ADR

| 编号 | 结论 |
|---|---|
| **0020** | **M3 状态模型**：(1) **不把 `state_delta` 加回 schema**（实测 +2.3s/轮），情绪增量改由"用户事件规则 + 复用已有的 `emotion` 字段"推导；(2) 情绪**比例靠拢**目标水平而非加法，避免正反馈顶到 1.0；(3) 情绪衰减、**关系不衰减**（`docs/06` §7 把它归为长期状态）；(4) 惰性衰减（读时计算），不起调度器；(5) 移除从未发布的 `assistant.state.updated`，改用 `docs/04` §4 的 `state.*`；(6) 新增只读 `GET /v1/state`；(7) 基线/事件规则是模块常量，只有 2 个字段进 Settings；(8) 未做：`state.activity.updated`、`PersonaTrait` |

---

## 八、风险点

1. **正反馈把情绪顶到 1.0 并钉住**——这是本设计最需要防的失败模式。缓解：**比例靠拢**
   而非加法 + 上下界 + 衰减；并有专门的性质测试（连续 50 轮同一情绪必须收敛不饱和）。
2. **状态并发写**——状态是每个 companion 一行，会被不同会话的 turn 同时读写。
   缓解：专用 `asyncio.Lock` 包住 read-modify-write，写入时再 clamp。
3. **顺序依赖**：快照必须在 `ask_llm` 前读（Layer 4），在 `validate_response` 后写
   （要用 `emotion`）。写反了会"用上一轮的状态回答这一轮"，且很难看出来。
4. **移除 `ASSISTANT_STATE_UPDATED` 是协议变更**——`docs/04` §3 与 §4 本来就并存两个名字。
   取 §4 的具体名，ADR 记录，并同步 `docs/04`。
5. **Layer 4 注入会改变 prompt**，可能影响模型行为。M1/M2 记录的"编造记忆"那条
   需要重新评估（README 的"已知限制"里已有这条，M3 要重测）。
6. **事件规则是词面匹配**（与 M2 的 no-store 同类局限）——"你真是个小天才"这类变体
   会漏判。好在状态增量是**小nudge**，漏判的代价远低于"写错记忆"，所以可接受；
   边界写进 README。
7. **衰减半衰期的取值缺乏依据**——先给 120 分钟并标注"需要评测调参"
   （`docs/10` §5 的固定对话集）。不要假装它是调好的。
8. **状态失败不能让 turn 失败**——与记忆同一降级模式；必须有测试锁住。
9. **迁移 0004 不能改 0001–0003**（checksum 会报错）。
10. **新增 Settings 字段必须同步 `.env.example`**，否则 `Copy-Item .env.example .env`
    后启动直接失败（M0/M1/M2 都踩过）。
11. **新日志 extra 字段要进 `EXTRA_ALLOWLIST`**，否则日志里看不到（M0 的 schema 闸门）。
12. **`stubs_used` 变空集后，`TurnResult.stubs_used` 这个字段是否还要留**——
    建议**保留**：它是"流水线还有没有未实现阶段"的机读信号，M6 还有 `tool_calls`。
    测试改成断言空集，语义从"哪些是桩"变成"还有没有桩"。
13. **新增集成测试必须用 `tmp_path`**——`tests/conftest.py::_guard_default_database`
    会在 session 结束断言默认库未被创建/改写。

---

## 九、待用户确认的点

1. 情绪**衰减半衰期默认 120 分钟**是否可接受？（纯属手感，随时可调）
2. 事件规则的**具体数值**采用 `docs/03` §5 的推荐值（happiness ±0.05~0.08、
   affection +0.02、conflict +0.03）——是否认可？
3. `GET /v1/state` 是 `docs/04` 里没有的新端点（只读、无写入），
   理由是验收与控制台都需要一个读取点。是否同意加？
