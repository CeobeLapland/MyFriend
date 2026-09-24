"""命令行入口。

astra serve     启动 HTTP 服务
astra migrate   应用数据库迁移（幂等）
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import uvicorn

from astra import __version__
from astra.infrastructure.db.migrations import upgrade
from astra.logging_setup import configure_logging
from astra.settings import Settings, get_settings

_logger = logging.getLogger("astra.cli")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra", description="Astra — local-first AI companion")
    parser.add_argument("--version", action="version", version=f"astra {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="启动 HTTP 服务")
    subparsers.add_parser("migrate", help="应用数据库迁移（幂等）")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "migrate":
        return _migrate(settings)
    return _serve(settings)


def _migrate(settings: Settings) -> int:
    _setup_logging(settings)

    applied = upgrade(settings.db_path)
    if applied:
        _logger.info(
            "applied migrations: %s",
            ", ".join(applied),
            extra={"event": "db.migrated", "detail": applied},
        )
    else:
        _logger.info("database already up to date", extra={"event": "db.up_to_date"})

    return 0


def _serve(settings: Settings) -> int:
    _setup_logging(settings)

    # log_config=None：避免 uvicorn 自己再装一套 handler，导致格式不一致与重复输出
    uvicorn.run(
        "astra.main:app",
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
    )
    return 0


def _setup_logging(settings: Settings) -> None:
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=settings.log_file,
    )


if __name__ == "__main__":
    sys.exit(main())
