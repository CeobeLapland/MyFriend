"""记忆的 SQLite 仓储（ADR 0004 / 0018）。

**三个存储对象，一个事务边界**：

======================================  ==========================================
对象                                     角色
======================================  ==========================================
``memories``                            事实（0001 建，本模块只读写）
``memory_embeddings``                   向量的**事实来源**（``vector_blob``）
``memory_vectors``                      sqlite-vec 的 vec0 索引，**派生数据**
======================================  ==========================================

``docs/04`` §2 / ``docs/11`` §4 要求"删除记忆时必须真正删掉事实数据与向量索引"，
所以写/删都在**同一个显式事务**里同时处理三者。漏删任何一处都会让"删了还能检索到"
变成静默 bug——那正是 M2 验收第 5 步要验的东西。

**排序一律 ``ORDER BY id``**（ADR 0012：本机时钟分辨率 15.625ms，``created_at``
在同一毫秒内无区分度）。

**相似度用余弦**：``memory_vectors`` 建表时指定了 ``distance_metric=cosine``，
所以 ``distance`` 就是 ``1 - cos``，``similarity = 1 - distance`` 可直接用。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec

from astra.domain.memory import Memory, MemoryFilter, MemoryType
from astra.infrastructure.db.connection import open_connection
from astra.infrastructure.db.timestamps import from_iso as _from_iso
from astra.infrastructure.db.timestamps import to_iso as _to_iso

_COLUMNS = (
    "id",
    "user_id",
    "companion_id",
    "type",
    "content",
    "summary",
    "importance",
    "confidence",
    "source_message_id",
    "source_event_id",
    "created_at",
    "last_accessed_at",
    "expires_at",
    "archived_at",
    "metadata_json",
)
_MEMORY_COLUMNS = ", ".join(_COLUMNS)
_MEMORY_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)

#: ``memory_vectors`` 的向量维度，**必须与迁移 0003 里 ``float[768]`` 一致**。
#: 迁移一旦应用就不可改（checksum），所以换 embedding 模型且维度变化时必须新开
#: 一个迁移并重建索引。装配期会拿它校验 ``Settings.embedding_dim``（ADR 0018）。
VECTOR_INDEX_DIM = 768


class SQLiteMemoryStore:
    """:class:`astra.domain.ports.MemoryStore` 的 SQLite 实现。

    ``embedding_model`` / ``embedding_dim`` 在构造时绑定：一个库只服务一套 embedding
    配置，维度还要与 vec0 表 DDL 里写死的 ``float[768]`` 对齐（装配期校验，见 ADR 0018）。
    """

    def __init__(self, db_path: Path, *, embedding_model: str, embedding_dim: int) -> None:
        self._db_path = db_path
        self._embedding_model = embedding_model
        self._embedding_dim = embedding_dim

    # ── 写入 ────────────────────────────────────────────

    async def add(self, memory: Memory, embedding: Sequence[float]) -> None:
        await asyncio.to_thread(self._add_sync, memory, tuple(embedding))

    async def update(self, memory: Memory, embedding: Sequence[float] | None = None) -> None:
        await asyncio.to_thread(
            self._update_sync, memory, tuple(embedding) if embedding is not None else None
        )

    async def delete(self, memory_id: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, memory_id)

    async def delete_all(self, *, user_id: str, companion_id: str) -> int:
        return await asyncio.to_thread(self._delete_all_sync, user_id, companion_id)

    async def touch_accessed(self, memory_ids: Sequence[str], *, at: datetime) -> None:
        await asyncio.to_thread(self._touch_accessed_sync, tuple(memory_ids), at)

    # ── 读取 ────────────────────────────────────────────

    async def get(self, memory_id: str) -> Memory | None:
        return await asyncio.to_thread(self._get_sync, memory_id)

    async def get_many(self, memory_ids: Sequence[str]) -> tuple[Memory, ...]:
        return await asyncio.to_thread(self._get_many_sync, tuple(memory_ids))

    async def list(self, query: MemoryFilter) -> tuple[Memory, ...]:
        return await asyncio.to_thread(self._list_sync, query)

    async def count(self, *, user_id: str, companion_id: str) -> int:
        return await asyncio.to_thread(self._count_sync, user_id, companion_id)

    async def search_similar(
        self,
        vector: Sequence[float],
        *,
        user_id: str,
        companion_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        return await asyncio.to_thread(
            self._search_similar_sync, tuple(vector), user_id, companion_id, now, limit
        )

    # ── 运行时开关 ──────────────────────────────────────

    async def get_flag(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_flag_sync, key)

    async def set_flag(self, key: str, value: str, *, at: datetime) -> None:
        await asyncio.to_thread(self._set_flag_sync, key, value, at)

    # ── 同步实现 ────────────────────────────────────────

    def _add_sync(self, memory: Memory, embedding: tuple[float, ...]) -> None:
        blob = self._to_blob(embedding)

        with open_connection(self._db_path, load_extensions=True) as conn:
            conn.execute("BEGIN")
            conn.execute(
                f"INSERT INTO memories ({_MEMORY_COLUMNS}) "  # noqa: S608 - 列名是模块常量
                f"VALUES ({_MEMORY_PLACEHOLDERS})",
                _memory_values(memory),
            )
            conn.execute(
                "INSERT INTO memory_embeddings "
                "(memory_id, embedding_model, embedding_dim, vector_blob, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    memory.id,
                    self._embedding_model,
                    self._embedding_dim,
                    blob,
                    _to_iso(memory.created_at),
                ),
            )
            conn.execute(
                "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                (memory.id, blob),
            )
            conn.execute("COMMIT")

    def _update_sync(self, memory: Memory, embedding: tuple[float, ...] | None) -> None:
        if embedding is not None:
            self._require_dim(embedding)

        with open_connection(self._db_path, load_extensions=True) as conn:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE memories SET type = ?, content = ?, summary = ?, importance = ?, "
                "confidence = ?, source_message_id = ?, source_event_id = ?, "
                "last_accessed_at = ?, expires_at = ?, archived_at = ?, metadata_json = ? "
                "WHERE id = ?",
                _memory_update_values(memory),
            )
            if embedding is not None:
                blob = sqlite_vec.serialize_float32(list(embedding))
                conn.execute(
                    "UPDATE memory_embeddings SET embedding_model = ?, embedding_dim = ?, "
                    "vector_blob = ? WHERE memory_id = ?",
                    (self._embedding_model, self._embedding_dim, blob, memory.id),
                )
                # vec0 的向量列不走 UPDATE（各 sqlite-vec 版本的支持程度不一），
                # 改成先删后插——条数极少，代价可忽略，正确性是确定的。
                conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory.id,))
                conn.execute(
                    "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                    (memory.id, blob),
                )
            conn.execute("COMMIT")

    def _delete_sync(self, memory_id: str) -> bool:
        with open_connection(self._db_path, load_extensions=True) as conn:
            conn.execute("BEGIN")
            # 先删索引（vec0 无外键），再删事实（memory_embeddings 靠 ON DELETE CASCADE 清掉）
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            removed = cursor.rowcount > 0
            conn.execute("COMMIT")

        return removed

    def _delete_all_sync(self, user_id: str, companion_id: str) -> int:
        scope = (user_id, companion_id)

        with open_connection(self._db_path, load_extensions=True) as conn:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM memory_vectors WHERE memory_id IN "
                "(SELECT id FROM memories WHERE user_id = ? AND companion_id = ?)",
                scope,
            )
            cursor = conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND companion_id = ?", scope
            )
            removed = max(cursor.rowcount, 0)
            conn.execute("COMMIT")

        return removed

    def _touch_accessed_sync(self, memory_ids: tuple[str, ...], at: datetime) -> None:
        if not memory_ids:
            return

        placeholders = ", ".join("?" for _ in memory_ids)
        with open_connection(self._db_path) as conn:
            # 同时自增 metadata 里的 access_count（docs/06 §4 的 access_frequency）。
            # 用 json_set 而不是独立列：docs/03 的 Memory 实体没有这个字段，
            # 而 access_count 只服务于排序权重，不需要独立索引。
            # 实测本机 SQLite 3.53 自带 JSON1。
            conn.execute(
                f"UPDATE memories SET last_accessed_at = ?, "  # noqa: S608 - 占位符按入参生成
                "metadata_json = json_set(metadata_json, '$.access_count', "
                "COALESCE(json_extract(metadata_json, '$.access_count'), 0) + 1) "
                f"WHERE id IN ({placeholders})",
                (_to_iso(at), *memory_ids),
            )

    def _get_sync(self, memory_id: str) -> Memory | None:
        with open_connection(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE id = ?",  # noqa: S608
                (memory_id,),
            ).fetchone()

        return _row_to_memory(row) if row is not None else None

    def _get_many_sync(self, memory_ids: tuple[str, ...]) -> tuple[Memory, ...]:
        if not memory_ids:
            return ()

        placeholders = ", ".join("?" for _ in memory_ids)
        with open_connection(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories "  # noqa: S608 - 占位符按入参生成
                f"WHERE id IN ({placeholders})",
                memory_ids,
            ).fetchall()

        # 入参顺序即调用方期望的顺序（检索结果按相似度排过），SQL 的 IN 不保证顺序
        by_id = {str(row["id"]): _row_to_memory(row) for row in rows}
        return tuple(by_id[memory_id] for memory_id in memory_ids if memory_id in by_id)

    def _list_sync(self, query: MemoryFilter) -> tuple[Memory, ...]:
        clauses = ["user_id = ?", "companion_id = ?"]
        params: list[object] = [query.user_id, query.companion_id]

        if query.types:
            placeholders = ", ".join("?" for _ in query.types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(member.value for member in query.types)
        if query.min_importance is not None:
            clauses.append("importance >= ?")
            params.append(query.min_importance)
        if query.created_after is not None:
            clauses.append("created_at >= ?")
            params.append(_to_iso(query.created_after))
        if not query.include_archived:
            clauses.append("archived_at IS NULL")
        if not query.include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_to_iso(datetime.now(tz=UTC)))

        sql = (
            f"SELECT {_MEMORY_COLUMNS} FROM memories "  # noqa: S608 - 子句由本方法内部拼装
            f"WHERE {' AND '.join(clauses)} ORDER BY id"
        )
        if query.limit is not None:
            sql += " LIMIT ?"
            params.append(query.limit)

        with open_connection(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        return tuple(_row_to_memory(row) for row in rows)

    def _count_sync(self, user_id: str, companion_id: str) -> int:
        with open_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM memories "
                "WHERE user_id = ? AND companion_id = ? AND archived_at IS NULL",
                (user_id, companion_id),
            ).fetchone()

        return int(row["n"]) if row is not None else 0

    def _search_similar_sync(
        self,
        vector: tuple[float, ...],
        user_id: str,
        companion_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        if limit <= 0:
            return ()
        blob = self._to_blob(vector)

        # vec0 的 KNN 必须出现在子查询里（`MATCH ... AND k = ?` 的语法只在那里成立），
        # 外层再 JOIN 事实表做归属与生命周期过滤——这样 k 是"最近 k 条候选"，
        # 被过滤掉的不会占额度。
        sql = (
            "SELECT v.memory_id AS memory_id, v.distance AS distance FROM ("
            "  SELECT memory_id, distance FROM memory_vectors WHERE embedding MATCH ? AND k = ?"
            ") v JOIN memories m ON m.id = v.memory_id "
            "WHERE m.user_id = ? AND m.companion_id = ? "
            "  AND m.archived_at IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?) "
            "ORDER BY v.distance"
        )

        with open_connection(self._db_path, load_extensions=True) as conn:
            rows = conn.execute(sql, (blob, limit, user_id, companion_id, _to_iso(now))).fetchall()

        return tuple((str(row["memory_id"]), float(row["distance"])) for row in rows)

    def _get_flag_sync(self, key: str) -> str | None:
        with open_connection(self._db_path) as conn:
            row = conn.execute("SELECT value FROM app_flags WHERE key = ?", (key,)).fetchone()

        return str(row["value"]) if row is not None else None

    def _set_flag_sync(self, key: str, value: str, at: datetime) -> None:
        with open_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO app_flags (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, _to_iso(at)),
            )

    # ── 内部 ────────────────────────────────────────────

    def _to_blob(self, vector: Sequence[float]) -> bytes:
        self._require_dim(vector)
        # ``bytes(...)`` 而不是直接返回：sqlite_vec 没有类型标注，直接返回会变成 Any
        return bytes(sqlite_vec.serialize_float32(list(vector)))

    def _require_dim(self, vector: Sequence[float]) -> None:
        if len(vector) != self._embedding_dim:
            msg = f"embedding dimension mismatch: expected {self._embedding_dim}, got {len(vector)}"
            raise ValueError(msg)


def _memory_values(memory: Memory) -> tuple[object, ...]:
    return (
        memory.id,
        memory.user_id,
        memory.companion_id,
        memory.type.value,
        memory.content,
        memory.summary,
        memory.importance,
        memory.confidence,
        memory.source_message_id,
        memory.source_event_id,
        _to_iso(memory.created_at),
        _to_iso(memory.last_accessed_at) if memory.last_accessed_at else None,
        _to_iso(memory.expires_at) if memory.expires_at else None,
        _to_iso(memory.archived_at) if memory.archived_at else None,
        json.dumps(dict(memory.metadata), ensure_ascii=False),
    )


def _memory_update_values(memory: Memory) -> tuple[object, ...]:
    """UPDATE 的赋值顺序与 :func:`_memory_values` 不同（不含 id / 归属 / ``created_at``）。

    ``created_at`` 不参与更新：它是"这条记忆何时产生"的事实，编辑内容不该改写它。
    """
    return (
        memory.type.value,
        memory.content,
        memory.summary,
        memory.importance,
        memory.confidence,
        memory.source_message_id,
        memory.source_event_id,
        _to_iso(memory.last_accessed_at) if memory.last_accessed_at else None,
        _to_iso(memory.expires_at) if memory.expires_at else None,
        _to_iso(memory.archived_at) if memory.archived_at else None,
        json.dumps(dict(memory.metadata), ensure_ascii=False),
        memory.id,
    )


def _row_to_memory(row: sqlite3.Row) -> Memory:
    raw_metadata = str(row["metadata_json"])
    return Memory(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        companion_id=str(row["companion_id"]),
        type=MemoryType(str(row["type"])),
        content=str(row["content"]),
        summary=str(row["summary"]) if row["summary"] is not None else None,
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        source_message_id=(
            str(row["source_message_id"]) if row["source_message_id"] is not None else None
        ),
        source_event_id=(
            str(row["source_event_id"]) if row["source_event_id"] is not None else None
        ),
        created_at=_from_iso(str(row["created_at"])),
        last_accessed_at=(
            _from_iso(str(row["last_accessed_at"])) if row["last_accessed_at"] else None
        ),
        expires_at=_from_iso(str(row["expires_at"])) if row["expires_at"] else None,
        archived_at=_from_iso(str(row["archived_at"])) if row["archived_at"] else None,
        metadata=json.loads(raw_metadata) if raw_metadata else {},
    )
