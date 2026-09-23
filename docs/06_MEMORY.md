# 06 Memory System

## 1. Memory 的四层

```text
L0 Working Context
L1 Recent Episodes
L2 Long-term Semantic Memory
L3 Relationship / Identity State
```

### L0 Working Context

当前 turn 和最近若干轮，不需要向量数据库。

### L1 Recent Episodes

最近会话摘要、近期事件。

### L2 Long-term Memory

稳定事实、偏好、目标、重要事件。

### L3 State

人格动态参数、关系参数、当前情绪和活动。

## 2. Memory Pipeline

```text
conversation turn
      ↓
candidate extraction
      ↓
importance score
      ↓
confidence score
      ↓
dedup / merge
      ↓
embedding
      ↓
SQLite + vector index
```

## 3. Importance Score

建议初始规则：

```text
importance = weighted_sum(
  explicit_request_to_remember * 0.35,
  long_term_preference * 0.25,
  future_relevance * 0.15,
  relationship_significance * 0.15,
  repetition * 0.10
)
```

这只是初始启发式，后续可以用评测数据调参。

## 4. Retrieval Score

不要只做 cosine similarity。

推荐：

```text
final_score =
  semantic_similarity * 0.50
  + importance * 0.20
  + recency * 0.15
  + access_frequency * 0.05
  + relationship_relevance * 0.10
```

先把这些权重放在配置中，不能硬编码在多个文件。

## 5. 查询流程

用户输入：

```text
“我最近又开始熬夜写项目了。”
```

并行：

```text
current context retrieval
semantic memory search
recent episode search
relationship state read
```

然后合并为 context pack。

## 6. 去重

发现：

```text
用户喜欢深夜写代码
```

已有：

```text
用户通常在晚上写程序
```

不应创建两个互不相关的 memory。

应该：

1. similarity > threshold -> candidate merge。
2. 更新 confidence / importance。
3. 保留 provenance。

## 7. Memory Consolidation

每天或每 N 次对话执行：

```text
episodes
  ↓
summary
  ↓
merge duplicates
  ↓
promote repeated facts to semantic memory
  ↓
archive low-value episodes
```

不要删除事实数据，只是 archive；真正删除由用户操作触发。

## 8. Forgetting / Expiration

不是所有 memory 都需要永久保存。

示例：

```text
“今天我要复习数据库”
```

可以是短期 memory。

而：

```text
“我正在长期学习计算机科学”
```

通常是 semantic memory。

Memory 类型应有 TTL policy。

## 9. 用户控制

必须提供：

- 查看。
- 编辑。
- 删除。
- 导出。
- 清空全部记忆。
- 禁用记忆。

当用户说：

> “不要记住这件事。”

必须进入显式 no-store flow。
