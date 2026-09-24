-- 0003_memories_and_vectors.sql
--
-- M2「记忆」的存储层（docs/03_DATA_MODEL.md §2、docs/06_MEMORY.md）。
--
-- 为什么 memories 表不在本文件里：它在 0001 就已按 docs/03 §3 的完整 DDL 建好，
-- 字段齐全（含 importance / confidence / expires_at / archived_at / metadata_json）。
-- 迁移文件一旦应用就**不可改动**（checksum 会报错），所以这里只新增。
--
-- 三个对象各自的角色：
--   1. memory_embeddings —— 向量的**事实来源**（docs/03 §2 的 vector_blob）。
--      即使向量索引损坏，也能从这里重建。docs/03 §1 明文要求：
--      "SQLite 是事实来源，vector index 不可以成为唯一数据源"。
--   2. memory_vectors    —— sqlite-vec 的 vec0 虚拟表，只是**检索索引**。
--      维度假定为 768（text-embedding-nomic-embed-text-v1.5，实测值）。
--      换模型且维度变化时必须新开迁移并重建索引（见 ADR 0018 风险项）。
--
--      **distance_metric=cosine 是刻意的**（实测 0.1.9 支持）：vec0 默认是 L2，
--      而文本 embedding 的相似度语义是余弦。用 L2 就得自己保证向量已归一化，
--      一旦上游模型换了归一化策略，去重阈值会静默失准。cosine 下
--      ``similarity = 1 - distance``，去重与重排序都直接可用。
--   3. app_flags         —— 运行时可切换的开关。docs/06 §9 要求用户能"禁用记忆"，
--      而 Settings 是启动期快照，改它要重启；这里让开关立即生效。
--
-- 约束：本文件禁止出现 BEGIN / COMMIT / ROLLBACK / SAVEPOINT。
--       事务由 migrations.py 的 runner 负责（它已加载 sqlite-vec 扩展）。

CREATE TABLE memory_embeddings (
  memory_id       TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  embedding_model TEXT NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  vector_blob     BLOB NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_vectors USING vec0(
  memory_id TEXT PRIMARY KEY,
  embedding float[768] distance_metric=cosine
);

-- 过期与归档的过滤条件（检索时排除 expires_at < now、archived_at IS NOT NULL）
CREATE INDEX idx_memories_expires ON memories(expires_at);
CREATE INDEX idx_memories_archived ON memories(archived_at);

CREATE TABLE app_flags (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
