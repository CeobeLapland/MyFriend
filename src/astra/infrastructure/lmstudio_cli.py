"""LM Studio 的 ``lms`` CLI 封装（ADR 0026 §4）。

**为什么走 CLI 而不是 REST**：REST 侧查不到"模型在 GPU 还是 CPU"（ADR 0025 §6 的实测），
而加载 / 卸载 / 服务开关在 CLI 上都是确定的路径（``lms load --gpu ...`` 实测 ≈5s）。

**解析与执行分开**：解析器只用固定样本做单测，执行只负责起进程、超时、
把原始 stdout/stderr 一起带回来（命令失败时 UI 要能看到原文）。

``lms ps --json`` 与 ``lms ls --json`` 的字段取自本机实测输出；解析器全部**防御式**
（缺字段不炸、类型不对就跳过），因为 CLI 输出会随 LM Studio 版本变。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

#: ``lms server status`` 的三种结论
ServerState = Literal["running", "stopped", "unknown"]

DEFAULT_TIMEOUT_S = 30.0
#: 实测加载 ≈5s（4B、warm cache）——首次加载 / 大模型要更久，给足余量
LOAD_TIMEOUT_S = 120.0
SERVER_TIMEOUT_S = 60.0

#: 回给 UI 的原始输出上限（命令可能很啰嗦，别把整页刷爆）
_MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True, slots=True)
class LmsResult:
    ok: bool
    code: int | None
    stdout: str
    stderr: str
    #: 失败原因（找不到 CLI / 超时 / 退出码），已截断
    message: str = ""

    def last_json(self) -> object | None:
        """取 stdout 里最后一段可解析的 JSON（``lms ... --json`` 的产物）。"""
        text = self.stdout.strip()
        if not text:
            return None

        parsed: object
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = _trailing_json(text)
        return parsed


@dataclass(frozen=True, slots=True)
class LmsModel:
    key: str
    display_name: str
    model_type: str
    params: str | None
    size_bytes: int | None
    max_context: int | None


@dataclass(frozen=True, slots=True)
class LmsLoadedModel:
    identifier: str
    model_key: str | None
    context_length: int | None
    size_bytes: int | None


def cli_path() -> str | None:
    """``lms`` 可执行文件。实测它在默认 PATH 上（``~/.lmstudio/bin/lms.exe``）。"""
    return shutil.which("lms")


def unavailable_result() -> LmsResult:
    return LmsResult(
        ok=False,
        code=None,
        stdout="",
        stderr="",
        message="找不到 lms（LM Studio 的命令行工具）。装 LM Studio 时会自带；"
        "如果装在别处，把它加进 PATH 再重启后端。",
    )


async def run_lms(args: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> LmsResult:
    """起一次 ``lms``，把结果原样带回来。**不抛异常**——失败也要能如实显示。"""
    executable = cli_path()
    if executable is None:
        return unavailable_result()

    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # 极端情况：文件在 but 不可执行
        return LmsResult(ok=False, code=None, stdout="", stderr="", message=f"启动 lms 失败：{exc}")

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return LmsResult(
            ok=False,
            code=None,
            stdout="",
            stderr="",
            message=f"`lms {' '.join(args)}` 超过 {timeout_s:.0f} 秒还没结束，已终止",
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")[:_MAX_OUTPUT_CHARS]
    stderr = stderr_bytes.decode("utf-8", errors="replace")[:_MAX_OUTPUT_CHARS]
    return LmsResult(
        ok=process.returncode == 0,
        code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        message="" if process.returncode == 0 else f"lms 退出码 {process.returncode}",
    )


# ── 解析 ────────────────────────────────────────────────


def _trailing_json(text: str) -> object | None:
    """CLI 有时会在 JSON 前面混进人类可读的日志：从最后一个 ``[`` / ``{`` 再试一次。"""
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        candidate: object
        try:
            candidate = json.loads(text[index:])
        except json.JSONDecodeError:
            continue
        return candidate
    return None


def parse_models(payload: object) -> list[LmsModel]:
    """``lms ls --json``：本机实测字段见 ADR 0026 的实测表。"""
    if not isinstance(payload, list):
        return []

    models: list[LmsModel] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = _as_text(item.get("modelKey") or item.get("path"))
        if key is None:
            continue
        models.append(
            LmsModel(
                key=key,
                display_name=_as_text(item.get("displayName")) or key,
                model_type=_as_text(item.get("type")) or "unknown",
                params=_as_text(item.get("paramsString")),
                size_bytes=_as_int(item.get("sizeBytes")),
                max_context=_as_int(item.get("maxContextLength")),
            )
        )
    return models


def parse_loaded(payload: object) -> list[LmsLoadedModel]:
    """``lms ps --json``。字段名在 LM Studio 各版本间有出入，所以两个都认。"""
    if not isinstance(payload, list):
        return []

    loaded: list[LmsLoadedModel] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        identifier = _as_text(item.get("identifier") or item.get("modelKey"))
        if identifier is None:
            continue
        loaded.append(
            LmsLoadedModel(
                identifier=identifier,
                model_key=_as_text(item.get("modelKey")),
                context_length=_as_int(item.get("contextLength")),
                size_bytes=_as_int(item.get("sizeBytes")),
            )
        )
    return loaded


def parse_server_status(text: str) -> ServerState:
    """``lms server status`` 输出的是英文文案，**不能靠退出码**。"""
    lowered = text.lower()
    if "not running" in lowered or "stopped" in lowered:
        return "stopped"
    if "running" in lowered or "listening" in lowered:
        return "running"
    return "unknown"


def _as_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
