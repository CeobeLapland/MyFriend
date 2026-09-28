"""用候选 ``.env`` 实例化一次 ``Settings``——写配置前的最后一道门（ADR 0026 §3）。

**必须起子进程、并且清掉 ``ASTRA_*`` 进程变量**：进程环境变量的优先级高于 ``.env``，
在本进程里校验会验到"启动时内联的那套值"而不是候选文件本身。

被校验的是**整份候选文件**，所以跨字段约束（例如五条检索权重之和必须为 1.0）
也能挡住——这正是"写坏 = 下次启动直接失败"（``extra="forbid"``）需要的前置防线。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: 子进程只是 import + 实例化 pydantic 模型；给足余量但不无限等
_DEFAULT_TIMEOUT_S = 60.0

#: 回给 UI 的错误摘要最多列几条——够定位就行，不要把整个 traceback 甩出去
_MAX_ERRORS = 8


@dataclass(frozen=True, slots=True)
class EnvCheckResult:
    ok: bool
    message: str


def check_env_file(path: Path, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> EnvCheckResult:
    """在干净环境里用 ``path`` 实例化 ``Settings``。"""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("ASTRA_")}

    try:
        completed = subprocess.run(  # noqa: S603 - 参数固定，无 shell，不涉及用户输入拼接
            [sys.executable, "-m", "astra.settings.env_check", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return EnvCheckResult(ok=False, message=f"校验超时（{timeout_s:.0f} 秒）")

    payload = _parse_payload(completed.stdout)
    if payload is None:
        detail = (completed.stderr or "").strip()[:400]
        return EnvCheckResult(
            ok=False,
            message=f"校验进程异常退出（code={completed.returncode}）：{detail}",
        )
    return EnvCheckResult(ok=bool(payload.get("ok")), message=str(payload.get("message", "")))


def _parse_payload(stdout: str) -> dict[str, object] | None:
    """取最后一行 JSON——子进程可能被别的东西（进度条、告警）污染过 stdout。"""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _summarize(exc: Exception) -> str:
    """把 pydantic 的校验错误压成一行中文可读的摘要。"""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:_MAX_ERRORS]:
            location = ".".join(str(item) for item in error.get("loc", ()))
            parts.append(f"{location}: {error.get('msg')}")
        return "；".join(parts)
    return f"{type(exc).__name__}: {exc}"


def _main(argv: list[str]) -> int:
    """子进程入口：``python -m astra.settings.env_check <候选文件>``。"""
    from astra.settings import Settings

    path = Path(argv[1])
    try:
        Settings(_env_file=path)
    except Exception as exc:  # 任何异常都要如实回给 UI，而不是崩掉
        _emit({"ok": False, "message": _summarize(exc)})
        return 1

    _emit({"ok": True, "message": ""})
    return 0


def _emit(payload: dict[str, object]) -> None:
    # 用 stdout.write 而不是 print：这是一个机器可读的子进程协议（也避开 T20 规则）
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
