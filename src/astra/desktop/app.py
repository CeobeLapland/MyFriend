"""``astra desktop`` —— 桌面壳（ADR 0030）。

**它不是第二个应用**：界面还是 ``GET /studio`` 那一份页面，壳只做浏览器不做的事——
开一个独立窗口、常驻托盘、托管后端子进程、（P2 起）把 Unity 形象嵌进窗口。

进程模型：

```text
astra desktop（壳进程）
├─ 主线程：pywebview 窗口（WebView2）→ http://127.0.0.1:8000/studio
├─ 工作线程：run_supervised(child_command(), …, stop=停机事件)
│    └─ 后端子进程：python -m astra.cli serve --no-supervisor（ASTRA_SUPERVISED=1）
└─ 托盘线程：pystray（显示窗口 / 退出）
```

三条不变量：

1. **监督协议一行没改**（ADR 0026 §1）：后端请求重启（退出码 75）→ 壳把它拉起来，
   于是 Studio 的「保存并重启」在桌面壳下原样可用；
2. **依赖边界**：pywebview / pystray **只在本包导入**——``astra serve`` 与
   ``astra chat`` 的依赖面不因为 GUI 变大（``tests/unit/test_desktop_shell.py`` 钉住）；
3. **关窗 ≠ 退出**：关窗只是隐藏到托盘，退出是显式动作（托盘菜单 / 页面上的出口）；
   托盘没起来时例外——那种情况下关窗就是退出，免得窗口被藏得找不回来。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from astra.settings import Settings
from astra.supervisor import SUPERVISED_ENV, child_command, run_supervised

_logger = logging.getLogger("astra.desktop")

#: 窗口标题。单实例的第二个进程靠它把已有窗口唤到前台（ADR 0030 §2）
WINDOW_TITLE = "Astra"
#: 会话级命名互斥体（不用 ``Global\``：那需要额外权限，单用户桌面不需要）
MUTEX_NAME = "astra-desktop-single"

#: 探 ``/health`` 的超时。**实测踩过**：本机 LM Studio 在跑时一次 ``/health`` 要 ~2.4 秒
#: （探针自己也要连 :1234），最初写的 2 秒会把每一次都掐掉——于是"以为后端没起来"，
#: 窗口也白白等到启动超时才开。下游全挂时 ``/health`` 最长可到 ~6 秒，所以给到 8 秒。
_HEALTH_TIMEOUT_S = 8.0
#: 后端启动预算（uvicorn 实测 ~1.5 秒就绪；剩下的是第一次成功探测的余量）
_BACKEND_BOOT_TIMEOUT_S = 45.0
_STOP_JOIN_TIMEOUT_S = 20.0

#: 被保留的互斥体句柄（句柄一被回收，单实例就失效了）
_MUTEX_HANDLES: list[int] = []

_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9


# ── 纯函数（有单测）─────────────────────────────────────


def local_host(host: str) -> str:
    """把 ``0.0.0.0`` / ``::`` 这类通配地址换成能连的地址。"""
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host  # noqa: S104 - 只是字符串比较


def studio_url(settings: Settings) -> str:
    """壳加载的地址：**URL 而不是 ``file://``**（ADR 0030 的 Reason）。

    与浏览器里逐字节同一份页面——WebSocket 的相对路径、音频的同源语义全都不变。
    """
    return f"http://{local_host(settings.http_host)}:{settings.http_port}/studio"


def log_path(settings: Settings) -> Path:
    """后端子进程的输出落到哪里。

    GUI 启动没有控制台可看，所以子进程的 stdout/stderr 必须落文件；
    ``ASTRA_LOG_FILE`` 有配就尊重它，否则用 ``data/astra-desktop.log``。
    """
    if settings.log_file is not None:
        return settings.log_file
    return settings.data_dir / "astra-desktop.log"


# ── 页面 ↔ 壳 的窄桥 ────────────────────────────────────


class DesktopApi:
    """Studio 页面能调到的壳接口。**只有两个方法**（ADR 0030 §3）。

    不做通用 IPC——它不是插件平台。页面侧只在 ``window.pywebview`` 存在时启用这些行为，
    所以同一个 studio.html 在浏览器里照旧。
    """

    def __init__(self, quit_app: Callable[[], None]) -> None:
        self._quit_app = quit_app
        #: ``#avatar-slot`` 的最新矩形（P2 起用它摆 Unity 子窗口，ADR 0031）
        self.last_avatar_rect: dict[str, float] | None = None

    def avatar_rect(self, rect: dict[str, float]) -> bool:
        """记下 ``#avatar-slot`` 的矩形。P2 之前只是记账（返回 True 表示收到）。"""
        self.last_avatar_rect = {key: float(value) for key, value in rect.items()}
        return True

    def quit_app(self) -> None:
        """页面上的退出入口（托盘之外的第二出口）。**不阻塞 JS 调用**。"""
        threading.Thread(target=self._quit_app, name="astra-quit", daemon=True).start()


# ── 单实例 ──────────────────────────────────────────────


def acquire_single_instance() -> bool:
    """命名互斥体：已经有实例时返回 ``False``。

    拿不到互斥体（极小概率的系统错误）时**放行**——单实例是体验优化，
    不该因为一个系统调用失败就把应用挡在门外。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        _logger.warning(
            "单实例互斥体创建失败，按「没有别的实例」继续",
            extra={"event": "desktop.mutex.failed"},
        )
        return True

    _MUTEX_HANDLES.append(int(handle))
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def focus_existing_window() -> bool:
    """把已有实例的窗口唤到前台；找不到就返回 ``False``。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]

    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False

    user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


# ── 后端子进程 ──────────────────────────────────────────


@dataclass
class _Backend:
    """后端子进程：探测复用、拉起、等它健康、停机。"""

    settings: Settings
    stop: threading.Event
    #: 复用了外部已经跑着的后端（壳不负责它的生死）
    reused: bool = False
    _thread: threading.Thread | None = None
    _log_handle: IO[str] | None = None

    def probe(self) -> bool:
        """``/health`` 探一下——已有后端就不拉起第二个（ADR 0030 §1）。"""
        url = f"http://{local_host(self.settings.http_host)}:{self.settings.http_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as response:
                return int(response.status) == 200
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def start(self) -> None:
        path = log_path(self.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = path.open("a", encoding="utf-8")
        _logger.info(
            "后端输出重定向到 %s",
            path,
            extra={"event": "desktop.backend.log", "detail": {"path": str(path)}},
        )

        env = {**os.environ, SUPERVISED_ENV: "1"}
        self._thread = threading.Thread(
            target=self._supervise, args=(env,), name="astra-backend", daemon=True
        )
        self._thread.start()

    def _supervise(self, env: dict[str, str]) -> None:
        code = run_supervised(
            child_command(),
            env,
            stop=self.stop,
            popen_kwargs={"stdout": self._log_handle, "stderr": subprocess.STDOUT},
        )
        _logger.warning(
            "后端监督循环结束",
            extra={"event": "desktop.backend.exit", "detail": {"code": code}},
        )

    def wait_until_up(self, timeout_s: float = _BACKEND_BOOT_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.probe():
                return True
            time.sleep(0.4)
        return False

    def stop_and_join(self, timeout_s: float = _STOP_JOIN_TIMEOUT_S) -> None:
        """停机：置位事件 → 等监督线程收尾（它会 terminate 子进程）。"""
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


# ── 壳 ──────────────────────────────────────────────────


class DesktopShell:
    """壳的全部状态与生命周期。``run_desktop`` 是它唯一的入口。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._backend = _Backend(settings=settings, stop=self._stop)
        self._window: Any = None
        self._tray: Any = None
        self._quitting = False
        self.api = DesktopApi(self.quit)

    # ── 生命周期 ────────────────────────────────────────

    def run(self) -> int:
        """跑起来（GUI 在主线程，所以这个调用直到窗口关闭才返回）。"""
        import webview  # 延迟导入：依赖边界见模块文档字符串

        if not acquire_single_instance():
            self._report_second_instance()
            return 0

        self._start_backend()

        self._window = webview.create_window(
            WINDOW_TITLE,
            studio_url(self._settings),
            js_api=self.api,
            width=1180,
            height=820,
            min_size=(900, 620),
        )
        self._window.events.closing += self._on_closing
        try:
            webview.start(self._after_gui_started)
        finally:
            self._stop_everything()
        return 0

    def _start_backend(self) -> None:
        if self._backend.probe():
            self._backend.reused = True
            _logger.info(
                "检测到已有后端在跑，复用它（这个壳不负责它的生死）",
                extra={"event": "desktop.backend.reused"},
            )
            return

        self._backend.start()
        if self._backend.wait_until_up():
            _logger.info("后端已就绪", extra={"event": "desktop.backend.ready"})
        else:
            _logger.warning(
                "后端在 %s 秒内没起来；窗口照开，页面会自己显示离线（看 %s）",
                _BACKEND_BOOT_TIMEOUT_S,
                log_path(self._settings),
                extra={"event": "desktop.backend.timeout"},
            )

    def _after_gui_started(self) -> None:
        """GUI 起好之后（pywebview 在另一个线程里调它）：装托盘。"""
        try:
            self._tray = _start_tray(self)
        except Exception:
            _logger.warning(
                "托盘没起来——这种情况下关窗就是退出（不把窗口藏得找不回来）",
                exc_info=True,
                extra={"event": "desktop.tray.failed"},
            )

    def _on_closing(self) -> bool:
        """关窗 = 隐藏到托盘；没有托盘时放行关闭（那就是退出）。"""
        if self._tray is None:
            return True
        self._window.hide()
        return False

    def _stop_everything(self) -> None:
        self._backend.stop_and_join()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:  # pragma: no cover - 托盘已经坏了，不该阻止退出
                _logger.warning("停托盘时出错", exc_info=True, extra={"event": "desktop.tray.stop"})

    # ── 托盘动作 ────────────────────────────────────────

    def show_window(self) -> None:
        if self._window is not None:
            self._window.show()
            self._window.restore()

    def quit(self) -> None:
        """退出：停托盘 → 停后端 → 关窗口（顺序见 ADR 0030 §1）。"""
        if self._quitting:
            return
        self._quitting = True
        self._stop_backend_and_close()

    def _stop_backend_and_close(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:  # pragma: no cover
                _logger.warning("停托盘时出错", exc_info=True, extra={"event": "desktop.tray.stop"})
        self._backend.stop_and_join()
        if self._window is not None:
            self._window.destroy()

    def _report_second_instance(self) -> None:
        if focus_existing_window():
            _logger.info(
                "已经有一个 Astra 在跑，把它的窗口唤到前台", extra={"event": "desktop.second"}
            )
        else:
            _logger.warning(
                "已经有一个 Astra 在跑，但没找到它的窗口（可能还在启动）",
                extra={"event": "desktop.second.nohwnd"},
            )


def _start_tray(shell: DesktopShell) -> Any:  # noqa: ANN401 - pystray 没有 stub（见 pyproject）
    """托盘：显示窗口、退出。pystray 在自己的线程里跑消息循环。"""
    import pystray
    from PIL import Image

    path = icon_path()
    image = Image.open(path) if path.exists() else _fallback_icon()

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", lambda: shell.show_window(), default=True),
        pystray.MenuItem("退出", lambda: shell.quit()),
    )
    icon = pystray.Icon("astra", image, WINDOW_TITLE, menu)
    threading.Thread(target=icon.run, name="astra-tray", daemon=True).start()
    return icon


def icon_path() -> Path:
    """``assets/astra.ico``（仓库根下）。缺文件时托盘会退回一个占位图标。"""
    return Path(__file__).resolve().parents[3] / "assets" / "astra.ico"


def _fallback_icon() -> Any:  # noqa: ANN401 - PIL 的 Image 没有对外可见的类型标注
    """没有图标文件时的占位——少一个资源不该让壳起不来。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (14, 16, 20, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 20, 44, 44), fill=(92, 245, 168, 255))
    return image


def run_desktop(settings: Settings) -> int:
    """``astra desktop`` 的入口。"""
    return DesktopShell(settings).run()
