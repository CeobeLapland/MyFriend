"""桌面壳（ADR 0030）。

**pywebview / pystray 只在这个包里导入，而且是函数内延迟导入**——
``astra serve`` 与 ``astra chat`` 的依赖面不因为 GUI 变大，
由 ``tests/unit/test_desktop_shell.py`` 钉住。
"""

from __future__ import annotations

from astra.desktop.app import (
    DesktopApi,
    DesktopShell,
    icon_path,
    local_host,
    log_path,
    run_desktop,
    studio_url,
)

__all__ = [
    "DesktopApi",
    "DesktopShell",
    "icon_path",
    "local_host",
    "log_path",
    "run_desktop",
    "studio_url",
]
