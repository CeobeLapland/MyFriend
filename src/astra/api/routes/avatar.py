"""``/v1/avatar/animation`` —— 动画映射的权威副本（ADR 0027 §2）。

**后端持有权威副本**（`data/astra_animation.json`），Unity 从它拉取 + 本地缓存兜底，
Studio 在动画页里编辑它。

两条刻意的设计：

- ``GET`` 在后端还没有副本时返回 **404**，而不是空配置——返回空的会被 Unity 当成
  "配置就是这样"，把用户辛苦配的映射冲掉；
- ``PUT`` **只校验不重写**：落盘的就是请求体原文（保留未知字段与排版），
  这样手改文件、将来加字段都不会被后端吃掉。校验规则见 ``domain/animation.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from astra.domain.animation import validate_animation
from astra.settings import Settings

router = APIRouter(prefix="/v1/avatar", tags=["avatar"])

#: 相对 ``settings.data_dir`` 的文件名。**与 Unity 工程里那份同名**——它现在是缓存
FILE_NAME = "astra_animation.json"


class AnimationWriteBody(BaseModel):
    ok: bool
    path: str
    bytes: int


@router.get(
    "/animation",
    response_class=Response,
    summary="读取动画映射（原始 JSON 文本；还没有副本时 404）",
)
async def read_animation(request: Request) -> Response:
    path = _animation_path(request)
    if not await asyncio.to_thread(path.exists):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "animation_not_set",
                "message": (
                    "后端还没有动画映射副本。跑一次 Unity 客户端会自动把本地那份上报上来；"
                    "也可以在 Studio 的动画页粘贴一份 JSON 导入。"
                ),
            },
        )

    text = await asyncio.to_thread(path.read_bytes)
    # 按**字节**返回：文本模式会把 CRLF 读成 LF，那就不是"原样"了
    return Response(content=text, media_type="application/json; charset=utf-8")


@router.put(
    "/animation", response_model=AnimationWriteBody, summary="写入动画映射（校验后原样落盘）"
)
async def write_animation(request: Request) -> AnimationWriteBody:
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad_request("invalid_json", "请求体不是 UTF-8 文本") from None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _bad_request(
            "invalid_json", f"不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行）"
        ) from None

    issues = validate_animation(payload)
    if issues:
        raise _bad_request("invalid_animation", "；".join(issue.render() for issue in issues))

    path = _animation_path(request)
    await asyncio.to_thread(_write_atomic, path, raw)
    return AnimationWriteBody(ok=True, path=str(path), bytes=len(raw))


def _animation_path(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return settings.data_dir / FILE_NAME


def _write_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再替换：写到一半崩掉也不会留下一份半截配置。

    **必须写字节**：``write_text`` 会按平台把 ``\\n`` 转成 ``\\r\\n``（Windows），
    那就不是"原样落盘"了——实测踩过（2514 字节变 2657）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_bytes(data)
    os.replace(staged, path)


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )
