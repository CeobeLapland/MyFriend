# ADR 0012 — ID and Identity Model

## Decision

- 所有 ID（`event_id` / `correlation_id` / `turn_id` / `message_id` /
  `conversation_id` / `session_id`）统一为 **stdlib 实现的 ULID 字符串**
  （`domain/ids.py`，26 字符 Crockford Base32，零依赖）。
- **排序一律 `ORDER BY id`**；`created_at` 只作展示。
- `session_id` 是**传输层临时概念，不落库**。
- `conversation_id` 是持久线程，**空闲 60 分钟后自动结束并新开一个**。
- `correlation_id` **一个 turn 一个**：在 turn 被接受时生成（入站若带了就复用），
  复制进该 turn 的每个事件、两条消息的 `metadata_json`、每条日志、`LLMRequest`。
  无 turn 上下文的事件用 `correlation_id = event_id`。
- 未知 / 已结束 / 不属于该用户的 `conversation_id` → **拒绝**，不静默新建。

## Reason

**为什么必须换掉 `uuid4`**：M0 的 ADR 里写的是"UUID4 已满足，顺序由 `created_at`
索引保证"。**这条被实测推翻了**：本机时钟分辨率是 **15.625ms**，
3000 次 `datetime.now()` 只产出 **1 个唯一值**，跨度 0.0s。同一个 turn 的 user 与
assistant 两条消息时间戳完全相同，`ORDER BY created_at` 没有区分度。

ULID 把 48-bit 毫秒时间戳编进前 10 个字符，字典序即时间序，`ORDER BY id` 才是
可靠依据。另外 `docs/04_API_AND_EVENTS.md` §1 的示例 `"event_id": "01J..."` 正是
ULID，实现与文档一致。

**为什么自己实现**：`python-ulid` 是 35 行可自测的代码换一个依赖；
`uuid.uuid7()` 要 Python 3.13（本项目锁 3.12）。升到 3.13 后可以换成它。

**为什么要单调模式**：ULID 规范允许在同一毫秒内递增随机部分。没有它，同一毫秒内
生成的两个 ID 会按随机位排序，快速连续写入和测试都会变得不确定。这是
`domain/ids.py` 里唯一的模块级状态（一个锁 + 两个整数），时钟回拨
（NTP 调整 / 虚拟机挂起）也由同一逻辑兜住。

**为什么要空闲切分**：否则所有对话永远堆在同一个 conversation 里，M2 的上下文
窗口只能机械截断；有了边界，M2 才能对已结束的会话做摘要（L1 episodes）。

**为什么拒绝未知 id**：客户端传未知 id 是 bug 或被删的数据，静默新建会掩盖它。

## Consequences

- M0 ADR 中"顺序由 `created_at` 保证"的说法**作废**，以本 ADR 为准。
- 迁移 `0002_message_turn_index.sql` 加 `(turn_id, role)` 复合索引，同时支撑
  幂等插入与按 id 读取。
- `messages` 表的 `created_at` 仍然写入，但只用于展示与"空闲判定"。
- M1 的 `ConversationService.load_history` 不加 limit（会话由空闲切分限制长度）；
  真需要时在仓储层加 `ORDER BY id DESC LIMIT n` 再反转。
