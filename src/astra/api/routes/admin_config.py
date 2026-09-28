"""``/v1/admin/config`` —— Studio 设置页读写 ``.env``（ADR 0026 §2 / §3）。

**只改值、不增删键**：``Settings`` 用 ``extra="forbid"``，``.env`` 里多一个未知键
就等于下次启动直接失败。所以这里有三道闸：

1. **白名单**（:data:`_WHITELIST`）——只有用户真的会改的键能被改；``SecretStr``
   （API key）根本不进这张表，也不会被回显。类型、枚举候选、默认值都从
   ``Settings.model_fields`` 推导，不在这里手抄（抄了就会漂）。
2. **只改值**——注释、空行与顺序原样保留（``infrastructure/dotenv_store.py``）。
3. **写前校验**——把候选内容拿去用干净环境实例化一次 ``Settings``
   （``settings/env_check.py``），跨字段约束（例如五条检索权重之和 = 1.0）也挡得住；
   不过就不落盘。落盘前还会把现有文件备份成 ``.env.bak``。

**来源必须如实标注**：进程环境变量的优先级高于 ``.env``（实测），所以每个键都带
``source``（``env`` / ``file`` / ``default``）——否则用户内联了 ``$env:ASTRA_LLM_MODEL``
启动时，会在 Studio 里改了文件、重启、发现"没生效"而不知道为什么（ADR 0026 §5）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, get_args, get_origin

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from astra.infrastructure import dotenv_store
from astra.settings import Settings
from astra.settings.env_check import check_env_file

router = APIRouter(prefix="/v1/admin", tags=["admin"])

#: 白名单：键 → (分组, 中文标签)。**只放用户真的会改的键**；API key 刻意不在表里
_WHITELIST: dict[str, tuple[str, str]] = {
    "ASTRA_LLM_PROVIDER": ("LLM", "提供方"),
    "ASTRA_LLM_MODEL": ("LLM", "模型"),
    "ASTRA_LLM_FALLBACK_MODEL": ("LLM", "降级模型（显存不足时）"),
    "ASTRA_LLM_BASE_URL": ("LLM", "服务地址"),
    "ASTRA_LLM_TEMPERATURE": ("LLM", "温度"),
    "ASTRA_LLM_MAX_TOKENS": ("LLM", "单次回复 token 上限"),
    "ASTRA_LLM_REASONING_EFFORT": ("LLM", "思考深度"),
    "ASTRA_EMBEDDING_PROVIDER": ("记忆", "向量化提供方"),
    "ASTRA_EMBEDDING_MODEL": ("记忆", "向量化模型"),
    "ASTRA_MEMORY_ENABLED": ("记忆", "启用记忆（启动期默认值）"),
    "ASTRA_MEMORY_MAX_CANDIDATES_PER_TURN": ("记忆", "每轮最多自动记几条（0 = 不自动记）"),
    "ASTRA_MEMORY_TOP_K": ("记忆", "每轮注入几条记忆（0 = 不注入）"),
    "ASTRA_STT_PROVIDER": ("语音", "语音识别（STT）"),
    "ASTRA_TTS_PROVIDER": ("语音", "语音合成（TTS）"),
    "ASTRA_TTS_BASE_URL": ("语音", "CosyVoice 地址"),
    "ASTRA_TTS_VOICE": ("语音", "预置音色"),
    "ASTRA_TTS_REFERENCE_AUDIO": ("语音", "参考音频（zero-shot 克隆；留空用预置音色）"),
    "ASTRA_ENV": ("运行", "运行环境（prod 会关掉 /dev）"),
    "ASTRA_AVATAR_ENABLED": ("运行", "形象客户端已接入（纯通告）"),
    "ASTRA_LOG_LEVEL": ("运行", "日志级别"),
}

#: ``Settings`` 认得的全部键。**这是权威集合**（``env_prefix="ASTRA_"``、无 alias），
#: 白名单必须是它的子集，``.env`` 里出现它之外的键就等于下次启动直接失败
_KNOWN_KEYS = frozenset(f"ASTRA_{name.upper()}" for name in Settings.model_fields)

_ValueKind = Literal["enum", "bool", "int", "float", "text", "path"]
_Source = Literal["env", "file", "default"]


class AdminConfigField(BaseModel):
    key: str
    group: str
    label: str
    kind: _ValueKind
    choices: list[str] = Field(default_factory=list)
    #: ``.env`` 里的值（文件里没有则是字段默认值）——表单里编辑的就是它
    value: str
    #: 当前进程实际生效的值
    effective: str
    default: str | None = None
    source: _Source
    #: 重启之后这个键的生效值会变吗
    restart_pending: bool = False


class AdminConfigBody(BaseModel):
    env_file: str
    env_exists: bool
    example_file: str
    supervised: bool
    restart_pending: list[str] = Field(default_factory=list)
    unknown_keys: list[str] = Field(default_factory=list)
    fields: list[AdminConfigField] = Field(default_factory=list)


class AdminConfigUpdateBody(BaseModel):
    values: dict[str, str]


class AdminConfigWriteBody(BaseModel):
    env_file: str
    created: bool
    written: list[str] = Field(default_factory=list)
    restart_pending: list[str] = Field(default_factory=list)


@router.get("/config", response_model=AdminConfigBody, summary="可编辑配置：当前值、来源与告警")
async def read_config(request: Request) -> AdminConfigBody:
    settings: Settings = request.app.state.settings
    env_file, example_file = env_paths(request)
    file_values = dotenv_store.parse_values(await _read_text(env_file))

    fields = [_field_view(settings, key, file_values) for key in _WHITELIST]
    return AdminConfigBody(
        env_file=str(env_file),
        env_exists=env_file.exists(),
        example_file=str(example_file),
        supervised=bool(getattr(request.app.state, "supervised", False)),
        restart_pending=sorted(field.key for field in fields if field.restart_pending),
        unknown_keys=sorted(key for key in file_values if key not in _KNOWN_KEYS),
        fields=fields,
    )


@router.put("/config", response_model=AdminConfigWriteBody, summary="写入 .env（只改白名单键的值）")
async def write_config(body: AdminConfigUpdateBody, request: Request) -> AdminConfigWriteBody:
    if not body.values:
        raise _bad_request("empty_update", "没有要改的键")

    unknown = sorted(set(body.values) - set(_WHITELIST))
    if unknown:
        raise _bad_request("unknown_key", f"这些键不能通过 Studio 修改：{', '.join(unknown)}")

    for key, value in body.values.items():
        if "\n" in value or "\r" in value:
            raise _bad_request("invalid_value", f"{key} 的值里不能有换行")

    settings: Settings = request.app.state.settings
    env_file, example_file = env_paths(request)
    written, created = await asyncio.to_thread(
        write_env_values, env_file, example_file, body.values
    )

    file_values = dotenv_store.parse_values(await _read_text(env_file))
    return AdminConfigWriteBody(
        env_file=str(env_file),
        created=created,
        written=written,
        restart_pending=_pending_keys(settings, file_values),
    )


# ── 路径与视图 ──────────────────────────────────────────


def env_paths(request: Request) -> tuple[Path, Path]:
    """``(.env, .env.example)``。路径由 ``create_app`` 放在 app.state 上（测试可覆盖）。

    :mod:`astra.api.routes.admin_system` 的"重启前先校验配置"用的是同一份路径解析。
    """
    env_file: Path = getattr(request.app.state, "env_file_path", None) or Path(".env").resolve()
    return env_file, env_file.with_name(".env.example")


async def _read_text(path: Path) -> str:
    return await asyncio.to_thread(_read_text_sync, path)


def _read_text_sync(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _field_view(settings: Settings, key: str, file_values: Mapping[str, str]) -> AdminConfigField:
    group, label = _WHITELIST[key]
    name = _field_name(key)
    info = Settings.model_fields[name]
    kind, choices = _kind_of(info)

    default = None if info.is_required() else _as_text(info.default)
    file_value = file_values.get(key)
    env_value = os.environ.get(key)

    if env_value is not None:
        source: _Source = "env"
        after_restart = env_value
    elif file_value is not None:
        source = "file"
        after_restart = file_value
    else:
        source = "default"
        after_restart = default or ""

    effective = _as_text(getattr(settings, name))
    return AdminConfigField(
        key=key,
        group=group,
        label=label,
        kind=kind,
        choices=choices,
        value=file_value if file_value is not None else (default or ""),
        effective=effective,
        default=default,
        source=source,
        restart_pending=not _same(kind, after_restart, effective),
    )


def _pending_keys(settings: Settings, file_values: Mapping[str, str]) -> list[str]:
    """哪些白名单键"重启之后生效值会变"（含"重启也不会变"的环境变量键的排除）。"""
    return sorted(
        field.key
        for field in (_field_view(settings, key, file_values) for key in _WHITELIST)
        if field.restart_pending
    )


def _field_name(key: str) -> str:
    return key.removeprefix("ASTRA_").lower()


def _kind_of(info: FieldInfo) -> tuple[_ValueKind, list[str]]:
    """从字段标注推导控件类型；``Literal`` 的候选直接给 UI 当枚举。"""
    annotation: object = info.annotation
    if get_origin(annotation) is Literal:
        return "enum", [str(item) for item in get_args(annotation)]
    if annotation is bool:
        return "bool", []
    if annotation is int:
        return "int", []
    if annotation is float:
        return "float", []
    if annotation is Path:
        return "path", []
    return "text", []


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _same(kind: _ValueKind, left: str, right: str) -> bool:
    try:
        return _coerce(kind, left) == _coerce(kind, right)
    except ValueError:
        # 值非法时退化成原样比较——"是不是同一份配置"不该因此炸掉
        return left.strip() == right.strip()


def _coerce(kind: _ValueKind, raw: str) -> object:
    text = raw.strip()
    if kind == "bool":
        return text.lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


# ── 落盘（同步，跑在线程里）─────────────────────────────


def write_env_values(
    env_file: Path, example_file: Path, values: Mapping[str, str]
) -> tuple[list[str], bool]:
    """读 → 改 → 校验 → 备份 → 落盘。返回 ``(写入的键, .env 是否这次新建)``。

    ADR 0029 起，人格页的"切换"（``/v1/admin/personas/{id}/activate``）与设置页
    共用这一条落盘路径——两处必须走同一套校验与备份，否则会一边有护栏、一边没有。
    """
    created = False
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
    elif example_file.exists():
        text = example_file.read_text(encoding="utf-8")
        created = True
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "missing_template",
                "message": f"既没有 {env_file} 也没有 {example_file}——仓库不完整",
            },
        )

    staged = env_file.with_name(f"{env_file.name}.candidate")
    staged.write_text(dotenv_store.render(text, values), encoding="utf-8")

    result = check_env_file(staged)
    if not result.ok:
        staged.unlink(missing_ok=True)
        raise _bad_request("invalid_value", result.message)

    if env_file.exists():
        shutil.copy2(env_file, env_file.with_name(f"{env_file.name}.bak"))
    os.replace(staged, env_file)
    return sorted(values), created


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )
