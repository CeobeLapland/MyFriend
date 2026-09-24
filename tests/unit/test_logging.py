"""``astra.logging_setup`` 的单元测试。

覆盖：JSON 字段结构、extra 白名单、敏感键脱敏、异常序列化、UTF-8 文件输出。
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from astra.logging_setup import (
    REDACTED,
    JsonFormatter,
    SensitiveKeyFilter,
    configure_logging,
)

_LOGGER_NAME = "astra.test.capture"


def _emit(level: int = logging.INFO, message: str = "hello", **extra: object) -> str:
    """把一条日志写进内存缓冲并返回原始输出。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveKeyFilter())

    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.log(level, message, extra=extra)
    return stream.getvalue().strip()


def _emit_json(**extra: object) -> dict[str, object]:
    return json.loads(_emit(**extra))


@pytest.fixture
def isolated_root_logging() -> Iterator[None]:
    """隔离 root logger 的 handler / level，供 configure_logging 测试使用。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level

    yield

    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)


# ── JsonFormatter ───────────────────────────────────────


def test_base_fields_present() -> None:
    payload = _emit_json()

    assert payload["level"] == "INFO"
    assert payload["logger"] == _LOGGER_NAME
    assert payload["msg"] == "hello"
    assert isinstance(payload["ts"], str)
    assert payload["ts"].endswith("+00:00"), "时间戳必须带 UTC 时区"


def test_allowlisted_extra_is_included() -> None:
    payload = _emit_json(event="turn.started", latency_ms=12.5, provider="lmstudio")

    assert payload["event"] == "turn.started"
    assert payload["latency_ms"] == 12.5
    assert payload["provider"] == "lmstudio"


def test_non_allowlisted_extra_is_dropped() -> None:
    """白名单之外的 extra 不应进入日志，避免未预期字段泄露。"""
    payload = _emit_json(unexpected_field="leaky")

    assert "unexpected_field" not in payload


def test_sensitive_extra_is_not_logged() -> None:
    """`content` / `token` 不在 extra 白名单里，因此根本不会进入日志。"""
    payload = _emit_json(content="用户说了一段很私密的话", token="sk-abcdef")

    assert "content" not in payload
    assert "token" not in payload
    assert "私密" not in json.dumps(payload, ensure_ascii=False)


def test_sensitive_filter_redacts_any_sensitive_key() -> None:
    """``SensitiveKeyFilter`` 独立于白名单：任何敏感键的值都会被替换为 ``[redacted]``。

    这是第二层防护——即使将来有人把敏感字段加进 ``EXTRA_ALLOWLIST``，
    值也不会以明文出现在日志里。
    """
    record = logging.LogRecord(
        name="astra.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    record.__dict__["content"] = "private"
    record.__dict__["API_KEY"] = "sk-abcdef"
    record.__dict__["event"] = "keep-me"

    assert SensitiveKeyFilter().filter(record) is True
    assert record.__dict__["content"] == REDACTED
    assert record.__dict__["API_KEY"] == REDACTED
    assert record.__dict__["event"] == "keep-me"


def test_exception_is_serialized() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("astra.test.exc")
    logger.handlers = [handler]
    logger.propagate = False

    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError:
        logger.exception("operation failed")

    payload = json.loads(stream.getvalue().strip())
    assert payload["msg"] == "operation failed"
    assert "ValueError: boom" in str(payload["exc"])


def test_output_is_single_line() -> None:
    raw = _emit(message="line one\nline two")

    assert "\n" not in raw
    assert json.loads(raw)["msg"] == "line one\nline two"


# ── configure_logging ───────────────────────────────────


def test_configure_logging_json_stream(
    isolated_root_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", fmt="json")

    logging.getLogger("astra.test.stream").info("hi", extra={"event": "boot"})

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["msg"] == "hi"
    assert payload["event"] == "boot"


def test_configure_logging_text_format(
    isolated_root_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", fmt="text")

    logging.getLogger("astra.test.text").info("plain message")

    out = capsys.readouterr().out
    assert "plain message" in out
    assert not out.strip().startswith("{")


def test_configure_logging_respects_level(
    isolated_root_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="WARNING", fmt="json")

    logger = logging.getLogger("astra.test.level")
    logger.info("should be dropped")
    logger.warning("should appear")

    out = capsys.readouterr().out
    assert "should be dropped" not in out
    assert "should appear" in out


def test_configure_logging_is_idempotent(isolated_root_logging: None) -> None:
    configure_logging(level="INFO")
    configure_logging(level="INFO")

    assert len(logging.getLogger().handlers) == 1


def test_configure_logging_writes_utf8_file(isolated_root_logging: None, tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "astra.log"
    configure_logging(level="INFO", fmt="json", log_file=log_file)

    logging.getLogger("astra.test.file").info("中文日志不应该乱码")

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["msg"] == "中文日志不应该乱码"


def test_configure_logging_creates_parent_dirs(isolated_root_logging: None, tmp_path: Path) -> None:
    log_file = tmp_path / "a" / "b" / "c" / "astra.log"
    configure_logging(level="INFO", log_file=log_file)

    assert log_file.parent.is_dir()
