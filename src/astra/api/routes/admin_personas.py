"""``/v1/admin/personas`` —— 人格的列表 / 读取 / 新建 / 保存 / 删除 / 切换（ADR 0029）。

- 一位人格 = 人格目录下的一个 ``<id>.toml``；目录 = ``ASTRA_PERSONA_PATH`` 的父目录。
- **id 即 ``companion_id``**：历史 / 记忆 / 状态都按 ``companion_id`` 归档，
  所以"换一位来聊"天然就是"换一套历史与记忆"，不需要额外的隔离机制。
- **切换写 ``.env`` 的两个键**（``ASTRA_COMPANION_ID`` / ``ASTRA_PERSONA_PATH``），
  **重启后生效**——人格在装配期加载（ADR 0015）；这里刻意**不**做运行时热切换
  （理由见 ADR 0029 的 Reason）。
- **编辑 = 整份重写**该文件（注释不保留；写前备份 ``<id>.toml.bak``）。
- **删除 = 只删文件**：该 id 名下的会话 / 记忆 / 状态仍在数据库里，用同名 id
  建回来就能继续用——删除用户数据必须走各自的显式入口（记忆页 / 历史栏）。
- 校验与加载**同一套模型**（``PersonaDocument``）：UI 放得过、启动炸不了。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from astra.api.routes.admin_config import env_paths, write_env_values
from astra.infrastructure import dotenv_store
from astra.infrastructure.persona.store import (
    InvalidPersonaIdError,
    PersonaEntry,
    PersonaStore,
)
from astra.infrastructure.persona.toml_loader import (
    PersonaDocument,
    PersonaLoadError,
    validate_document,
)
from astra.settings import Settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])

#: 切换时要写的两个键——**它们就是"激活"这件事的全部状态**
_ACTIVE_KEYS = ("ASTRA_COMPANION_ID", "ASTRA_PERSONA_PATH")


class PersonaSummary(BaseModel):
    id: str
    display_name: str | None = None
    path: str
    #: 当前进程正在用的那一位
    is_running: bool = False
    #: 已被写进 ``.env``、**重启后**才生效的那一位
    is_pending: bool = False
    #: 文件坏了（解析失败 / id 非法）时的原因——列表如实标出，而不是藏起来
    error: str | None = None


class PersonaListBody(BaseModel):
    directory: str
    running_id: str | None = None
    pending_id: str | None = None
    supervised: bool = False
    personas: list[PersonaSummary] = Field(default_factory=list)


class PersonaBody(BaseModel):
    id: str
    path: str
    document: PersonaDocument


class PersonaCreateBody(BaseModel):
    id: str
    persona: dict[str, Any]


class PersonaUpdateBody(BaseModel):
    persona: dict[str, Any]


class PersonaActivateBody(BaseModel):
    id: str
    restart_required: bool = True
    supervised: bool = False
    env_file: str
    written: list[str] = Field(default_factory=list)


# ── 列表 ────────────────────────────────────────────────


@router.get("/personas", response_model=PersonaListBody, summary="列出人格（含运行中 / 待启用）")
async def list_personas(request: Request) -> PersonaListBody:
    store = _store(request)
    entries = await asyncio.to_thread(store.list)
    running, pending = _active_ids(request)

    return PersonaListBody(
        directory=str(store.directory),
        running_id=running,
        pending_id=pending,
        supervised=bool(getattr(request.app.state, "supervised", False)),
        personas=[_summary(entry, running=running, pending=pending) for entry in entries],
    )


# ── 单份文档 ────────────────────────────────────────────


@router.get("/personas/{persona_id}", response_model=PersonaBody, summary="读取一份人格文档")
async def read_persona(persona_id: str, request: Request) -> PersonaBody:
    store = _store(request)
    document = await _require_document(store, persona_id)
    return PersonaBody(id=persona_id, path=str(store.path_for(persona_id)), document=document)


@router.post(
    "/personas",
    response_model=PersonaBody,
    status_code=status.HTTP_201_CREATED,
    summary="新建一个人格（不自动激活）",
)
async def create_persona(body: PersonaCreateBody, request: Request) -> PersonaBody:
    store = _store(request)
    document = _validate(body.persona)

    try:
        path = await asyncio.to_thread(store.create, body.id, document)
    except InvalidPersonaIdError as exc:
        raise _bad("invalid_id", str(exc)) from exc
    except FileExistsError as exc:
        raise _conflict(
            "persona_exists", f"已经有一位叫 {body.id} 的人格了——去改它，或者换个 id"
        ) from exc

    return PersonaBody(id=body.id, path=str(path), document=document)


@router.put("/personas/{persona_id}", response_model=PersonaBody, summary="覆盖保存一个人格")
async def update_persona(persona_id: str, body: PersonaUpdateBody, request: Request) -> PersonaBody:
    store = _store(request)
    document = _validate(body.persona)
    await _require_document(store, persona_id)

    path = await asyncio.to_thread(store.write, persona_id, document)
    return PersonaBody(id=persona_id, path=str(path), document=document)


@router.delete(
    "/personas/{persona_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一个人格文件（不删它的会话与记忆）",
)
async def delete_persona(persona_id: str, request: Request) -> None:
    store = _store(request)
    running, pending = _active_ids(request)

    try:
        entries = await asyncio.to_thread(store.list)
    except InvalidPersonaIdError as exc:  # pragma: no cover - list 不做 id 校验
        raise _bad("invalid_id", str(exc)) from exc

    if len(entries) == 1 and entries[0].id == persona_id:
        raise _conflict("persona_last", "这是最后一位人格——删了下次启动就没有身份了")

    if persona_id in {running, pending}:
        raise _conflict(
            "persona_active",
            "这一位正在使用（或已被设为下次启动用）——先切换走，再来删它",
        )

    try:
        removed = await asyncio.to_thread(store.delete, persona_id)
    except InvalidPersonaIdError as exc:
        raise _bad("invalid_id", str(exc)) from exc

    if not removed:
        raise _not_found(persona_id)


@router.post(
    "/personas/{persona_id}/activate",
    response_model=PersonaActivateBody,
    summary="把这一位写进 .env（重启后生效）",
)
async def activate_persona(persona_id: str, request: Request) -> PersonaActivateBody:
    store = _store(request)
    path = store.path_for(persona_id)  # id 非法在这里就抛了
    await _require_document(store, persona_id)

    env_file, example_file = env_paths(request)
    values: Mapping[str, str] = {
        "ASTRA_COMPANION_ID": persona_id,
        "ASTRA_PERSONA_PATH": _env_path_text(path),
    }
    written, _created = await asyncio.to_thread(write_env_values, env_file, example_file, values)

    return PersonaActivateBody(
        id=persona_id,
        restart_required=True,
        supervised=bool(getattr(request.app.state, "supervised", False)),
        env_file=str(env_file),
        written=written,
    )


# ── 内部 ────────────────────────────────────────────────


def _store(request: Request) -> PersonaStore:
    """人格目录 = ``ASTRA_PERSONA_PATH`` 的父目录（ADR 0029 的约定）。"""
    settings: Settings = request.app.state.settings
    return PersonaStore(settings.persona_path.parent)


def _active_ids(request: Request) -> tuple[str | None, str | None]:
    """``(运行中的 id, 重启后生效的 id)``。

    第二个从 ``.env`` 的 ``ASTRA_PERSONA_PATH`` 读——刚切换但还没重启时两者不同，
    UI 上显示"重启后启用"，**不假装已经生效**（ADR 0026 §5 的同一精神）。
    ``.env`` 没写这个键时，重启后仍是当前这一位，所以退化成同一个值。
    """
    settings: Settings = request.app.state.settings
    running = settings.persona_path.stem if settings.persona_path.is_file() else None

    env_file, _example = env_paths(request)
    pending = running
    if env_file.exists():
        values = dotenv_store.parse_values(env_file.read_text(encoding="utf-8"))
        raw = values.get("ASTRA_PERSONA_PATH", "").strip()
        if raw:
            pending = Path(raw).stem

    return running, pending


def _summary(entry: PersonaEntry, *, running: str | None, pending: str | None) -> PersonaSummary:
    return PersonaSummary(
        id=entry.id,
        display_name=entry.display_name,
        path=str(entry.path),
        is_running=entry.id == running,
        # 只有"待启用与运行中不同"才点亮这个徽标——否则每位都会亮
        is_pending=pending is not None and pending != running and entry.id == pending,
        error=entry.error,
    )


def _validate(payload: dict[str, Any]) -> PersonaDocument:
    """校验 Studio 提交的文档。失败 → 400 ``invalid_persona``，消息可直接展示。"""
    try:
        return validate_document(payload)
    except PersonaLoadError as exc:
        raise _bad("invalid_persona", str(exc)) from exc


async def _require_document(store: PersonaStore, persona_id: str) -> PersonaDocument:
    """读一份**必须存在且合法**的文档：不存在 → 404；存在但坏了 → 400。"""
    try:
        exists = await asyncio.to_thread(store.exists, persona_id)
    except InvalidPersonaIdError as exc:
        raise _bad("invalid_id", str(exc)) from exc

    if not exists:
        raise _not_found(persona_id)

    try:
        return await asyncio.to_thread(store.read, persona_id)
    except PersonaLoadError as exc:
        raise _bad("invalid_persona", f"这个文件读不出来，先修好它：{exc}") from exc


def _env_path_text(path: Path) -> str:
    """``.env`` 里的路径写法：能相对 cwd 就用 ``./…``（与 ``.env.example`` 一致）。"""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.cwd().resolve())
    except ValueError:
        return str(resolved)
    return f"./{relative.as_posix()}"


def _bad(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _not_found(persona_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "persona_not_found", "message": f"persona not found: {persona_id}"},
    )
