# M2 Memory — 实施计划

## Context

M1（文本大脑 + 事件总线 + 开发者控制台）已交付并验收通过。当前质量门：**556 tests passed / ruff check+format 全绿 / mypy --strict 全绿**。

现在实现 **M2「记忆」**——`docs/08_IMPLEMENTATION_PLAN.md` 阶段 2：Memory candidate extractor、Importance scoring、Embedding adapter、sqlite-vec integration、Retrieval、Memory management API。

**验收标准（文档原文）**：
```
1. 第一次告诉 AI 一个稳定偏好
2. 关闭程序
3. 重新启动
4. 新会话中能在相关语境检索出来
5. 删除后不再检索到
```

**已由用户拍板**（本轮）：
- **提取走「内联·精简」**：主 schema 加 `memory_candidates`，但每条只带 `{type, content}`；`importance` / `confidence` 由**代码**按 `docs/06` §3 的加权启发式打分。
- **范围 = 验收必需 + TTL**：提取 → 打分 → 去重/合并 → embedding → sqlite-vec 检索 → 记忆管理 API → no-store 流程 → TTL 过期/归档。**不做** `docs/06` §7 的每日 consolidation。

**M1 留下的三个空桩**：`_retrieve_memory` / `_apply_state_delta` / `_enqueue_memory_extraction`。M2 填掉前两个的**两个**（`retrieve_memory` + `enqueue_memory_extraction`），`apply_state_delta` 留给 M3。`test_orchestrator.py::test_stub_ledger` 会因此变红——**那是设计好的信号**，必须显式把账本改成 `{"apply_state_delta"}`。

---

## 一、已实测确认的环境事实

这些是本机实测结果（本轮跑过），**直接决定下面的设计**：

| # | 事实 | 影响 |
|---|---|---|
| 1 | `sqlite-vec` **0.1.9 在 Windows + Python 3.12 可安装、可加载**：`sqlite_vec.loadable_path()` → `...\site-packages\sqlite_vec\vec0`（无 `.dll` 后缀也能加载），`vec_version()` = `v0.1.9` | 采用 sqlite-vec，无需备选方案 |
| 2 | vec0 **支持文本主键**：`create virtual table v using vec0(memory_id text primary key, embedding float[768])`，KNN 与 `delete ... where memory_id = ?` 均正常 | 可直接用 `memory_id`（ULID）做主键，无需 rowid 映射表 |
| 3 | vec0 的建表 DDL **可在 `executescript` 的 `BEGIN;...COMMIT;` 里执行**（迁移 runner 就是这么干的），会连带创建 `_info`/`_chunks`/`_rowids`/`_vector_chunks00` 影子表 | 迁移 0003 可直接建 vec0 表 |
| 4 | LM Studio `/v1/embeddings` 可用：`text-embedding-nomic-embed-text-v1.5` → **768 维**，标准 OpenAI 格式 `{data:[{embedding:[...]}], model, usage}` | embedding adapter 走同一 `llm_base_url` |
| 5 | 该端点的 `usage.prompt_tokens/total_tokens` **恒为 0** | 不能依赖 usage 做观测，用字符数代替 |
| 6 | **内联候选的延迟代价**（qwen3.5-9b / reasoning=none / 同一 prompt，median of 3）：4 字段 **7.7s** ↔ +精简候选 `{type,content}` **10.2s（+2.4s）** ↔ +完整候选 5 字段 **12.2s（+4.5s）** | 选**精简候选**：省 2.1s，且 importance 本来就不该让模型自评 |
| 7 | 模型会给出**越界数值**（本轮实测 `importance: 10`，schema 限 `0..1`）——与 M1 的 `pitch=200` 同源 | 任何数值字段都必须 clamp |
| 8 | 独立提取调用（同模型，专用 prompt）约 **3.5s**；换 `nemotron-3-nano-4b` **冷启动 15.7s**（要换模型加载） | 若将来改后台提取，仍应复用同一模型，不要换小模型 |

---

## 二、技术选型结论

### 2.1 领域层：记忆值对象（`domain/memory.py`，新）

```
MemoryType(StrEnum): semantic | episodic | preference | relationship | event
    # 取自 docs/03 §2 的 type 注释。docs/schemas 里示例用的 goal/fact 不采用——
    # 那个文件自己标注了「只是预设示例」，而 docs/03 是数据模型规格。

Memory(frozen): id, user_id, companion_id, type, content, summary,
                importance, confidence, source_message_id, source_event_id,
                created_at, last_accessed_at, expires_at, archived_at, metadata
    # 字段与 docs/03 §2 的 Memory 实体逐一对应

MemoryCandidate(frozen): type: MemoryType, content: str
    # LLM 只产出这两个字段（实测决定，见 §一 #6）

MemoryHit(frozen): memory: Memory, score: float, components: dict[str, float]
    # components 保留各分项，供开发者控制台/日志观测检索为何命中
```

- **数值范围由 `__post_init__` 强制**（与 `domain/response.py` 同一模式）：`importance` / `confidence` ∈ [0,1]，越界即 `ValueError`。
- **TTL 策略是模块常量**，不进 `Settings`：
  ```
  MEMORY_TTL_DAYS: dict[MemoryType, int | None] = {
      episodic: 7, event: 30, semantic: None, preference: None, relationship: None,
  }
  ```
  理由：这是**语义规则**（`docs/06` §8 说 episodic 短期、semantic 长期），不是运维旋钮；放代码里只有一处真相，也避免再往 Settings 塞 5 个字段。

### 2.2 领域端口（`domain/ports.py`，改）

```python
@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...
    @property
    def dim(self) -> int: ...
    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

class MemoryStore(Protocol):
    async def add(self, memory: Memory, embedding: Sequence[float]) -> None: ...
    async def get(self, memory_id: str) -> Memory | None: ...
    async def list(...) -> tuple[Memory, ...]: ...
    async def update(self, memory: Memory) -> None: ...
    async def delete(self, memory_id: str) -> bool: ...      # 事实 + embedding + 向量索引
    async def delete_all(self) -> int: ...
    async def touch_accessed(self, memory_ids: Sequence[str], *, at: datetime) -> None: ...
    async def search_similar(self, vector, *, limit: int, exclude_ids=()) -> tuple[tuple[str, float], ...]: ...
    async def count(self) -> int: ...
```

- **`MemoryStore` 同时管事实表与向量索引**：它们在同一 SQLite 文件里，拆成两个端口只会让"删除必须两边都删"这件事失去单一入口（`docs/04` §2 / `docs/11` §4 都要求删除时同步处理向量索引）。
- **`search_similar` 只返回 `(memory_id, distance)`**，重排序在 `MemoryService` 里做——KNN 拿不到 importance / recency，而 `docs/06` §4 的公式需要它们。
- 沿用 `runtime_checkable` + `async def` 契约（实现内部同步、入口 `to_thread`）的既有约定。

### 2.3 结构化输出：加回 `memory_candidates`（改 `assistant_schema.py` / `response_parser.py` / `domain/response.py`）

- **发送的 profile 变成 5 字段**：`{reply, emotion, action, voice, memory_candidates}`。
  - `memory_candidates`: `array` of `{type: string, content: string}`，`additionalProperties:false`。
  - **不加 `enum`**：沿用 ADR 0011 的理由（加了会让归一化变死代码）；`type` 的合法性由 `domain/vocabulary.normalize_memory_type` 兜底。
  - **不给 importance/confidence 字段**：实测模型给过 `10`，而且 `docs/06` §3 的公式本来就该由代码算。
- `AssistantResponse` 加字段：`memory_candidates: tuple[MemoryCandidate, ...] = ()`。
- **`prune_unknown_keys` 必须扩展**：现在的实现只处理「值是 dict 的嵌套模型」，遇到**对象数组**会整段保留，然后被 `extra="forbid"` 判为校验失败 → **可重试**失败。必须支持"数组里每个元素按 item 模型剪枝"。这是本轮最容易漏的一处（M1 没有数组字段，所以没暴露）。
- `_SECTION_DEFAULTS` 加 `"memory_candidates": []`（缺失 → 空列表，**不算降级**）。
- `_to_domain` 里：归一化 type、丢掉 `content` 为空的候选、按 `content` 去重、按 `Settings.memory_max_candidates_per_turn` 截断（超限记一条 WARNING）。
- **注意**：`docs/schemas/assistant_response.schema.json` 保留 7 字段完整定义不动；`test_schema_alignment.py` 改为比对「当前 profile 子集」，并新增一条断言锁住"M2 profile = 5 字段"。

### 2.4 记忆管理服务（`application/memory_service.py`，新）

这是 `docs/02` §2.4 的 **Memory Manager**，检索与写入的唯一入口。

**写入（`ingest_candidates`）**：
```
if 记忆被禁用 or 本轮命中 no-store 短语: return ()
for candidate in candidates:
    importance = score_importance(candidate, user_text, repetition)
    confidence = score_confidence(candidate, user_text)
    vector = embed(candidate.content)
    hit = store.search_similar(vector, limit=1)          # 去重（docs/06 §6）
    if hit and similarity(hit) > memory_dedup_threshold:
        merge: 更新 importance/confidence（取较大）、repetition+1、保留 provenance、touch updated_at
        发布 memory.updated
    else:
        create Memory(id=new_id(), expires_at=now+TTL[type], ...)
        发布 memory.created
```
- **合并而非新建**：`docs/06` §6 明文要求"相似度超阈值 → merge，更新 confidence/importance，保留 provenance"。
- **`repetition` 存在 `metadata_json`**（不新增列）：它只影响 importance 启发式，不是需要独立查询的字段。

**检索（`retrieve`）**：
```
if 记忆被禁用: return ()
vector = embed(query)
candidates = store.search_similar(vector, limit=memory_candidate_limit)
memories = store.get_many(ids)
过滤掉 archived / 已过期（expires_at < now）
final_score = semantic*0.50 + importance*0.20 + recency*0.15 + access*0.05 + relationship*0.10
排序取 top_k（低于 memory_min_score 的丢弃）
touch_accessed(top_k) ; 发布 memory.retrieved
```
- 五个权重**全部来自 `Settings`**（`docs/06` §4 明文要求"放在配置中，不能硬编码在多个文件"）。
- `recency` 用 `exp(-λ·days_since_created)`；`access` 用归一化的访问次数；`relationship` 在 M3 之前恒为 0（M3 接入 `RelationshipState` 后再填）——**在代码里显式写死并注释**，不假装有数据。

**管理（`docs/06` §9 六项能力）**：`list_memories` / `get` / `update` / `delete` / `export` / `clear_all` / `set_enabled`。

**no-store 流程（`docs/06` §9）**：
- `detect_no_store(user_text)`：正则匹配「不要记住 / 别记 / 别记住 / 忘掉这件事 / 不用记」等。
- 命中 → 本轮**完全不写入**，记 `event="memory.suppressed"` 日志，**不落库任何候选**。
- **诚实边界**：这是短语匹配，不是语义理解。README 与 ADR 里必须写明"措辞变体可能漏判；用户可用 `DELETE /v1/memories/{id}` 事后删除"。

### 2.5 Prompt 第 5 层（改 `domain/prompt.py` / `application/prompt_builder.py`）

- `build_system_prompt(persona, *, memories=())`：在 **Speech style 之后、Output format 之前**插入 `RELEVANT MEMORIES` 层（`docs/05` §1 的层序）。
- 每条按 `docs/05` §5 的格式渲染：`content` / `importance` / `confidence` / `last_accessed`。
- **空则整段省略**（沿用 M1 约定：不输出空标题，否则模型以为存在相应信息）。
- **确定性**：记忆按 `final_score` 降序、同分按 `id` 升序排列，保证同输入 → 逐字节相同（`docs/10` §2）。
- Layer 8 的文案更新为 5 字段，并补 `memory_candidates` 的说明与「**不是每轮都产出候选**」规则（`docs/05` §8：值得/不值得保存的清单）。
- **顺带修一个已知问题**：M1 实测模型会在全新会话里**编造记忆**（"我正试着把刚才那几段对话的碎片拼起来看"）。Layer 5 一旦真的有内容，要重新评估这条 prompt 的遵从度，并把结论回写 README 的"已知限制"。

### 2.6 Orchestrator 填桩（改 `application/orchestrator.py`）

| 空桩 | M2 落点 |
|---|---|
| `_retrieve_memory(conversation_id, user_text)` | 调 `memory_service.retrieve(user_text)`，**返回值**改成 `tuple[Memory, ...]`，由 `_build_context` 填进 Layer 5 |
| `_enqueue_memory_extraction(outcome)` | 调 `memory_service.ingest_candidates(outcome.response.memory_candidates, ...)` |
| `_apply_state_delta` | **保持空桩**（M3） |

- **方法名保留 `_enqueue_*`**（对齐 `docs/02` §2.2 的流水线命名），但 M2 是**在 turn 内 await 完成**、不投递后台队列——这是"内联·精简"的直接推论。ADR 0018 里写明这个命名与实现的差异。
- **记忆失败绝不能让 turn 失败**：`_retrieve_memory` 与 `_enqueue_memory_extraction` 各自 `try/except Exception`，失败只记 `event="memory.failed"` + 继续。LLM 已成功，用户必须拿到回复。
- `STUB_STAGES` 改成 `frozenset({"apply_state_delta"})`；`test_stub_ledger` 同步改。
- `TurnResult` 加 `memories_created: int = 0` / `memories_retrieved: int = 0`（可观测性，供控制台显示）。
- **`_build_context` 的调用顺序**：先 `retrieve` 拿到记忆，再 `build_conversation_context(..., memories=memories)`。

### 2.7 Embedding adapter（`providers/lmstudio/embeddings.py`，新）

- `LMStudioEmbeddingProvider`：`POST {base_url}/embeddings`，`{model, input: [text, ...]}`，解析 `data[i].embedding`。
- **httpx 只允许出现在 `providers/**`**（ADR 0010）——`EmbeddingProvider` 端口在 `domain/ports.py`，实现只在这里。
- 复用 `providers/lmstudio/errors.py::translate` 与 `client.py` 的 `httpx.Timeout` + `asyncio.wait_for` 双保险模式。
- `embedding_provider = "none"` 时**不注册**、`MemoryService` 降级为"不检索、不写入"（与 `llm_provider=none` 同构）。
- **失败即降级**：embedding 不可达时检索返回空、写入跳过，turn 照常。

### 2.8 SQLite 实现（`infrastructure/db/memory_repository.py`，新）+ 扩展加载（改 `connection.py`）

**迁移 `0003_memories_and_vectors.sql`**：
```sql
CREATE TABLE memory_embeddings (
  memory_id       TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  embedding_model TEXT NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  vector_blob     BLOB NOT NULL,      -- docs/03 §2 的 vector_blob：向量的**事实来源**
  created_at      TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_vectors USING vec0(
  memory_id TEXT PRIMARY KEY,
  embedding float[768]                 -- 维度硬编码：换模型需新迁移 + 重建（见风险 #1）
);

CREATE INDEX idx_memories_expires  ON memories(expires_at);
CREATE INDEX idx_memories_archived ON memories(archived_at);

CREATE TABLE app_flags (               -- 运行时可切换的开关（docs/06 §9「禁用记忆」）
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- **`memories` 表在 0001 就已建好且字段齐全**（`docs/03` §3 逐字照抄），**本迁移不改它**——改 0001 会触发 checksum 报错。
- **`vector_blob` 是事实来源，vec0 只是索引**（`docs/03` §1："SQLite 是事实来源，vector index 不可以成为唯一数据源"）。这给了索引损坏时的重建路径。
- **`connection.py` 加 `load_extensions: bool = False`**：为 `True` 时 `enable_load_extension(True)` → `sqlite_vec.load(conn)` → `enable_load_extension(False)`。
- **`migrations.py::upgrade()` 改用 `open_connection(db_path, load_extensions=True)`**：否则 0003 的 vec0 DDL 会因为扩展没加载而失败。这是对 M0 组件的必要改动，ADR 0018 里说明。
- 删除路径**必须在同一事务里**删三处：`memories`（事实）+ `memory_embeddings`（FK 级联）+ `memory_vectors`（vec0 无 FK，手动删）。这是 `docs/04` §2「必须真正删除事实数据和向量索引」的落地。
- 排序一律 `ORDER BY id`（ADR 0012）。

### 2.9 API（`api/routes/memories.py`，新）

| 端点 | 语义 |
|---|---|
| `GET /v1/memories` | 列表；支持 `type` / `min_importance` / `created_after` / `query`（语义检索）/ `archived` / `limit`（`docs/04` §2 指定的 5 个过滤条件） |
| `GET /v1/memories/{id}` | 单条 |
| `PATCH /v1/memories/{id}` | 编辑 `content` / `summary` / `importance` / `confidence` / `type`；改 `content` 时**重新 embed 并更新向量** |
| `DELETE /v1/memories/{id}` | 硬删除（事实 + embedding + 向量）；不存在 → 404 |
| `POST /v1/memories/clear` | 清空全部（返回删除条数） |
| `GET /v1/memories/export` | 导出 JSON |
| `GET /v1/memories/settings` | 读 `{enabled: bool}` |
| `PUT /v1/memories/settings` | 切换禁用/启用（写 `app_flags`，立即生效，无需重启） |

- **静态路径必须声明在 `/{memory_id}` 之前**，否则 `/export`、`/settings`、`/clear` 会被当成 id 匹配掉。
- `GET /v1/config` 增加 `memory_enabled` 字段（客户端/控制台要知道当前状态）。
- 错误码：不新增 `ErrorCode`（闭集）；未知 id 复用 404，与 `conversations` 路由同构。

### 2.10 开发者控制台（改 `api/static/dev.html`）

加一个「记忆」面板：列表（type / content / importance / confidence / expires）、单条删除、一键清空、启用/禁用开关、本轮检索命中的记忆与分项得分。

- **保持零构建、零 npm、零新增 Python 依赖**（ADR 0017）。

### 2.11 Settings（`settings.py` + `.env.example` **必须同步改**）

新增字段（17 个）：

```
# ── Embedding ────────────────────────────────────────
embedding_provider: Literal["none","lmstudio"] = "none"
embedding_base_url: str = "http://localhost:1234/v1"
embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
embedding_dim: int = 768
embedding_timeout_s: float = 30.0

# ── Memory ───────────────────────────────────────────
memory_enabled: bool = True
memory_top_k: int = 5
memory_candidate_limit: int = 20          # KNN 过采样数，重排序后再截断到 top_k
memory_max_candidates_per_turn: int = 3
memory_dedup_threshold: float = 0.90
memory_min_score: float = 0.30
memory_context_max_chars: int = 2000

# ── Retrieval 权重（docs/06 §4，五者之和应为 1.0）────
memory_weight_semantic: float = 0.50
memory_weight_importance: float = 0.20
memory_weight_recency: float = 0.15
memory_weight_access: float = 0.05
memory_weight_relationship: float = 0.10
```

- **importance 的 5 个启发式权重不进 Settings**，作为 `application/memory_scoring.py` 的模块常量（`docs/06` §3 自己说"这只是初始启发式"，而 §4 才要求"放在配置中"）。这样既满足文档，又不让 Settings 再膨胀 5 个字段。
- **注意**：Settings 字段将从 ~30 涨到 ~47，超过 `settings.py` 注释里写的"~30 个时再评估分组"阈值。M2 **保持扁平**（分组有 pydantic-settings 的 `default_factory` 歧义风险），但要在 ADR 0018 里明确记录这笔技术债，并把"分组"排到 M3/M4。
- `.env.example`：取消注释 `ASTRA_EMBEDDING_MODEL` / `ASTRA_EMBEDDING_DIM`，补齐其余键；`extra="forbid"` 下不同步会**直接启动失败**。

### 2.12 日志与事件

- `logging_setup.EXTRA_ALLOWLIST` 增加：`memory_id`、`count`、`similarity`、`memory_type`。
- `domain/events.py::EventType` 补 `MEMORY_UPDATED` / `MEMORY_ARCHIVED` / `MEMORY_DELETED`（`MEMORY_CANDIDATE_CREATED` / `MEMORY_CREATED` / `MEMORY_RETRIEVED` 已在 M1b 登记）。
- 新增的 `extra` 字段**不得包含记忆正文**——`SENSITIVE_KEYS` 里没有 `content` 之外的保护；记忆正文属于用户内容，只传 `memory_id` + `memory_type` + 长度。

### 2.13 测试策略

沿用既有约定：**不引入 `pytest-asyncio`**（async 用 `asyncio.run()`）；集成测试用 `tmp_path`（`tests/conftest.py::_guard_default_database` 会在 session 结束断言默认库未被改写）。

```
tests/fakes.py                              # 改：+FakeEmbeddingProvider（确定性伪向量）
tests/unit/    test_memory_scoring / test_memory_service / test_memory_repository /
               test_response_parser_memory（候选归一化/数组剪枝/截断）/
               test_embeddings_provider（httpx.MockTransport）/ test_no_store
tests/contract/ test_schema_alignment（改：5 字段 profile）/ test_event_envelope_contract（memory 事件）
tests/integration/ test_memory_persistence_restart（验收 5 步）/ test_memory_api（CRUD + clear + export + settings）
```

- `test_memory_repository` 直接跑真实 sqlite-vec（临时库），验证：写入 → KNN 命中 → 删除 → KNN 不再命中 → `memory_embeddings` / `memory_vectors` 都清空。
- `test_orchestrator` 的桩账本改成 `{"apply_state_delta"}`；另加"记忆失败不影响 turn 成功"的用例。

---

## 三、目标文件树

```
src/astra/
├─ settings.py                 # 改：+17 个字段（embedding / memory / retrieval 权重）
├─ logging_setup.py            # 改：EXTRA_ALLOWLIST += memory_id/count/similarity/memory_type
├─ domain/
│  ├─ memory.py                # 新：MemoryType / Memory / MemoryCandidate / MemoryHit / MEMORY_TTL_DAYS
│  ├─ ports.py                 # 改：+EmbeddingProvider / MemoryStore Protocol
│  ├─ response.py              # 改：AssistantResponse += memory_candidates
│  ├─ prompt.py                # 改：+Layer 5 RELEVANT MEMORIES；Layer 8 文案 → 5 字段
│  ├─ vocabulary.py            # 改：+MEMORY_TYPE_CHOICES / normalize_memory_type
│  └─ events.py                # 改：+MEMORY_UPDATED / MEMORY_ARCHIVED / MEMORY_DELETED
├─ application/
│  ├─ assistant_schema.py      # 改：+MemoryCandidatePayload（数组）
│  ├─ response_parser.py       # 改：数组剪枝 + 候选归一化/去重/截断
│  ├─ memory_scoring.py        # 新：importance / confidence 启发式（docs/06 §3）
│  ├─ memory_service.py        # 新：Memory Manager（写入/检索/管理/no-store）
│  ├─ prompt_builder.py        # 改：build_conversation_context(..., memories=())
│  └─ orchestrator.py          # 改：填 retrieve_memory + enqueue_memory_extraction；桩账本 → {apply_state_delta}
├─ api/
│  ├─ app.py                   # 改：装配 MemoryStore / EmbeddingProvider / MemoryService
│  ├─ schemas.py               # 改：+Memory* 请求响应模型
│  ├─ static/dev.html          # 改：+记忆面板
│  └─ routes/
│     ├─ memories.py           # 新：GET/PATCH/DELETE/clear/export/settings
│     └─ config.py             # 改：+memory_enabled
├─ infrastructure/db/
│  ├─ connection.py            # 改：+load_extensions
│  ├─ migrations.py            # 改：upgrade() 用 load_extensions=True
│  ├─ memory_repository.py     # 新：SQLiteMemoryStore（事实 + embedding + vec0）
│  └─ migrations/0003_memories_and_vectors.sql   # 新
└─ providers/lmstudio/
   └─ embeddings.py            # 新：LMStudioEmbeddingProvider

docs/decisions/0018-m2-memory-architecture.md   # 新
docs/decisions/0019-memory-management-api.md    # 新
docs/decisions/0011-structured-output-policy.md # 改：补记 M2 加回 memory_candidates（精简形态）
```

---

## 四、实施顺序

前置（Windows）：
```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

| # | 步骤 | 验证 |
|---|---|---|
| 1 | `uv add sqlite-vec`；`connection.py` 加 `load_extensions`；`migrations.py` 改 `upgrade()` | `pytest tests/unit/test_migrations.py -q` |
| 2 | `0003_memories_and_vectors.sql` + 迁移测试（含 vec0 建表/幂等/checksum） | `pytest tests/unit/test_migrations.py -q` |
| 3 | `domain/memory.py` + `domain/vocabulary.py`(+memory type) + tests | `pytest tests/unit/test_vocabulary.py -q` |
| 4 | `domain/ports.py`(+2 端口)；`test_architecture_rules` 确认不破 | `mypy src` + `pytest tests/unit/test_architecture_rules.py -q` |
| 5 | `providers/lmstudio/embeddings.py` + `FakeEmbeddingProvider` + tests | `pytest tests/unit/test_embeddings_provider.py -q` |
| 6 | `infrastructure/db/memory_repository.py` + tests（真实 sqlite-vec） | `pytest tests/unit/test_memory_repository.py -q` |
| 7 | `application/memory_scoring.py` + tests | `pytest tests/unit/test_memory_scoring.py -q` |
| 8 | `assistant_schema.py` 加候选数组；`response_parser.py` 数组剪枝 + 归一化；`domain/response.py` 加字段 | `pytest tests/contract -q` |
| 9 | `application/memory_service.py` + tests（去重/合并/no-store/禁用/TTL） | `pytest tests/unit/test_memory_service.py -q` |
| 10 | `domain/prompt.py` Layer 5 + `prompt_builder.py` + tests（确定性） | `pytest tests/unit/test_prompt_builder.py tests/unit/test_prompt_assembly.py -q` |
| 11 | `settings.py` + `.env.example` **同步改** | `pytest tests/unit/test_settings.py -q` |
| 12 | `orchestrator.py` 填两桩 + 桩账本改 + 失败隔离测试 | `pytest tests/unit/test_orchestrator.py -q` |
| 13 | `api/schemas.py` + `routes/memories.py` + `routes/config.py` + `app.py` 装配 | `pytest tests/integration -q` |
| 14 | 集成测试：重启持久化（验收 5 步）+ 管理 API | `pytest -q` |
| 15 | `dev.html` 记忆面板 | 手工 |
| 16 | ADR 0018/0019 + 更新 0011 + README（含已知限制） | 人工检查 |
| 17 | 真机端到端验收 | 见 §六 |

---

## 五、依赖变更

| 包 | 变更 | 为什么不能省 |
|---|---|---|
| `sqlite-vec>=0.1.9` | **runtime 新增** | 唯一在 Windows/Py3.12 实测可加载的向量索引；纯 Python 暴力余弦在 10k×768 维下约数秒（Python 循环），不可接受。已在 §一 #1–#3 实测确认 |

**明确否决**：`numpy`（只为一个余弦计算引入重型依赖）、`sqlite-vss`（本机未验证且依赖更重）、`sentence-transformers` / `onnxruntime`（要另起模型进程，8GB 显存不够）、`pytest-asyncio`（沿用 `asyncio.run()`）、`jsonschema`（沿用结构化比对）。

---

## 六、验收标准与命令

### 自动化

```
1. uv run pytest -q                                    全绿（M1 556 条不回归 + 新增）
2. ruff check . && ruff format --check . && mypy src    全绿
3. 桩账本测试断言 stubs_used == {"apply_state_delta"}
4. 契约测试断言 M2 profile = 5 字段，且 docs/schemas 的 7 字段定义仍在
5. test_memory_repository：写入 → KNN 命中 → 删除 → KNN 不再命中 → 三处数据都清空
6. test_memory_service：相似候选合并而非新建；no-store 短语不产生记忆；禁用时不检索不写入
7. test_orchestrator：记忆子系统抛异常时 turn 仍 status=ok 且回复非空
```

### 真机端到端（验收 5 步）

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
# LM Studio 需同时加载 qwen/qwen3.5-9b 与 text-embedding-nomic-embed-text-v1.5
$env:ASTRA_LLM_PROVIDER="lmstudio"; $env:ASTRA_EMBEDDING_PROVIDER="lmstudio"
uv run astra serve

# 1) 告诉 AI 一个稳定偏好
$b='{"content":"我特别喜欢深夜写代码，请记住这个。","input_type":"text"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b))
Invoke-RestMethod http://127.0.0.1:8000/v1/memories | ConvertTo-Json -Depth 6   # 应出现一条

# 2) Ctrl+C 关闭 → 3) 重启 serve
# 4) 新会话里相关语境能检索出来（返回体应带检索到的记忆）
$b2='{"content":"我一般什么时候写代码效率最高？","input_type":"text"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/turns `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($b2))

# 5) 删除后不再检索到
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:8000/v1/memories/$id"
# 重跑第 4 步 → 检索结果为空

# 附加：no-store
$b3='{"content":"我明天要去医院，这件事不要记住。","input_type":"text"}'
# → /v1/memories 不新增；日志出现 memory.suppressed
# 附加：禁用记忆
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8000/v1/memories/settings `
  -ContentType 'application/json' -Body '{"enabled":false}'
# → 之后既不检索也不写入
```

---

## 七、新增 / 修改 ADR

| 编号 | 结论 |
|---|---|
| **0018**（新） | **M2 记忆架构**：内联·精简候选 `{type,content}`（实测 +2.4s，完整字段 +4.5s）；importance/confidence 由代码按 `docs/06` §3 打分（实测模型给过越界值 `10`）；sqlite-vec 0.1.9 为索引、`memory_embeddings.vector_blob` 为向量事实来源（`docs/03` §1）；检索五权重入 Settings（`docs/06` §4）；TTL 策略为模块常量；`upgrade()` 需加载扩展；Settings 扁平字段已达 ~47，分组技术债排到 M3/M4 |
| **0019**（新） | **记忆管理 API 与用户控制**：`/v1/memories` 全套（查/改/删/导出/清空/禁用），删除必须同事务清事实 + embedding + 向量；no-store 为短语匹配（诚实边界写进 README）；禁用开关存 `app_flags` 运行时生效 |
| **0011**（改） | 补记：M2 把 `memory_candidates` 加回 profile，但采用**精简形态**（仅 `type`/`content`，无 importance/confidence/expires_in_days），延迟从 +4.5s 降到 +2.4s；"字段按里程碑加回"的原则不变 |

---

## 八、风险点

1. **vec0 维度硬编码 `float[768]`**——换 embedding 模型（维度变了）必须新迁移 + 重建索引。缓解：`EmbeddingProvider.dim` 在启动装配期与 768 比对，不一致**快速失败并给出明确中文提示**；README 记录该限制。
2. **迁移 runner 现在依赖 sqlite-vec 扩展**——`upgrade()` 改为加载扩展。若扩展加载失败，0003 会显式报错（而不是静默跳过）。这是对 M0 组件的改动，ADR 0018 记录。
3. **内联提取给每轮加 ~2.4s**——已实测并写入 ADR 0011/0018。**这是刻意的取舍**：换来"落库即持久"，直接满足验收第 2–4 步（后台提取会在"说完就关程序"时丢记忆）。
4. **`prune_unknown_keys` 不支持对象数组**——M1 没有数组字段所以没暴露。不修的话，合法的 `memory_candidates` 会让校验失败并被判为**可重试**，每轮白跑一次 LLM。必须先把剪枝扩展到数组元素，并加针对性测试。
5. **记忆失败不能拖垮 turn**——`retrieve` / `ingest` 各自 `try/except Exception`，失败只记日志。LLM 已经成功，用户必须拿到回复。必须有测试锁住。
6. **删除必须三处一起删**——`memories` + `memory_embeddings`（FK 级联）+ `memory_vectors`（vec0 无 FK，手动）。漏删向量会导致"删了还能检索到"，**直接违反验收第 5 步**。
7. **`extra="forbid"` + 新增 Settings 字段**——必须同步 `.env.example`，否则 `Copy-Item .env.example .env` 后启动直接失败（M0/M1 都踩过）。
8. **Settings 字段数超阈值**——~30 → ~47，超过 `settings.py` 注释里的分组阈值。M2 保持扁平并在 ADR 记债。
9. **no-store 是短语匹配，不是语义理解**——措辞变体可能漏判。必须在 README 写明边界，并给用户事后删除的路径。
10. **`enable_load_extension` 的安全姿态**——加载后立刻 `enable_load_extension(False)`；只加载项目自带的 sqlite-vec，绝不加载外部路径。
11. **`/v1/memories/{id}` 会吞掉静态路径**——`export` / `settings` / `clear` 必须声明在 `{memory_id}` 之前。
12. **LLM 可能每轮都吐候选**——`docs/05` §8 明确"候选不是每轮都创建"。Layer 8 要写清值得/不值得保存的清单，并在 `_to_domain` 里按 `memory_max_candidates_per_turn` 硬截断。
13. **检索层会改变 prompt，可能重新诱发"编造记忆"**——M1 已实测过该问题。Layer 5 有内容后必须重测遵从度，结论回写 README。
14. **`relationship` 分项在 M3 之前恒为 0**——显式写死并注释，不假装有数据；权重仍进配置，M3 接入后无需改结构。
15. **新增集成测试必须用 `tmp_path`**——`tests/conftest.py::_guard_default_database` 会在 session 结束断言默认库未被创建/改写。
16. **`test_schema_alignment.py` 会因 profile 变化变红**——这是设计好的信号，需显式改为"比对当前 5 字段 profile"，而不是放宽断言。
17. **LM Studio 同时加载 chat 模型与 embedding 模型**——8GB 显存下可能装不下两个。缓解：`/health` 的 `llm` 探针已能反映 chat 模型是否加载；embedding 失败时降级为"不检索不写入"并记日志，不影响聊天。README 记录显存建议。

---

## 九、待用户确认的点

1. 本计划的**范围**（验收必需 + TTL，不做 consolidation）是否符合预期？
2. `docs/03` 的 `type` 词表（`semantic/episodic/preference/relationship/event`）与 `docs/schemas` 示例里的 `goal/fact` 不一致——计划**采用 `docs/03`**（它是数据模型规格，schema 文件自标"只是预设示例"）。是否认可？
3. Settings 新增 17 个字段、总数达 ~47，超过注释里的分组阈值——**M2 保持扁平、把分组排到 M3/M4**，是否可接受？
