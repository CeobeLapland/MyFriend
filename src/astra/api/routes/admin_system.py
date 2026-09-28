"""``POST /v1/admin/restart`` —— 请求重启后端（ADR 0026 §1 / §2）。

**这个端点自己不重启任何东西**，它只做三件事：

1. 确认"有人会把我拉起来"（监督进程给的 ``ASTRA_SUPERVISED``，落在 ``app.state`` 上）；
   没人管的时候返回 **409**，而不是退出后变成一具尸体；
2. 把**现在这份 ``.env``** 校验一遍——写坏的文件不该靠"重启失败"才发现；
3. 置位 ``restart_requested`` 与 uvicorn 的 ``should_exit``，让它优雅收摊。

退出码的翻译在 ``cli._serve_foreground``：请求过就是 75（请重启我），否则 0。
未监督 / 配置非法都**如实报错**，不静默降级成"看起来重启了"。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from astra.api.routes.admin_config import env_paths
from astra.settings.env_check import check_env_file

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class RestartBody(BaseModel):
    status: Literal["restarting"] = "restarting"


@router.post(
    "/restart",
    response_model=RestartBody,
    status_code=status.HTTP_202_ACCEPTED,
    summary="请求重启后端（需要监督进程；会中断在跑的 turn）",
)
async def restart_backend(request: Request) -> RestartBody:
    state = request.app.state
    server = getattr(state, "uvicorn_server", None)

    if not getattr(state, "supervised", False) or server is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "not_supervised",
                "message": (
                    "后端不是以监督模式启动的：重启会让它退出，而没有人会把它拉起来。"
                    "请用 `uv run astra serve`（默认带监督）启动，"
                    "或先保存配置后自己重启 `uv run astra serve --no-supervisor`。"
                ),
            },
        )

    env_file, _example = env_paths(request)
    if env_file.exists():
        result = await asyncio.to_thread(check_env_file, env_file)
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "env_invalid",
                    "message": f".env 现在是非法的，重启会直接失败——先修好它：{result.message}",
                },
            )

    # 先回 202，再让 uvicorn 收摊：请求本身要能出得去
    state.restart_requested = True
    server.should_exit = True
    return RestartBody()
