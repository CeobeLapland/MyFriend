"""Studio 自己的小状态：**只记"我下发给 ``lms`` 的声明值"**（ADR 0025 §6 / ADR 0026 §4）。

显存归属（模型在 GPU 还是 CPU）在 LM Studio 侧**查不到**（实测：``lms ps --json`` 与
REST 都没有这个字段），所以 Studio 只能记录自己下发的 ``--gpu`` 参数，并在 UI 上
明确标注"声明，不是实测"。

这份文件放在 ``data/``（用户数据目录），**不是配置真相**——配置的唯一真相仍是 ``.env``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: 相对 ``settings.data_dir``。**不叫 settings 或 config**——它只是一条声明记录
FILE_NAME = "studio-state.json"

_KEY = "declared_load"


@dataclass(frozen=True, slots=True)
class DeclaredLoad:
    model_key: str
    gpu: str
    context_length: int | None
    at: str  # ISO 8601（带时区）


def state_path(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def read_declared(path: Path) -> DeclaredLoad | None:
    """读不到 / 读坏了都返回 ``None``——这只影响一句提示，不该让页面报错。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    entry = payload.get(_KEY)
    if not isinstance(entry, dict):
        return None

    model_key = entry.get("model_key")
    gpu = entry.get("gpu")
    at = entry.get("at")
    if not isinstance(model_key, str) or not isinstance(gpu, str) or not isinstance(at, str):
        return None

    context_length = entry.get("context_length")
    return DeclaredLoad(
        model_key=model_key,
        gpu=gpu,
        context_length=context_length if isinstance(context_length, int) else None,
        at=at,
    )


def write_declared(path: Path, declared: DeclaredLoad) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        _KEY: {
            "model_key": declared.model_key,
            "gpu": declared.gpu,
            "context_length": declared.context_length,
            "at": declared.at,
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
