# ADR 0018 — M2 Memory Architecture

## Decision

### 1. 记忆提取走「内联·精简候选」

主 schema 加回 `memory_candidates`（ADR 0011 承诺的"M2 加回"），但每条**只有
`type` + `content`**：

```json
"memory_candidates": [
  {"type": "preference", "content": "用户喜欢深夜写代码"}
]
```

`importance` / `confidence` **不由模型给**，改由 `application/memory_scoring.py`
按 `docs/06` §3 的加权和算。

### 2. 提取在 turn 内完成，不投递后台队列

`Orchestrator._enqueue_memory_extraction` 保留流水线命名（`docs/02` §2.2），
但**是 `await` 完成的**。它排在 `append_turn` 之后（这样候选能带上
`source_message_id`）、消息事件发布之后（回复要尽快到达用户）。

### 3. 向量索引用 sqlite-vec；`vector_blob` 是事实来源

- `memory_embeddings.vector_blob` —— 向量的**事实来源**（`docs/03` §2）。
- `memory_vectors`（vec0 虚拟表）—— 只是**检索索引**，`distance_metric=cosine`。
- 删除时在**同一事务**里清掉 `memories` + `memory_embeddings`（FK 级联）+
  `memory_vectors`（vec0 无外键，手动删）。

### 4. 检索权重进 `Settings`，importance 启发式留代码

- `docs/06` §4 明文要求"放在配置中，不能硬编码在多个文件" → 五个检索权重进
  `Settings`，并加 `model_validator` 强制**和为 1.0**（否则排序分的量纲会随配置
  漂移）。
- `docs/06` §3 只说"这是初始启发式，后续可以用评测数据调参" → 五个 importance
  权重作为 `memory_scoring.py` 的模块常量，不给 Settings 再添 5 个字段。

### 5. **准入与排序分开**：闸门用原始余弦相似度，不用加权总分

这是 M2 **实机验收时发现并修正**的一个设计错误，值得单独记一笔。

`docs/06` §4 的加权公式把"是不是同一件事"（semantic）与"重不重要 / 新不新"
（importance / recency）揉成一个分。最初我把它同时当成**准入闸门**（低于
`min_score` 就丢弃）——结果一条完全不相干的记忆，只要够重要够新，也能靠非语义项
凑过阈值被塞进 prompt。实机验收时就看到了：查询"我家的猫"，返回了两条**咖啡**
相关的记忆。

实测（`text-embedding-nomic-embed-text-v1.5`，短中文文本，8 对样本）：

```text
相关对: 0.6378 – 0.8308  (mean 0.7064)
无关对: 0.6156 – 0.7439  (mean 0.6780)
```

两组**几乎完全重叠**——模型对短中文的余弦区分度很弱，没有哪个阈值能干净切开。
所以：

1. **准入**：原始余弦相似度硬闸门 `memory_min_similarity`（默认 **0.70**）。
   宁可漏掉一条相关记忆，也不要把不相干的记忆塞进 prompt——**编造式的"想起来"
   比想不起来更伤**（`docs/01` §4）。
2. **排序**：才用 `docs/06` §4 的加权总分。

**这是一个已知的、有测量支撑的局限**：重叠意味着闸门必然同时误杀与漏放。
真正的解法是换一个中文区分度更好的 embedding 模型（需要新迁移 + 重建索引），
留待后续里程碑。README 的"已知限制"里记录了同一组数字。

### 6. TTL 策略是模块常量

`domain/memory.py::MEMORY_TTL_DAYS`：episodic 7 天、event 30 天、
semantic / preference / relationship 不过期。这是**语义规则**（`docs/06` §8），
不是运维旋钮。

### 6. `upgrade()` 加载 sqlite-vec 扩展

迁移 `0003` 会建 vec0 虚拟表，其 DDL 需要扩展已加载。扩展加载幂等且廉价，
所以 `upgrade()` 统一开启，避免"哪些迁移需要扩展"这种隐式耦合。

## Reason

**为什么候选只带两个字段**：实测（qwen3.5-9b / reasoning=none / 同一 prompt，
median of 3）：

| 变体 | 每轮延迟 |
|---|---|
| 4 字段（M1） | 7.7s |
| +精简候选 `{type,content}` | **10.2s（+2.4s）** |
| +完整候选（含 importance/confidence/expires_in_days） | 12.2s（+4.5s） |

完整形态多付约 2.1s，而模型给出的分数**还会越界**——实测 `importance: 10`
（schema 限 `0..1`），与 M1 的 `voice.pitch = 200` 同源，仍然要 clamp。
`docs/06` §3 的公式本来就是几个可判定事实的加权和（用户有没有明说"记住"、
类型是不是偏好、重复了几次），不需要语言理解。既然代码算得更准更便宜，
就不该让模型猜。

**为什么不做后台提取**：后台提取（回复后另起一次 LLM 调用，实测约 3.5s）
确实能把回复延迟压回 7.7s，但它**牺牲持久性**——用户在回复后立刻关掉程序，
提取还没跑完，记忆就丢了，而这恰好是 M2 验收的第 2–4 步
（"关闭程序 → 重启 → 仍能检索到"）。内联提取换来的是"落库即持久"。
另外，8GB 显存下换用 `nemotron-3-nano-4b` 做提取要**冷启动 15.7s**，
"换小模型就便宜了"这条捷径不成立。

**为什么索引与事实分开存**：`docs/03` §1 明文规定"SQLite 是事实来源，
vector index 不可以成为唯一数据源"。`vector_blob` 让索引损坏时有重建路径，
也满足 `docs/03` §2 的 `MemoryEmbedding` 实体定义。

**为什么用 `distance_metric=cosine`**：vec0 默认是 L2，而文本 embedding 的
相似度语义是余弦。用 L2 就得自己保证向量已归一化——一旦上游模型换了归一化
策略，去重阈值会**静默失准**。余弦下 `similarity = 1 - distance` 可直接用。
（实测 sqlite-vec 0.1.9 支持该选项。）

**为什么 `access_frequency` 存在 `metadata_json` 里**：`docs/03` 的 Memory 实体
没有这个字段，而它只服务于排序权重、不需要独立索引。仓储的 `touch_accessed`
用 `json_set` 自增（实测本机 SQLite 3.53 自带 JSON1）。

## Consequences

- `AssistantResponse` 从 4 字段变 5 字段；`docs/schemas/assistant_response.schema.json`
  **保留 7 字段完整定义**，其中候选仍是 5 字段——代码侧发的是它的**子集**
  （`test_schema_alignment.py` 显式断言这条子集关系）。
- **每轮回复多约 2.4s**。这是为持久性付的代价，写进 README 的"已知限制"。
- `prune_unknown_keys` 必须支持**对象数组**：M1 没有数组字段，所以"只按 dict
  剪枝"的写法一直没暴露；不修的话候选里的多余键会让校验失败，而那是**可重试**
  失败，等于每轮白跑一次 LLM。
- **维度 768 写死在迁移 `0003` 的 `float[768]` 里**（迁移不可改）。换 embedding
  模型且维度变化时必须新开迁移并重建索引；装配期用 `VECTOR_INDEX_DIM` 校验
  `Settings.embedding_dim`，不符则**启动即失败**。
- **`upgrade()` 现在依赖 sqlite-vec 扩展**：扩展加载失败时 `0003` 会显式报错，
  而不是静默跳过。
- **Settings 字段数从 ~30 涨到 ~47**，超过 `settings.py` 注释里"~30 个时再评估
  分组"的阈值。M2 **保持扁平**（分组要动全部读取点，风险与收益不成比例），
  把分组排到 M3/M4——**这是一笔显式记录的技术债**。
- 记忆子系统的**任何失败都不影响 turn**（ADR 0014 的同一模式）：检索失败返回
  空、写入失败返回 0，只记 `event="memory.failed"`。
- **与计划的偏差**：候选数量上限（`memory_max_candidates_per_turn`）不在
  `response_parser` 里做，而在 `MemoryService` 里——解析器只管"让数据合法"，
  上限是 `Settings` 驱动的策略，属于消费方。
