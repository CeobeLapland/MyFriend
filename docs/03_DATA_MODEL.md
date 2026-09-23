# 03 Data Model

## 1. 数据原则

SQLite 是事实来源（source of truth）。Vector index 只是 memory retrieval 的索引，不可以成为唯一数据源。

所有可删除的数据必须有稳定 ID；不要依赖数组下标。

推荐 UUID/ULID 字符串。

## 2. 核心实体

### User

```text
id
name
locale
timezone
created_at
updated_at
```

### Companion

```text
id
name
version
persona_version
voice_profile_id
avatar_profile_id
created_at
updated_at
```

### Conversation

```text
id
user_id
companion_id
started_at
ended_at
summary
status
```

### Message

```text
id
conversation_id
turn_id
role              # user / assistant / system / tool
content
metadata_json
created_at
```

### Memory

```text
id
user_id
companion_id
type              # semantic / episodic / preference / relationship / event
content
summary
importance       # 0..1
confidence       # 0..1
source_message_id
source_event_id
created_at
last_accessed_at
expires_at
archived_at
metadata_json
```

### MemoryEmbedding

```text
memory_id
embedding_model
embedding_dim
vector_ref / vector_blob
created_at
```

### EmotionState

```text
companion_id
happiness
energy
curiosity
social_need
stress
embarrassment
excitement
updated_at
```

值域固定为 `0..1`。不要允许任意文本覆盖状态。

### RelationshipState

```text
companion_id
familiarity
trust
affection
reliance
conflict
updated_at
```

同样是 `0..1`。

### PersonaTrait

```text
companion_id
trait_name
value
min_value
max_value
source              # config / event / manual
updated_at
```

### Event

```text
event_id
type
version
occurred_at
actor
payload_json
correlation_id
```

### ToolExecution

```text
id
conversation_id
tool_name
tool_version
arguments_json
result_json
risk_level
confirmed_by_user
status
started_at
finished_at
```

## 3. SQLite 示例 DDL

```sql
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  companion_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
  content TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  companion_id TEXT NOT NULL,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  summary TEXT,
  importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  source_message_id TEXT,
  source_event_id TEXT,
  created_at TEXT NOT NULL,
  last_accessed_at TEXT,
  expires_at TEXT,
  archived_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_messages_conversation_created
  ON messages(conversation_id, created_at);

CREATE INDEX idx_memories_companion_type
  ON memories(companion_id, type);

CREATE INDEX idx_memories_importance
  ON memories(importance DESC);
```

## 4. 不把“好感度”当作唯一关系模型

Relationship 是多个维度的状态。禁止代码里出现：

```python
relationship = 73
```

应该使用：

```python
relationship = RelationshipState(
    familiarity=0.73,
    trust=0.64,
    affection=0.51,
    reliance=0.32,
    conflict=0.06,
)
```

## 5. 状态更新

状态变化应来自事件，而不是 LLM 自己直接写数据库。

例如：

```text
USER_PRAISED_COMPANION
    -> happiness +0.05
    -> affection +0.02

USER_RETURNED_AFTER_LONG_ABSENCE
    -> happiness +0.08
    -> familiarity +0.01

TASK_COMPLETED_TOGETHER
    -> trust +0.03
```

LLM 可以建议 state_delta，但必须经过 State Manager 的规则校验。
