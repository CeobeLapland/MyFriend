"""``/v1/memories`` —— 记忆的查看 / 编辑 / 删除 / 导出 / 清空 / 禁用（`docs/06` §9）。

这是 `docs/06` §9 要求的**用户控制面**，也是 M2 验收第 5 步（"删除后不再检索到"）
的入口。

**两条实现注意**：

1. **静态路径必须声明在 ``/{memory_id}`` 之前**。FastAPI 按声明顺序匹配，
   否则 ``/export``、``/settings``、``/clear`` 会被当成 id 吞掉。
2. **删除是真的删**：``MemoryService.delete`` 在同一事务里清掉事实、embedding 与
   向量索引（`docs/04` §2 / `docs/11` §4）。不存在的 id 返回 404，不静默成功。

``GET /v1/memories?query=...`` 走**语义检索**（与聊天时的检索同一条路径）；
不带 ``query`` 则按过滤条件列表。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from astra.api.dependencies import get_memory_service
from astra.api.schemas import (
    MemoryBody,
    MemoryClearBody,
    MemoryExportBody,
    MemoryListBody,
    MemorySettingsBody,
    MemoryUpdateBody,
)
from astra.application.memory_service import MemoryService
from astra.domain.memory import Memory, MemoryFilter, MemoryType

router = APIRouter(prefix="/v1/memories", tags=["memories"])

MemoryDep = Annotated[MemoryService, Depends(get_memory_service)]


def _to_body(memory: Memory) -> MemoryBody:
    return MemoryBody(
        id=memory.id,
        user_id=memory.user_id,
        companion_id=memory.companion_id,
        type=memory.type.value,
        content=memory.content,
        summary=memory.summary,
        importance=memory.importance,
        confidence=memory.confidence,
        source_message_id=memory.source_message_id,
        source_event_id=memory.source_event_id,
        created_at=memory.created_at,
        last_accessed_at=memory.last_accessed_at,
        expires_at=memory.expires_at,
        archived_at=memory.archived_at,
        metadata=dict(memory.metadata),
    )


def _not_found(memory_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "memory_not_found", "message": f"memory not found: {memory_id}"},
    )


# ── 静态路径（必须排在 /{memory_id} 之前）───────────────


@router.get("/settings", response_model=MemorySettingsBody, summary="读取记忆开关")
async def read_settings(memories: MemoryDep) -> MemorySettingsBody:
    return MemorySettingsBody(enabled=await memories.is_enabled())


@router.put("/settings", response_model=MemorySettingsBody, summary="启用/禁用记忆")
async def update_settings(memories: MemoryDep, body: MemorySettingsBody) -> MemorySettingsBody:
    """运行时切换，立即生效（存 ``app_flags``，不重启）。"""
    await memories.set_enabled(body.enabled)
    return MemorySettingsBody(enabled=await memories.is_enabled())


@router.post("/clear", response_model=MemoryClearBody, summary="清空全部记忆")
async def clear_memories(memories: MemoryDep) -> MemoryClearBody:
    return MemoryClearBody(removed=await memories.clear())


@router.get("/export", response_model=MemoryExportBody, summary="导出全部记忆（含归档/过期）")
async def export_memories(memories: MemoryDep) -> MemoryExportBody:
    exported = await memories.export()
    return MemoryExportBody(
        exported_at=datetime.now(tz=UTC),
        total=len(exported),
        memories=[_to_body(memory) for memory in exported],
    )


# ── 列表与单条 ──────────────────────────────────────────


@router.get("", response_model=MemoryListBody, summary="列出或语义检索记忆")
async def list_memories(
    memories: MemoryDep,
    memory_type: Annotated[list[str] | None, Query(alias="type")] = None,
    min_importance: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    created_after: datetime | None = None,
    query: str | None = None,
    archived: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> MemoryListBody:
    """``docs/04`` §2 指定的五个过滤条件，外加 ``query``（语义检索）。

    ``query`` 走的是与聊天同一套检索（含重排序与 ``min_score`` 过滤），
    因此返回顺序是**相关度**而不是 id。
    """
    if query is not None:
        hits = await memories.retrieve(query)
        found = [hit.memory for hit in hits]
        if min_importance is not None:
            found = [memory for memory in found if memory.importance >= min_importance]
        return MemoryListBody(memories=[_to_body(memory) for memory in found], total=len(found))

    default = memories.default_filter()
    listed = await memories.list_memories(
        MemoryFilter(
            user_id=default.user_id,
            companion_id=default.companion_id,
            types=tuple(_parse_types(memory_type)),
            min_importance=min_importance,
            created_after=created_after,
            include_archived=archived,
            limit=limit,
        )
    )
    return MemoryListBody(memories=[_to_body(memory) for memory in listed], total=len(listed))


def _parse_types(raw: list[str] | None) -> list[MemoryType]:
    """把查询串里的类型名解析成枚举；不认识的值直接 422（而不是静默忽略）。"""
    if not raw:
        return []

    parsed: list[MemoryType] = []
    for value in raw:
        try:
            parsed.append(MemoryType(value))
        except ValueError as exc:
            known = ", ".join(member.value for member in MemoryType)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_input",
                    "message": f"unknown memory type: {value} ({known})",
                },
            ) from exc
    return parsed


@router.get("/{memory_id}", response_model=MemoryBody, summary="读取单条记忆")
async def read_memory(memory_id: str, memories: MemoryDep) -> MemoryBody:
    memory = await memories.get(memory_id)
    if memory is None:
        raise _not_found(memory_id)
    return _to_body(memory)


@router.patch("/{memory_id}", response_model=MemoryBody, summary="编辑记忆")
async def update_memory(memory_id: str, body: MemoryUpdateBody, memories: MemoryDep) -> MemoryBody:
    updated = await memories.update(
        memory_id,
        content=body.content,
        summary=body.summary,
        memory_type=MemoryType(body.type) if body.type is not None else None,
        importance=body.importance,
        confidence=body.confidence,
    )
    if updated is None:
        raise _not_found(memory_id)
    return _to_body(updated)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除记忆")
async def delete_memory(memory_id: str, memories: MemoryDep) -> None:
    """**真正删除**事实、embedding 与向量索引（`docs/04` §2）。"""
    if not await memories.delete(memory_id):
        raise _not_found(memory_id)
