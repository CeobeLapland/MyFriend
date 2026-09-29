"""``astra desktop`` —— 桌面壳（ADR 0030）。

**它不是第二个应用**：界面还是 ``GET /studio`` 那一份页面，壳只做浏览器不做的事——
开一个独立窗口、常驻托盘、托管后端子进程、（P2 起）把 Unity 形象嵌进窗口。

进程模型：

```text
astra desktop（壳进程）
├─ 主线程：pywebview 窗口（WebView2）→ http://127.0.0.1:8000/studio
├─ 工作线程：run_supervised(child_command(), …, stop=停机事件)
│    └─ 后端子进程：python -m astra.cli serve --no-supervisor（ASTRA_SUPERVISED=1）
├─ 托盘线程：pystray（显示窗口 / 退出）
└─ 角色子进程：Builds/Windows/Astra.exe -parentHWND <壳窗口>（ADR 0031，可选）
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

from astra.desktop import win32
from astra.desktop.avatar import AvatarHost, AvatarState
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

#: pywebview 在 Windows 上的窗口类前缀（找"我们自己的窗口"用）
_FORM_CLASS_PREFIX = "WindowsForms10"


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
    """Studio 页面能调到的壳接口。**三个方法、都很窄**（ADR 0030 §3 + P2 修订）。

    不做通用 IPC——它不是插件平台。页面侧只在 ``window.pywebview`` 存在时启用这些行为，
    所以同一个 studio.html 在浏览器里照旧。
    """

    def __init__(self, quit_app: Callable[[], None], avatar: AvatarHost | None = None) -> None:
        self._quit_app = quit_app
        self._avatar = avatar
        #: 页面最近报上来的 ``#avatar-slot`` 矩形与开关（诊断/测试用）
        self.last_avatar_rect: dict[str, object] | None = None

    def avatar_rect(self, rect: dict[str, object]) -> bool:
        """页面报来 ``#avatar-slot`` 的矩形 + 两个开关（P2 修订后的载荷）：

        ``{x, y, width, height, visible, enabled}``——
        ``enabled=False`` 是"关掉「显示角色」"（结束进程，省显存），
        ``visible=False`` 只是"这块区域现在看不见"（藏起来，进程留着）。
        """
        self.last_avatar_rect = dict(rect)
        if self._avatar is None:
            return False
        return self._avatar.apply(rect)

    def avatar_state(self) -> dict[str, object]:
        """只读状态：配没配播放器、在不在跑、以及一句人话。"""
        if self._avatar is None:
            return {"available": False, "running": False, "message": "这个进程不是桌面壳"}
        state: AvatarState = self._avatar.state()
        return {"available": state.available, "running": state.running, "message": state.message}

    def quit_app(self) -> None:
        """页面上的退出入口（托盘之外的第二出口）。**不阻塞 JS 调用**。"""
        threading.Thread(target=self._quit_app, name="astra-quit", daemon=True).start()


# ── 单实例 ──────────────────────────────────────────────


def acquire_single_instance() -> bool:
    """命名互斥体：已经有实例时返回 ``False``。

    细节（句柄要一直持有）见 :func:`astra.desktop.win32.acquire_named_mutex`。
    """
    first, handle = win32.acquire_named_mutex(MUTEX_NAME)
    if handle:
        _MUTEX_HANDLES.append(handle)
    elif first:
        _logger.warning(
            "单实例互斥体创建失败，按「没有别的实例」继续",
            extra={"event": "desktop.mutex.failed"},
        )
    return first


def focus_existing_window() -> bool:
    """把已有实例的窗口唤到前台；找不到就返回 ``False``。"""
    hwnd = win32.find_window_by_title(WINDOW_TITLE)
    if not hwnd:
        return False
    win32.focus_window(hwnd)
    return True


def find_own_window(pid: int, *, timeout_s: float = 8.0) -> int:
    """找本进程那个 pywebview 窗口（靠"自己 PID + 窗口类前缀"，不依赖标题）。

    标题会被页面改写（``<title>``），所以不拿标题当锚；类名是 pywebview 在 Windows 上
    的 WinForms 窗口类，稳定。

    **带重试是必要的**：``webview.start()`` 的回调会在 GUI 刚起来时就被调用，
    那一刻窗口往往还没 ``Show()``（实测踩过：只看可见窗口 → 一个都没找到 →
    角色区永远起不来）。这里等它最多 ``timeout_s`` 秒；实在没可见的，就把那个
    还没显示的窗体句柄返回（当 ``-parentHWND`` 的宿主一样能用）。
    """
    deadline = time.monotonic() + timeout_s
    fallback = 0
    while time.monotonic() < deadline:
        for hwnd in win32.top_level_windows_of(pid):
            if not win32.window_class(hwnd).startswith(_FORM_CLASS_PREFIX):
                continue
            if win32.is_window_visible(hwnd):
                return hwnd
            fallback = fallback or hwnd
        time.sleep(0.2)
    return fallback


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
        self._avatar = AvatarHost(
            player=settings.avatar_player_path,
            log_file=settings.data_dir / "astra-avatar.log",
        )
        self._window: Any = None
        self._tray: Any = None
        self._quitting = False
        self.api = DesktopApi(self.quit, avatar=self._avatar)

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
        """GUI 起好之后（pywebview 在另一个线程里调它）：接上角色区、装托盘。"""
        hwnd = find_own_window(os.getpid())
        if hwnd:
            self._avatar.attach(hwnd)
        else:  # pragma: no cover - 只在极少数窗口枚举失败时发生
            _logger.warning(
                "没找到自己的窗口句柄——角色区起不来（托盘照常）",
                extra={"event": "desktop.hwnd.missing"},
            )

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
        # 顺序照 ADR 0030 §1：角色 → 后端 → 窗口
        self._avatar.stop(reason="壳退出")
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
        # 顺序照 ADR 0030 §1：角色 → 后端 → 窗口
        self._avatar.stop(reason="用户退出")
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
