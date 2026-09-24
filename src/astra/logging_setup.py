"""结构化日志。

设计要点：

- 使用 stdlib ``logging`` + 自写 ``JsonFormatter``，不引入 structlog / loguru。
- **两层隐私控制**（依据 `docs/11_SECURITY_AND_PRIVACY.md` §6：默认不记录原始音频、
  摄像头帧、完整文件内容、API token）：

  1. ``EXTRA_ALLOWLIST`` —— **schema 闸门**。不在白名单里的 ``extra`` 字段
     根本不会进入日志。这是当前生效的主力机制：``content`` / ``text`` /
     ``prompt`` / ``token`` 都不在白名单里。
  2. ``SensitiveKeyFilter`` —— **脱敏**。把 ``extra`` 中敏感键的值替换为
     ``[redacted]``。白名单收紧时它是冗余的，但只要将来有人往白名单里加了
     敏感字段，它就会兜住。属于刻意的纵深防御，不是死代码。
- **Windows 编码**：handler 显式 ``encoding="utf-8"``。日志被重定向到管道/文件时
  Python 会退化为 locale 编码（中文环境为 cp936），不加这一项会导致中文日志
  ``UnicodeEncodeError``。

已知局限：过滤只能作用于 ``extra`` 的**键**。如果调用方把用户内容拼进
``logger.info(f"...{text}")`` 的 message 里，过滤无法拦截。因此约定：
**日志调用只传元数据，绝不传用户内容**。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "[redacted]"

#: extra 中值必须被脱敏的键（小写比较）
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "audio",
        "authorization",
        "content",
        "cookie",
        "file_content",
        "messages",
        "password",
        "prompt",
        "refresh_token",
        "reply",
        "secret",
        "secret_key",
        "text",
        "token",
    }
)

#: ``LogRecord`` 的内置属性，不算 extra
_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
    }
)

#: 允许出现在日志中的 extra 字段。新增日志字段时需要同步加到这里。
EXTRA_ALLOWLIST: frozenset[str] = frozenset(
    {
        "component",
        "conversation_id",
        "correlation_id",
        "detail",
        "duration_ms",
        "event",
        "latency_ms",
        "method",
        "model",
        "path",
        "provider",
        "request_id",
        "status",
        "status_code",
        "tool_name",
        "turn_id",
    }
)


class SensitiveKeyFilter(logging.Filter):
    """把 ``extra`` 中敏感键的值替换为 ``[redacted]``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key in _RESERVED:
                continue
            if key.lower() in SENSITIVE_KEYS:
                record.__dict__[key] = REDACTED
        return True


class JsonFormatter(logging.Formatter):
    """把 ``LogRecord`` 序列化为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED or key not in EXTRA_ALLOWLIST:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_formatter(fmt: str) -> logging.Formatter:
    if fmt == "json":
        return JsonFormatter()
    return logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | None = None,
) -> None:
    """配置 root logger。

    幂等：重复调用会先移除并关闭已有 handler，避免日志重复输出。

    调用方（``astra.cli``）应在创建 uvicorn 之前调用本函数，并向 uvicorn 传
    ``log_config=None``，否则 uvicorn 会自己装一套 handler 导致格式不一致。
    """
    if hasattr(sys.stdout, "reconfigure"):
        # 保证重定向到管道/文件时中文不会因 cp936 报错
        sys.stdout.reconfigure(encoding="utf-8")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level.upper())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(_build_formatter(fmt))
    stream_handler.addFilter(SensitiveKeyFilter())
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(_build_formatter(fmt))
        file_handler.addFilter(SensitiveKeyFilter())
        root.addHandler(file_handler)
