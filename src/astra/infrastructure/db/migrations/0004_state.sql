-- 0004_state.sql
--
-- M3「状态」的存储层（docs/03_DATA_MODEL.md §2 的 EmotionState / RelationshipState）。
--
-- 两张表都是**每个 companion 一行**（companion_id 做主键）：状态是角色的属性，
-- 不是每段会话的属性。并发读写由 StateService 的专用锁串行化（单进程，ADR 0002）。
--
-- CHECK 约束是**纵深防御**：真正的强制点在 domain/state.py 的 __post_init__ 与
-- StateService 的写入路径。docs/03 §2 明文要求"值域固定为 0..1。不要允许任意文本
-- 覆盖状态"——DB 层再拦一次，坏数据就进不来。
--
-- 为什么 relationship_states 没有衰减相关字段：关系**不衰减**（docs/06 §7 把它归入
-- 长期状态）。情绪的衰减是**惰性**的（读时按 updated_at 计算），也不需要额外列。
--
-- 不做 PersonaTrait（docs/03 §2）：docs/08 阶段 3 没列它，当前也没有消费者。
--
-- 约束：本文件禁止出现 BEGIN / COMMIT / ROLLBACK / SAVEPOINT。
--       事务由 migrations.py 的 runner 负责。

CREATE TABLE emotion_states (
  companion_id  TEXT PRIMARY KEY,
  happiness     REAL NOT NULL CHECK (happiness     >= 0.0 AND happiness     <= 1.0),
  energy        REAL NOT NULL CHECK (energy        >= 0.0 AND energy        <= 1.0),
  curiosity     REAL NOT NULL CHECK (curiosity     >= 0.0 AND curiosity     <= 1.0),
  social_need   REAL NOT NULL CHECK (social_need   >= 0.0 AND social_need   <= 1.0),
  stress        REAL NOT NULL CHECK (stress        >= 0.0 AND stress        <= 1.0),
  embarrassment REAL NOT NULL CHECK (embarrassment >= 0.0 AND embarrassment <= 1.0),
  excitement    REAL NOT NULL CHECK (excitement    >= 0.0 AND excitement    <= 1.0),
  updated_at    TEXT NOT NULL
);

CREATE TABLE relationship_states (
  companion_id TEXT PRIMARY KEY,
  familiarity  REAL NOT NULL CHECK (familiarity >= 0.0 AND familiarity <= 1.0),
  trust        REAL NOT NULL CHECK (trust       >= 0.0 AND trust       <= 1.0),
  affection    REAL NOT NULL CHECK (affection   >= 0.0 AND affection   <= 1.0),
  reliance     REAL NOT NULL CHECK (reliance    >= 0.0 AND reliance    <= 1.0),
  conflict     REAL NOT NULL CHECK (conflict    >= 0.0 AND conflict    <= 1.0),
  updated_at   TEXT NOT NULL
);
