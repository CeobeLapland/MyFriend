"""P0 原型：pywebview 空壳 + Unity 播放器用 ``-parentHWND`` 内嵌（ADR 0031 §4）。

**这不是产品代码**——P0 只回答四个问题：

  ① 嵌得进去吗、跟着壳窗口缩放吗  ② 鼠标坐标准不准
  ③ 键盘焦点在 HTML / Unity 之间切得对不对  ④ 系统缩放 125%/150% 下错不错位

①（嵌入 + 跟随矩形）可以 ``--selftest`` 机械核对；②③④ 要人坐在机器前看，
页面顶部会实时显示"键盘焦点落在谁身上"，输入框用来看字符有没有漏进 Unity。

用法：

```powershell
uv run --with pywebview python .trae/prototypes/desktop_embed.py             # 交互模式
uv run --with pywebview python .trae/prototypes/desktop_embed.py --selftest  # 自动核对 ①
```

前置：先按 ADR 0031 §2 的命令构建出 ``Builds/Windows/Astra.exe``。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

PLAYER = Path(r"E:\UnityProject\VirtualTuber\Builds\Windows\Astra.exe")
TITLE = "Astra 原型 — Unity 内嵌验证"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
try:
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = ctypes.c_uint
except AttributeError:  # pragma: no cover - Win10 1607 之前没有它
    user32.GetDpiForWindow = None

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

#: SetWindowPos 的 hWndInsertAfter：0 = HWND_TOP（在兄弟窗口里置顶）
HWND_TOP = 0

GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

#: slot 的 CSS 尺寸 → 物理像素的容差（SetWindowPos 与 Unity 自绘边框差几个像素很正常）
_RECT_TOLERANCE_PX = 12

_user32_setlong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_user32_setlong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32_setlong.restype = ctypes.c_ssize_t
_user32_getlong = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_user32_getlong.argtypes = [wintypes.HWND, ctypes.c_int]
_user32_getlong.restype = ctypes.c_ssize_t
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.SetParent.restype = wintypes.HWND


def player_style(hwnd: int) -> str:
    """播放器窗口的样式位（诊断用：确认 Unity 自己设了 ``WS_CHILD``）。"""
    return hex(int(_user32_getlong(hwnd, GWL_STYLE)))


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def dpi_scale(hwnd: int) -> float:
    if user32.GetDpiForWindow is None:  # pragma: no cover
        return 1.0
    dpi = int(user32.GetDpiForWindow(hwnd)) or 96
    return dpi / 96.0


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def find(title: str | None, class_name: str | None, *, timeout_s: float = 60.0) -> int:
    """按标题 / 类名轮询找窗口（Unity 播放器的类是 ``UnityWndClass``）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(class_name, title)
        if hwnd:
            return int(hwnd)
        time.sleep(0.25)
    return 0


def focus_owner(shell: int, unity: int) -> str:
    """键盘焦点现在落在谁身上——用来在交互模式里目视 ③。"""
    info = GUI_THREAD_INFO()
    info.cbSize = ctypes.sizeof(GUI_THREAD_INFO)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return f"unknown(err={ctypes.get_last_error()})"
    hwnd = int(info.hwndFocus or 0)
    if not hwnd:
        return "none"
    if hwnd == unity:
        return "unity"
    # 往父窗口方向走几层，看它属于哪棵树
    walker = hwnd
    for _ in range(8):
        walker = int(user32.GetParent(walker) or 0)
        if walker == 0:
            break
        if walker == unity:
            return "unity"
        if walker == shell:
            return "webview" if "Chrome_WidgetWin" in window_class(hwnd) else "shell"
    return f"other({window_class(hwnd)})"


class GUI_THREAD_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUI_THREAD_INFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.mouse_event.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def children(hwnd: int) -> list[dict[str, object]]:
    """壳窗口的直接子窗口——诊断用（看清 WebView2 与 Unity 各自挂在谁下面）。"""
    found: list[dict[str, object]] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(child: int, _param: int) -> bool:
        if int(user32.GetParent(child) or 0) == hwnd:
            found.append(
                {
                    "hwnd": int(child),
                    "class": window_class(child),
                    "rect": list(window_rect(child)),
                    "visible": bool(user32.IsWindowVisible(child)),
                }
            )
        return True

    user32.EnumChildWindows(hwnd, enum_proc(callback), 0)
    return found


def find_unity_child(shell: int, *, timeout_s: float = 120.0) -> int:
    """在壳的**子窗口**里找 Unity 播放器窗口（P0 实测：``-parentHWND`` 生效）。

    不要去 ``FindWindowW("UnityWndClass")``：那只找顶层窗口，而播放器的窗口在给了
    ``-parentHWND`` 之后**已经是子窗口**了——实测它会先匹配到 Unity 编辑器残留的
    顶层窗口，把结论带偏（第一轮就踩过）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for child in children(shell):
            if child["class"] == "UnityWndClass":
                return int(child["hwnd"])
        time.sleep(0.25)
    return 0


def sample_colors(hwnd: int, *, grid: int = 8) -> dict[str, object]:
    """在窗口矩形里打网格取屏幕像素——用来佐证"这块区域真的是 Unity 在画"。

    ``GetPixel`` 取的是**屏幕**上已合成的像素，所以能看到 DWM 合成后的结果。
    """
    left, top, width, height = window_rect(hwnd)
    dc = user32.GetDC(0)
    try:
        counts: dict[int, int] = {}
        for iy in range(grid):
            for ix in range(grid):
                x = left + int((ix + 0.5) * width / grid)
                y = top + int((iy + 0.5) * height / grid)
                value = int(gdi32.GetPixel(dc, x, y)) & 0xFFFFFF
                counts[value] = counts.get(value, 0) + 1
    finally:
        user32.ReleaseDC(0, dc)

    top_value, top_count = max(counts.items(), key=lambda item: item[1])
    red, green, blue = top_value & 0xFF, (top_value >> 8) & 0xFF, (top_value >> 16) & 0xFF
    return {
        "distinct": len(counts),
        "top_color": f"#{red:02X}{green:02X}{blue:02X}",
        "top_share": round(top_count / (grid * grid), 2),
    }


def click_screen(x: int, y: int) -> None:
    """在屏幕坐标上真的点一下（跨进程注入，等价于用户点击）。"""
    user32.SetCursorPos(x, y)
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.4)


def focus_check(shell: int, unity: int) -> dict[str, object]:
    """③ 键盘焦点：点 Unity 区域 → 焦点该落到 Unity；点页面 → 焦点该回到 WebView。"""
    left, top, width, height = window_rect(unity)
    click_screen(left + width // 2, top + height // 2)
    after_unity = focus_owner(shell, unity)

    click_screen(left + 60, max(8, top - 70))  # slot 上方的说明文字区域（页面）
    after_page = focus_owner(shell, unity)

    return {"after_click_unity": after_unity, "after_click_page": after_page}


class Bridge:
    """页面 → 壳：把 ``#slot`` 的矩形交给壳，壳用它摆 Unity 子窗口。"""

    def __init__(self) -> None:
        self.shell = 0
        self.unity = 0
        self.last_rect: dict[str, float] | None = None
        self.last_applied: tuple[int, int, int, int] | None = None
        self.player_style: str | None = None

    def set_slot(self, rect: dict) -> bool:
        self.last_rect = rect
        if not self.unity:
            return False
        scale = dpi_scale(self.shell) if self.shell else 1.0
        x = round(float(rect.get("x", 0)) * scale)
        y = round(float(rect.get("y", 0)) * scale)
        width = max(16, round(float(rect.get("width", 1)) * scale))
        height = max(16, round(float(rect.get("height", 1)) * scale))
        if self.last_applied == (x, y, width, height):
            return True
        # 不带 SWP_NOZORDER：Unity 要在兄弟窗口里保持在 WebView2 之上
        ok = bool(user32.SetWindowPos(self.unity, HWND_TOP, x, y, width, height, SWP_NOACTIVATE))
        if ok:
            self.last_applied = (x, y, width, height)
        return ok

    def focus_probe(self) -> str:
        return focus_owner(self.shell, self.unity)

    def quit_app(self) -> None:
        threading.Thread(target=_shutdown, daemon=True).start()


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
  body { margin: 0; background: #14171e; color: #e9ecf1;
         font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", system-ui, sans-serif; }
  .head { padding: 14px 20px 8px; }
  h3 { margin: 0 0 6px; font-size: 15px; }
  p { margin: 4px 0; color: #8a93a3; font-size: 12px; }
  code { color: #5cf5a8; }
  #probe { width: 420px; padding: 6px 10px; background: #1b1f28; color: #e9ecf1;
           border: 1px solid #262c38; border-radius: 8px; font: inherit; }
  #focus { color: #7aa8ff; font-family: ui-monospace, Consolas, monospace; }
  #slot { position: absolute; left: 24px; right: 24px; top: 150px; bottom: 24px;
          border: 1px dashed #5cf5a8; border-radius: 10px; }
</style></head>
<body>
  <div class="head">
    <h3>P0 原型：Unity 内嵌</h3>
    <p id="status">等待 Unity 播放器…</p>
    <p>键盘焦点现在在：<span id="focus">—</span>（点输入框应显示 webview，点绿框里应显示 unity）</p>
    <p><input id="probe" placeholder="在这里打字：字符要出现在这里、且不该漏进 Unity"></p>
    <p>要看四件事：① 拖动窗口边缘缩放 → Unity 跟着变、不变形 ② 鼠标在绿框里点/悬停 → 坐标准确
       ③ 焦点切换（上面那行） ④ 系统缩放 125%/150% → 不错位、不糊</p>
  </div>
  <div id="slot"></div>
  <script>
    let api = null;
    function report() {
      const r = document.getElementById("slot").getBoundingClientRect();
      if (api) api.set_slot({ x: r.x, y: r.y, width: r.width, height: r.height, visible: true });
    }
    function tick() { if (api) api.focus_probe().then((who) => { document.getElementById("focus").textContent = who; }); }
    function ready() {
      api = window.pywebview.api;
      document.getElementById("status").textContent = "Unity 应该出现在绿色虚线框里";
      report();
      window.addEventListener("resize", report);
      setInterval(report, 500);
      setInterval(tick, 700);
    }
    if (window.pywebview && window.pywebview.api) { ready(); }
    else { window.addEventListener("pywebviewready", ready); }
  </script>
</body></html>
"""


def kill_stale_players() -> None:
    """杀掉上一轮遗留的播放器。

    P0 实测：只 terminate 自己拉起的那个不够——上一轮留下的窗口会一直在桌面上
    （而且会被误当成"这次嵌进去的窗口"）。壳的做法见 ADR 0031 §3。
    """
    subprocess.run(["taskkill", "/IM", "Astra.exe", "/F"], capture_output=True, check=False)  # noqa: S603,S607


def launch_unity(bridge: Bridge, shell: int) -> subprocess.Popen[bytes]:
    unity = subprocess.Popen(  # noqa: S603 - 参数固定，无 shell
        [
            str(PLAYER),
            "-parentHWND",
            str(shell),
            "-screen-fullscreen",
            "0",
            "-logFile",
            str(PLAYER.parent / "astra-player.log"),
        ]
    )
    return unity


def _shutdown() -> None:
    import webview  # 延迟导入：与产品一样只在壳路径里用它

    kill_stale_players()
    for window in webview.windows:
        window.destroy()


def _rect_check(bridge: Bridge) -> dict[str, object]:
    """Unity 窗口矩形（换算成壳的客户区坐标）与壳"下发的期望矩形"差多少。"""
    left, top, width, height = window_rect(bridge.unity)
    point = wintypes.POINT(left, top)
    user32.ScreenToClient(bridge.shell, ctypes.byref(point))
    actual = [point.x, point.y, width, height]
    expected = list(bridge.last_applied or [])
    delta: int | None = None
    if len(expected) == 4:
        delta = max(abs(a - b) for a, b in zip(actual, expected))
    return {
        "unity_rect_client": actual,
        "expected": expected,
        "max_delta_px": delta,
        "matches": bool(delta is not None and delta <= _RECT_TOLERANCE_PX),
    }


def _selftest(bridge: Bridge, player: subprocess.Popen[bytes], window: object) -> int:
    """机械化核对 ①：嵌进去了吗、矩形跟着 slot 吗、壳缩放时跟得上吗。"""
    try:
        return _selftest_inner(bridge, player, window)
    finally:
        if player.poll() is None:
            player.terminate()
        _shutdown()


def _selftest_inner(bridge: Bridge, player: subprocess.Popen[bytes], window: object) -> int:
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline and not bridge.unity:
        time.sleep(0.25)

    time.sleep(2.0)  # 等 Unity 第一帧与 SetWindowPos 生效
    result: dict[str, object] = {
        "shell_hwnd": bridge.shell,
        "unity_hwnd": bridge.unity,
        "dpi_scale": dpi_scale(bridge.shell),
        "slot_rect_css": bridge.last_rect,
        "applied_rect": bridge.last_applied,
        "player_style": bridge.player_style,
        "shell_children": children(bridge.shell),
    }
    if not bridge.unity:
        result["error"] = "90 秒内没有等到 Unity 播放器窗口"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    result["unity_parent_is_shell"] = int(user32.GetParent(bridge.unity) or 0) == bridge.shell
    result["rect"] = _rect_check(bridge)

    # 置前才能拿到"真的画在屏幕上"的像素、并让点击落在我们的窗口上
    user32.SetForegroundWindow(bridge.shell)
    time.sleep(0.6)
    front = int(user32.GetForegroundWindow() or 0) == bridge.shell
    result["foreground_is_shell"] = front
    if front:
        result["render_sample"] = sample_colors(bridge.unity)
        result["focus"] = focus_check(bridge.shell, bridge.unity)
    else:
        result["render_sample"] = "skipped：壳不在前台"
        result["focus"] = None

    # 跟随缩放：把壳窗口改大，页面会重报 slot → 壳重摆 Unity
    before = bridge.last_applied
    if window is not None:
        window.resize(1120, 840)  # type: ignore[attr-defined]
        time.sleep(2.5)
        result["resize"] = {
            "applied_before": before,
            "applied_after": bridge.last_applied,
            "changed": before != bridge.last_applied,
        }
        result["rect_after_resize"] = _rect_check(bridge)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    rect = result["rect"]
    after = result.get("rect_after_resize") or {}
    resize = result.get("resize") or {}
    core_ok = (
        bool(result["unity_parent_is_shell"])
        and bool(rect.get("matches"))
        and bool(resize.get("changed"))
        and bool(after.get("matches"))
    )
    focus = result.get("focus") or {}
    focus_ok = (
        not focus
        or (
            focus.get("after_click_unity") == "unity"
            and focus.get("after_click_page") in {"webview", "shell"}
        )
    )
    return 0 if (core_ok and focus_ok) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 原型：Unity 内嵌")
    parser.add_argument("--selftest", action="store_true", help="自动核对嵌入与矩形后退出")
    args = parser.parse_args()

    if not PLAYER.exists():
        print(f"播放器还不存在：{PLAYER}\n先按 ADR 0031 §2 的命令构建。", file=sys.stderr)
        return 1

    import webview

    bridge = Bridge()
    window = webview.create_window(TITLE, html=PAGE, js_api=bridge, width=980, height=760)
    player: dict[str, subprocess.Popen[bytes]] = {}
    outcome: dict[str, int] = {}

    def worker() -> None:
        shell = find(TITLE, None, timeout_s=30.0)
        bridge.shell = shell
        if not shell:
            print("找不到壳窗口（FindWindowW 失败）", file=sys.stderr)
            return
        kill_stale_players()  # 先把上一轮遗留的播放器清掉，免得找错窗口
        handle = launch_unity(bridge, shell)
        player["handle"] = handle
        # -parentHWND 生效：播放器的窗口直接出现在壳的子窗口里（实测，见 ADR 0031）
        unity = find_unity_child(shell, timeout_s=120.0)
        bridge.unity = unity
        if unity:
            bridge.player_style = player_style(unity)
            bridge.last_applied = None
            if bridge.last_rect:
                bridge.set_slot(bridge.last_rect)
        print(f"壳窗口={shell} Unity 窗口={unity} dpi={dpi_scale(shell):.3f}")
        if args.selftest:
            # 非主线程里 raise SystemExit 只会结束这个线程——所以只记结果，
            # 由 _selftest 内部 destroy 窗口来让 webview.start() 返回
            outcome["code"] = _selftest(bridge, handle, window)

    webview.start(worker)

    ended = player.get("handle")
    if ended is not None and ended.poll() is None:
        ended.terminate()
    return outcome.get("code", 0)


if __name__ == "__main__":
    sys.exit(main())