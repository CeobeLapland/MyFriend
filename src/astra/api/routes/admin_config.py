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
from pydantic import BaseModel, Field, SecretStr
from pydantic.fields import FieldInfo

from astra.infrastructure import dotenv_store
from astra.settings import Settings
from astra.settings.env_check import check_env_file

router = APIRouter(prefix="/v1/admin", tags=["admin"])

#: 白名单：键 → (分组, 中文标签)。**只放用户真的会改的键**。
#:
#: 密钥（``SecretStr``）从 ADR 0032 起也在这里，但走的是**只写不读**那条路：
#: 读接口只报"已配置 / 未配置"，表单留空表示不改动（见 :func:`_secret_view`）。
_WHITELIST: dict[str, tuple[str, str]] = {
    "ASTRA_LLM_PROVIDER": ("LLM", "提供方"),
    "ASTRA_LLM_MODEL": ("LLM", "模型"),
    "ASTRA_LLM_FALLBACK_MODEL": ("LLM", "降级模型（显存不足时）"),
    "ASTRA_LLM_BASE_URL": ("LLM", "服务地址"),
    "ASTRA_LLM_API_KEY": ("LLM", "API 密钥（留空不改动）"),
    "ASTRA_LLM_TEMPERATURE": ("LLM", "温度"),
    "ASTRA_LLM_MAX_TOKENS": ("LLM", "单次回复 token 上限"),
    "ASTRA_LLM_REASONING_EFFORT": ("LLM", "思考深度"),
    "ASTRA_LLM_SEND_REASONING_EFFORT": ("LLM", "发送 reasoning_effort（多数第三方不支持）"),
    "ASTRA_LLM_USE_JSON_SCHEMA": ("LLM", "结构化输出用 json_schema（第三方常不支持）"),
    "ASTRA_EMBEDDING_PROVIDER": ("记忆", "向量化提供方"),
    "ASTRA_EMBEDDING_MODEL": ("记忆", "向量化模型"),
    "ASTRA_EMBEDDING_BASE_URL": ("记忆", "向量化服务地址"),
    "ASTRA_EMBEDDING_API_KEY": ("记忆", "向量化 API 密钥（留空不改动）"),
    "ASTRA_MEMORY_ENABLED": ("记忆", "启用记忆（启动期默认值）"),
    "ASTRA_MEMORY_MAX_CANDIDATES_PER_TURN": ("记忆", "每轮最多自动记几条（0 = 不自动记）"),
    "ASTRA_MEMORY_TOP_K": ("记忆", "每轮注入几条记忆（0 = 不注入）"),
    "ASTRA_STT_PROVIDER": ("语音", "语音识别（STT）"),
    "ASTRA_TTS_PROVIDER": ("语音", "语音合成（TTS）"),
    "ASTRA_TTS_BASE_URL": ("语音", "语音合成地址（cosyvoice 与第三方共用）"),
    "ASTRA_TTS_VOICE": ("语音", "音色（cosyvoice 是预设名，第三方是后端音色 id）"),
    "ASTRA_TTS_REFERENCE_AUDIO": ("语音", "参考音频（zero-shot 克隆；留空用预置音色）"),
    "ASTRA_STT_BASE_URL": ("语音", "语音识别地址（只用于第三方）"),
    "ASTRA_STT_MODEL": ("语音", "语音识别模型（第三方）"),
    "ASTRA_STT_LANGUAGE": ("语音", "识别语言提示，如 zh（只用于第三方；留空自动）"),
    "ASTRA_STT_API_KEY": ("语音", "语音识别 API 密钥（留空不改动）"),
    "ASTRA_TTS_MODEL": ("语音", "语音合成模型（第三方）"),
    "ASTRA_TTS_RESPONSE_FORMAT": ("语音", "合成音频格式（第三方；pcm 是裸流）"),
    "ASTRA_TTS_PCM_SAMPLE_RATE": ("语音", "裸流采样率（只在格式选 pcm 时有意义）"),
    "ASTRA_TTS_API_KEY": ("语音", "语音合成 API 密钥（留空不改动）"),
    "ASTRA_ENV": ("运行", "运行环境（prod 会关掉 /dev）"),
    "ASTRA_AVATAR_ENABLED": ("运行", "形象客户端已接入（纯通告）"),
    "ASTRA_AVATAR_PLAYER_PATH": ("运行", "Unity 播放器路径（桌面壳的角色区；留空不嵌）"),
    "ASTRA_LOG_LEVEL": ("运行", "日志级别"),
}

#: ``Settings`` 认得的全部键。**这是权威集合**（``env_prefix="ASTRA_"``、无 alias），
#: 白名单必须是它的子集，``.env`` 里出现它之外的键就等于下次启动直接失败
_KNOWN_KEYS = frozenset(f"ASTRA_{name.upper()}" for name in Settings.model_fields)

_ValueKind = Literal["enum", "bool", "int", "float", "text", "path", "secret"]
_Source = Literal["env", "file", "default"]


class AdminConfigField(BaseModel):
    key: str
    group: str
    label: str
    kind: _ValueKind
    choices: list[str] = Field(default_factory=list)
    #: ``.env`` 里的值（文件里没有则是字段默认值）——表单里编辑的就是它。
    #: **密钥恒为空串**：表单留空表示"不改动"，所以没有值需要回显（ADR 0032）。
    value: str
    #: 当前进程实际生效的值。密钥在这里是"已配置 / 未配置"，绝不出现明文。
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
    unknown = sorted(set(body.values) - set(_WHITELIST))
    if unknown:
        raise _bad_request("unknown_key", f"这些键不能通过 Studio 修改：{', '.join(unknown)}")

    # 密钥不回显，所以"留空"只能解释成**不改动**——否则每次保存都会把已配好的
    # 密钥清成空串。非密钥键的留空照旧是"真的清空"（例如清掉参考音频路径）。
    values = {
        key: value for key, value in body.values.items() if value.strip() or not _is_secret_key(key)
    }
    if not values:
        raise _bad_request("empty_update", "没有要改的键")

    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise _bad_request("invalid_value", f"{key} 的值里不能有换行")

    settings: Settings = request.app.state.settings
    env_file, example_file = env_paths(request)
    written, created = await asyncio.to_thread(write_env_values, env_file, example_file, values)

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

    file_value = file_values.get(key)
    env_value = os.environ.get(key)
    default = None if info.is_required() else _as_text(info.default)

    if env_value is not None:
        source: _Source = "env"
        after_restart = env_value
    elif file_value is not None:
        source = "file"
        after_restart = file_value
    else:
        source = "default"
        after_restart = default or ""

    if kind == "secret":
        return _secret_view(
            key=key,
            group=group,
            label=label,
            effective=_secret_value(settings, name),
            # 文件 / 环境变量里放着明文，可以直接比；"哪都没有"时要拿字段的**真默认值**
            # 兜底——`_as_text(SecretStr)` 得到的是掩码（``**********``），拿它比较
            # 会把每个密钥都误报成"重启后生效"。
            after_restart=_secret_default(info) if source == "default" else after_restart,
            source=source,
        )

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


def _secret_view(
    *,
    key: str,
    group: str,
    label: str,
    effective: str,
    after_restart: str,
    source: _Source,
) -> AdminConfigField:
    """密钥字段的对外视图：**只报"有没有配"，不报值**（ADR 0032）。

    ``value`` 恒为空——表单留空表示"不改动"，所以后端没有值需要回显，用户也就不
    可能从接口里读出密钥。``restart_pending`` 用**真值**在服务端算，只把布尔结论
    发出去：这既回答了"重启会不会变"，又不需要暴露任何一个字符。
    """
    return AdminConfigField(
        key=key,
        group=group,
        label=label,
        kind="secret",
        choices=[],
        value="",
        effective="已配置" if effective.strip() else "未配置",
        default=None,
        source=source,
        restart_pending=after_restart != effective,
    )


def _secret_value(settings: Settings, name: str) -> str:
    raw = getattr(settings, name)
    return raw.get_secret_value() if isinstance(raw, SecretStr) else str(raw)


def _secret_default(info: FieldInfo) -> str:
    raw = info.default
    return raw.get_secret_value() if isinstance(raw, SecretStr) else ""


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
    if annotation is SecretStr:
        return "secret", []
    if annotation is bool:
        return "bool", []
    if annotation is int:
        return "int", []
    if annotation is float:
        return "float", []
    if annotation is Path:
        return "path", []
    return "text", []


def _is_secret_key(key: str) -> bool:
    kind, _ = _kind_of(Settings.model_fields[_field_name(key)])
    return kind == "secret"


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
