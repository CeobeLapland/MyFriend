-- 0001_init.sql
--
-- 来源：docs/03_DATA_MODEL.md §3（逐字照抄，含三个索引）。
--
-- 为什么只有这三张表：
--   本文件只包含文档给出了**完整 DDL** 的实体。其余实体（users / companions /
--   emotion_states / relationship_states / persona_traits / events /
--   tool_executions / memory_embeddings）在文档里只有字段列表，没有类型与主键定义。
--   字段列表不是 schema 契约——提前猜会制造迁移债。它们在各自里程碑（M2/M3/M6）
--   设计定型时再以新的迁移文件加入。
--
-- 约束：本文件禁止出现 BEGIN / COMMIT / ROLLBACK / SAVEPOINT。
--       事务由 migrations.py 的 runner 负责，文件内再写会导致嵌套事务报错。
--       （由 tests/unit/test_migrations.py 断言拦截）

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
