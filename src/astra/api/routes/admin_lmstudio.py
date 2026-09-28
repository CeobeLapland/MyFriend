"""``/v1/admin/lmstudio`` —— 模型与 LM Studio 服务（ADR 0026 §2 / §4）。

| 端点 | 作用 |
|---|---|
| ``GET ""`` | CLI 可用性 / server 状态 / 本地模型 / 已加载 / 上次的显存声明值 |
| ``POST /server`` | ``lms server start\\|stop`` |
| ``POST /load`` | ``lms load <key> --gpu max\\|off\\|0~1 [-c N] -y``，并记下声明值 |
| ``POST /unload`` | ``lms unload <identifier>`` 或全部 |

**"声明不是实测"**：LM Studio 不暴露"模型在 GPU 还是 CPU"（ADR 0025 §6 实测），
所以 ``declared`` 是 Studio 自己下发的参数，UI 必须如实标注。

命令失败一律带原始 stdout/stderr 回给 UI（截断到几千字符），并且：
找不到 CLI → **409 ``lms_unavailable``**；命令失败/超时 → **502 ``lms_failed``**。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from astra.infrastructure import lmstudio_cli, studio_state
from astra.infrastructure.lmstudio_cli import LmsModel, LmsResult, ServerState, run_lms
from astra.settings import Settings

router = APIRouter(prefix="/v1/admin/lmstudio", tags=["admin"])


# ── 响应模型 ────────────────────────────────────────────


class LmsCliBody(BaseModel):
    available: bool
    path: str | None = None
    message: str | None = None


class LmsModelBody(BaseModel):
    key: str
    display_name: str
    model_type: str
    params: str | None = None
    size_bytes: int | None = None
    max_context: int | None = None


class LmsLoadedBody(BaseModel):
    identifier: str
    model_key: str | None = None
    context_length: int | None = None
    size_bytes: int | None = None


class LmsDeclaredBody(BaseModel):
    model_key: str
    gpu: str
    context_length: int | None = None
    at: str


class LmStudioStatusBody(BaseModel):
    cli: LmsCliBody
    server: ServerState
    server_output: str | None = None
    models: list[LmsModelBody] = Field(default_factory=list)
    loaded: list[LmsLoadedBody] = Field(default_factory=list)
    declared: LmsDeclaredBody | None = None


class LmsActionBody(BaseModel):
    ok: bool
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    declared: LmsDeclaredBody | None = None


class LmsServerBody(BaseModel):
    action: Literal["start", "stop"]


class LmsLoadBody(BaseModel):
    model_key: str = Field(min_length=1)
    #: ``max`` 全塞 GPU / ``off`` 走 CPU / 0~1 的浮点是 LM Studio 的分层比例
    gpu: Literal["max", "off"] | Annotated[float, Field(ge=0.0, le=1.0)] = "max"
    context_length: Annotated[int | None, Field(ge=256)] = None


class LmsUnloadBody(BaseModel):
    identifier: str | None = None
    unload_all: bool = False


# ── 读 ──────────────────────────────────────────────────


@router.get("", response_model=LmStudioStatusBody, summary="LM Studio：服务状态、模型与显存声明值")
async def read_status(request: Request) -> LmStudioStatusBody:
    executable = lmstudio_cli.cli_path()
    if executable is None:
        return LmStudioStatusBody(
            cli=LmsCliBody(available=False, message=lmstudio_cli.unavailable_result().message),
            server="unknown",
        )

    server_result, models_result, loaded_result = await asyncio.gather(
        run_lms(["server", "status"]),
        run_lms(["ls", "--json"]),
        run_lms(["ps", "--json"]),
    )

    return LmStudioStatusBody(
        cli=LmsCliBody(available=True, path=executable),
        server=lmstudio_cli.parse_server_status(server_result.stdout + server_result.stderr),
        server_output=(server_result.stdout + server_result.stderr).strip() or None,
        models=[
            _model_body(model) for model in lmstudio_cli.parse_models(models_result.last_json())
        ],
        loaded=[
            _loaded_body(item) for item in lmstudio_cli.parse_loaded(loaded_result.last_json())
        ],
        declared=_declared_body(studio_state.read_declared(_state_path(request))),
    )


# ── 动作 ────────────────────────────────────────────────


@router.post("/server", response_model=LmsActionBody, summary="启动 / 停止 LM Studio 的本地服务")
async def control_server(body: LmsServerBody) -> LmsActionBody:
    result = await run_lms(["server", body.action], timeout_s=lmstudio_cli.SERVER_TIMEOUT_S)
    _raise_for(result)
    return LmsActionBody(ok=True, stdout=result.stdout, stderr=result.stderr)


@router.post("/load", response_model=LmsActionBody, summary="加载模型（并记下显存声明值）")
async def load_model(body: LmsLoadBody, request: Request) -> LmsActionBody:
    gpu = _gpu_arg(body.gpu)
    args = ["load", body.model_key, "--gpu", gpu, "-y"]
    if body.context_length is not None:
        args += ["--context-length", str(body.context_length)]

    result = await run_lms(args, timeout_s=lmstudio_cli.LOAD_TIMEOUT_S)
    _raise_for(result)

    declared = studio_state.DeclaredLoad(
        model_key=body.model_key,
        gpu=gpu,
        context_length=body.context_length,
        at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )
    await asyncio.to_thread(studio_state.write_declared, _state_path(request), declared)

    return LmsActionBody(
        ok=True,
        stdout=result.stdout,
        stderr=result.stderr,
        declared=_declared_body(declared),
    )


@router.post("/unload", response_model=LmsActionBody, summary="卸载模型")
async def unload_model(body: LmsUnloadBody) -> LmsActionBody:
    if body.unload_all:
        args = ["unload", "--all"]
    elif body.identifier:
        args = ["unload", body.identifier]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_input", "message": "要给出 identifier，或者 unload_all=true"},
        )

    result = await run_lms(args, timeout_s=lmstudio_cli.SERVER_TIMEOUT_S)
    _raise_for(result)
    return LmsActionBody(ok=True, stdout=result.stdout, stderr=result.stderr)


# ── 内部 ────────────────────────────────────────────────


def _state_path(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return studio_state.state_path(settings.data_dir)


def _gpu_arg(gpu: str | float) -> str:
    if isinstance(gpu, float):
        return f"{gpu:g}"
    return gpu


def _raise_for(result: LmsResult) -> None:
    if result.ok:
        return

    raw = {"stdout": result.stdout, "stderr": result.stderr}
    if lmstudio_cli.cli_path() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "lms_unavailable", "message": result.message, **raw},
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "lms_failed", "message": result.message, **raw},
    )


def _model_body(model: LmsModel) -> LmsModelBody:
    return LmsModelBody(
        key=model.key,
        display_name=model.display_name,
        model_type=model.model_type,
        params=model.params,
        size_bytes=model.size_bytes,
        max_context=model.max_context,
    )


def _loaded_body(loaded: lmstudio_cli.LmsLoadedModel) -> LmsLoadedBody:
    return LmsLoadedBody(
        identifier=loaded.identifier,
        model_key=loaded.model_key,
        context_length=loaded.context_length,
        size_bytes=loaded.size_bytes,
    )


def _declared_body(declared: studio_state.DeclaredLoad | None) -> LmsDeclaredBody | None:
    if declared is None:
        return None
    return LmsDeclaredBody(
        model_key=declared.model_key,
        gpu=declared.gpu,
        context_length=declared.context_length,
        at=declared.at,
    )
