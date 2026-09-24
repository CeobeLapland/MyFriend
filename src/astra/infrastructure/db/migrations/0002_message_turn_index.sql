-- 0002_message_turn_index.sql
--
-- 支撑两件事：
--   1. ConversationService.append_turn 的**幂等插入**——同一 (turn_id, role)
--      已存在则跳过（docs/04 §6 的事件幂等性）
--   2. 按 id 读取历史时的排序（ORDER BY id，见 ADR 0012：created_at 在同一
--      毫秒内无区分度，不能作为排序依据）
--
-- 用 (turn_id, role) 复合索引而不是只索引 turn_id：幂等查询的条件正是这两列。
--
-- 约束：本文件禁止出现 BEGIN / COMMIT / ROLLBACK / SAVEPOINT。
--       事务由 migrations.py 的 runner 负责。

CREATE INDEX idx_messages_turn_role ON messages(turn_id, role);
