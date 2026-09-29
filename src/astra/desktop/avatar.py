"""角色区：把 Unity 播放器嵌进壳窗口（ADR 0031）。

形态（P0 原型实测过，见 ADR 0031 的「P0 原型结论」）：

- 壳把**自己的窗口句柄**交给播放器：``Astra.exe -parentHWND <hwnd> -screen-fullscreen 0``；
  Unity 自己就把窗口建成壳的**子窗口**（样式 ``WS_CHILD | WS_VISIBLE``）——
  **不需要手工 ``SetParent``**；
- 壳只负责摆放：页面报上来的 ``#avatar-slot`` 矩形（CSS px）× DPI 比例、clamp 进客户区，
  然后 ``SetWindowPos``（客户区坐标 + 置顶，否则会被 WebView2 那个子窗口盖住）。

三条实测教训：
1. 找播放器窗口要在**壳的子窗口**里按类名找——``FindWindowW`` 只搜顶层窗口，
   实测会先匹配到 Unity 编辑器残留的顶层窗口；
2. 启动前与退出时都要按镜像名清一遍遗留播放器（``terminate()`` 不保证杀死）；
3. 拿到 ``#avatar-slot`` 之前先把播放器藏起来，否则它会用默认尺寸在窗口里闪一下。

**两个不同的概念**（页面侧分得很清楚，见 ADR 0030 §3 的 P2 修订）：

- ``enabled=False``（用户关掉「显示角色」）→ **结束进程**（这是显存开关，ADR 0031 §3）；
- ``visible=False``（切到"状态"卡片 / 窗口太窄）→ 只是**藏起来**，进程留着。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from astra.desktop import win32

_logger = logging.getLogger("astra.desktop.avatar")

#: 播放器窗口的类名（Unity 的 Windows 播放器与编辑器共用这个类）
PLAYER_WINDOW_CLASS = "UnityWndClass"
#: 等播放器窗口出现的预算（首次启动含着色器预热，实测十几秒）
_STARTUP_TIMEOUT_S = 120.0
#: 播放器退出时的宽限
_STOP_GRACE_S = 8.0
#: 小于这个物理像素就当"这块区域不该显示"（视觉上也确实没法看）
_MIN_SIDE_PX = 40


def _as_float(value: object, default: float = 0.0) -> float:
    """页面来的数字（JSON 里就是 int/float）；不是数字就退回默认值，不抛异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def placement_scales(
    rect: Mapping[str, object],
    *,
    client_size: tuple[int, int],
    fallback_scale: float,
) -> tuple[float, float]:
    """页面的 CSS px → 壳客户区物理像素的换算比（x、y 分开算）。

    **按测量算、不按 DPI 假设**：实测（150% 缩放下）WebView2 的视口尺寸与客户区的
    物理尺寸**不成 DPI 比例**——pywebview 把 WebView2 控件的尺寸按 DIP 数值当物理像素
    用掉了，于是页面以为视口宽 ~1689 CSS px，而客户区只有 1165 物理像素。
    硬套 ``×1.5`` 会把角色区摆到窗口外面去（实测踩过）。

    所以用"客户区物理尺寸 ÷ 页面自报视口尺寸"这个比值；页面没报视口时
    （老页面 / 单测）退回 DPI 比例。
    """
    viewport_width = _as_float(rect.get("viewport_width"))
    viewport_height = _as_float(rect.get("viewport_height"))
    client_width, client_height = client_size
    scale_x = client_width / viewport_width if viewport_width > 0 else fallback_scale
    scale_y = client_height / viewport_height if viewport_height > 0 else fallback_scale
    return scale_x, scale_y


def compute_placement(
    rect: Mapping[str, object],
    *,
    scales: tuple[float, float],
    client_size: tuple[int, int],
    min_side: int = _MIN_SIDE_PX,
) -> tuple[int, int, int, int] | None:
    """把页面上报的 CSS 矩形换算成"壳客户区里的物理像素矩形"。

    返回 ``None`` 表示**不该显示**（不可见 / 太小）——调用方据此把窗口藏起来。
    纯函数，有单测（ADR 0031 的几何部分）。clamp 用"挪进客户区"而不是"缩小"，
    这样 Unity 的画面比例不会被挤变形。
    """
    if not rect.get("visible", False):
        return None

    scale_x, scale_y = scales
    width = round(_as_float(rect.get("width")) * scale_x)
    height = round(_as_float(rect.get("height")) * scale_y)
    if width < min_side or height < min_side:
        return None

    client_width, client_height = client_size
    x = round(_as_float(rect.get("x")) * scale_x)
    y = round(_as_float(rect.get("y")) * scale_y)
    x = max(0, min(x, max(0, client_width - width)))
    y = max(0, min(y, max(0, client_height - height)))
    return x, y, width, height


@dataclass(frozen=True, slots=True)
class AvatarState:
    """给 Studio 的只读状态（``DesktopApi.avatar_state``）。"""

    available: bool
    running: bool
    message: str


class AvatarHost:
    """Unity 播放器的生命周期与摆放。**只在桌面壳里用**。"""

    def __init__(self, player: Path | None, log_file: Path | None = None) -> None:
        self._player = player
        self._log_file = log_file
        self._process: subprocess.Popen[bytes] | None = None
        self._shell = 0
        self._hwnd = 0
        self._rect: Mapping[str, object] | None = None
        self._message = ""
        #: 上次启动失败后**不再自动重启**（否则页面每秒一次的报点会变成重启风暴）；
        #: 用户把「显示角色」关掉再打开 = 显式重试。
        self._blocked = False
        self._lock = threading.Lock()

    # ── 状态 ────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """配了播放器、而且文件真的在。"""
        return self._player is not None and self._player.is_file()

    def state(self) -> AvatarState:
        with self._lock:
            running = self._hwnd != 0 and self._process is not None and self._process.poll() is None
            if not self.available:
                if self._player is None:
                    message = (
                        "没有配播放器——在设置页填 ASTRA_AVATAR_PLAYER_PATH（构建命令见 README）"
                    )
                else:
                    message = f"播放器不在：{self._player}"
            elif running:
                message = "运行中"
            elif self._process is not None and self._process.poll() is None:
                message = "启动中…"
            else:
                message = self._message or "角色已关闭"
            return AvatarState(available=self.available, running=running, message=message)

    def attach(self, shell_hwnd: int) -> None:
        """壳窗口的句柄——播放器的 ``-parentHWND`` 就是它。GUI 起来之后调。"""
        with self._lock:
            self._shell = shell_hwnd
        _logger.info(
            "角色区的宿主窗口：%s",
            shell_hwnd,
            extra={"event": "avatar.attach", "detail": {"hwnd": shell_hwnd}},
        )

    # ── 页面驱动 ────────────────────────────────────────

    def apply(self, rect: Mapping[str, object]) -> bool:
        """页面报来一份矩形/开关状态 → 该起就起、该藏就藏、该摆就摆。"""
        if self._rect != dict(rect):
            client = win32.client_size(self._shell) if self._shell else (0, 0)
            _logger.debug(
                "角色区报点：%s（视口 %sx%s，客户区 %s，比值 %.3f×%.3f）",
                {key: rect.get(key) for key in ("x", "y", "width", "height", "visible", "enabled")},
                rect.get("viewport_width"),
                rect.get("viewport_height"),
                client,
                *placement_scales(rect, client_size=client, fallback_scale=self._scale()),
                extra={"event": "avatar.rect"},
            )
        self._rect = dict(rect)
        if not bool(rect.get("enabled", True)):
            # 关一次再开 = 显式重试（也清掉上次失败留下的"不再自动重启"）
            self._blocked = False
            self.stop(reason="页面关掉了「显示角色」")
            return True
        if self._blocked or not self.available or self._shell == 0:
            return False
        if self._process is None or self._process.poll() is not None:
            self.start()
            return True
        self._place()
        return True

    # ── 生命周期 ────────────────────────────────────────

    def start(self) -> None:
        """起播放器（清遗留 → 拉起 → 后台线程等它的窗口出现）。"""
        if self._shell == 0 or not self.available:
            return
        if self._process is not None and self._process.poll() is None:
            return

        self._kill_stale()  # 清遗留：P0 实测踩过幽灵窗口
        args = [
            str(self._player),
            "-parentHWND",
            str(self._shell),
            "-screen-fullscreen",
            "0",
        ]
        if self._log_file is not None:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            args += ["-logFile", str(self._log_file)]

        with self._lock:
            self._message = ""
        self._process = subprocess.Popen(args)  # noqa: S603 - 参数固定，无 shell
        _logger.info(
            "拉起角色播放器", extra={"event": "avatar.start", "detail": {"pid": self._process.pid}}
        )
        threading.Thread(target=self._await_window, name="astra-avatar", daemon=True).start()

    def stop(self, *, reason: str = "") -> None:
        """停播放器：terminate → 宽限 → kill → 再按路径清一遍兜底。"""
        process = self._process
        self._process = None
        self._hwnd = 0
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            _logger.info(
                "角色播放器已停止",
                extra={"event": "avatar.stop", "detail": {"reason": reason}},
            )
        self._kill_stale()  # 兜底：terminate 不保证杀死（P0 实测）

    def _kill_stale(self) -> int:
        """清掉**同一个可执行文件**的遗留进程。

        **按路径找、不按镜像名**：`Astra.exe` 这个名字壳自己的启动器也在用，
        按镜像名杀会把壳（和别的 ``astra`` 命令）一起干掉——实测踩过这个大坑，
        详见 :func:`astra.desktop.win32.pids_of_exe`。
        """
        if self._player is None:
            return 0
        killed = 0
        for pid in win32.pids_of_exe(self._player):
            if win32.kill_process(pid):
                killed += 1
        if killed:
            _logger.info(
                "清掉遗留的播放器进程",
                extra={"event": "avatar.stale", "detail": {"count": killed}},
            )
        return killed

    # ── 内部 ────────────────────────────────────────────

    def _await_window(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                self._fail("播放器进程退出了（看 data/astra-avatar.log）")
                return
            hwnd = _find_player_child(self._shell)
            if hwnd:
                with self._lock:
                    self._hwnd = hwnd
                win32.show_window(hwnd, False)  # 先藏：等页面报矩形，别用默认尺寸闪一下
                self._place()
                return
            time.sleep(0.25)
        self._fail(f"{_STARTUP_TIMEOUT_S:.0f} 秒内没等到播放器窗口")

    def _place(self) -> None:
        hwnd = self._hwnd
        if hwnd == 0 or self._rect is None:
            return
        client = win32.client_size(self._shell)
        placement = compute_placement(
            self._rect,
            scales=placement_scales(self._rect, client_size=client, fallback_scale=self._scale()),
            client_size=client,
        )
        if placement is None:
            win32.show_window(hwnd, False)
            return
        x, y, width, height = placement
        win32.place_child(hwnd, x, y, width, height)
        win32.show_window(hwnd, True)

    def _scale(self) -> float:
        """壳窗口的 DPI 比例（页面的 CSS px × 它 = 客户区物理像素）。"""
        return win32.dpi_scale(self._shell) if self._shell else 1.0

    def _fail(self, message: str) -> None:
        with self._lock:
            self._message = message
            self._blocked = True
            self._hwnd = 0
        _logger.warning(message, extra={"event": "avatar.failed"})


def _find_player_child(shell_hwnd: int) -> int:
    for child in win32.children_of(shell_hwnd):
        if win32.window_class(child) == PLAYER_WINDOW_CLASS:
            return child
    return 0
