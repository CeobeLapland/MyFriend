"""桌面壳用到的 Win32 原语（ADR 0030 / 0031）。

ctypes 直调 user32/kernel32——**不引 pywin32**：壳的 GUI 依赖面已经由
pywebview / pystray / Pillow 定死（ADR 0030 §4），这里只是把几个调用包一层。
同步、无状态；纯计算部分（矩形换算）在 :mod:`astra.desktop.avatar` 里，有单测。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
from pathlib import Path
from typing import Any

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ── 参数与返回值声明（ctypes 不写这些，64 位下句柄会被截断）──
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumChildWindows.restype = wintypes.BOOL
_get_dpi_for_window: Any = getattr(_user32, "GetDpiForWindow", None)
if _get_dpi_for_window is not None:  # pragma: no cover - Win10 1607 起才有
    _get_dpi_for_window.argtypes = [wintypes.HWND]
    _get_dpi_for_window.restype = ctypes.c_uint

_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.EnumProcesses.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_psapi.EnumProcesses.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001

#: SetWindowPos：0 = HWND_TOP（在兄弟窗口里置顶，WebView2 也是子窗口）
HWND_TOP = 0
SWP_NOACTIVATE = 0x0010

#: ShowWindow
SW_HIDE = 0
SW_RESTORE = 9

#: CreateMutexW 的"已经存在"
ERROR_ALREADY_EXISTS = 183


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_title(hwnd: int) -> str:
    length = int(_user32.GetWindowTextLengthW(hwnd)) + 1
    buffer = ctypes.create_unicode_buffer(length)
    _user32.GetWindowTextW(hwnd, buffer, length)
    return buffer.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """屏幕坐标下的 ``(x, y, width, height)``（子窗口也是屏幕坐标）。"""
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


def is_window_visible(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(hwnd))


def pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def dpi_scale(hwnd: int) -> float:
    """窗口所在显示器的缩放比例（``GetDpiForWindow / 96``）。"""
    if _get_dpi_for_window is None:  # pragma: no cover - Win10 1607 之前
        return 1.0
    dpi = int(_get_dpi_for_window(hwnd)) or 96
    return dpi / 96.0


def find_window_by_title(title: str) -> int:
    return int(_user32.FindWindowW(None, title))


def top_level_windows_of(pid: int) -> list[int]:
    """某个进程的顶层窗口。"""
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _param: int) -> bool:
        if pid_of(hwnd) == pid:
            found.append(int(hwnd))
        return True

    _user32.EnumWindows(proc(callback), 0)
    return found


def children_of(hwnd: int) -> list[int]:
    """**直接**子窗口（``EnumChildWindows`` 会给全后代，所以过滤一层 ``GetParent``）。"""
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(child: int, _param: int) -> bool:
        if int(_user32.GetParent(child) or 0) == hwnd:
            found.append(int(child))
        return True

    _user32.EnumChildWindows(hwnd, proc(callback), 0)
    return found


def show_window(hwnd: int, visible: bool) -> None:
    _user32.ShowWindow(hwnd, SW_RESTORE if visible else SW_HIDE)


def focus_window(hwnd: int) -> None:
    _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)


def place_child(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """把子窗口摆到父客户区的 ``(x, y, width, height)``，并保持在兄弟窗口最上层。"""
    return bool(_user32.SetWindowPos(hwnd, HWND_TOP, x, y, width, height, SWP_NOACTIVATE))


def acquire_named_mutex(name: str) -> tuple[bool, int]:
    """命名互斥体：返回 ``(是不是第一个实例, 句柄)``。

    **句柄必须活着**（回收了单实例就失效），所以把它交给调用方持有——
    ``astra.desktop.app`` 有一个模块级列表专门存它。
    拿不到互斥体（极少见的系统错误）时按"第一个实例"处理：单实例是体验优化，
    不该因为一个系统调用失败就把应用挡在门外。
    """
    handle = int(_kernel32.CreateMutexW(None, False, name) or 0)
    if not handle:
        return True, 0
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS, handle


def pids_of_exe(path: Path) -> list[int]:
    """按**可执行文件路径**找出正在跑的进程（不含自己）。

    **不能按镜像名杀**：实测踩过一次大坑——仓库里 `astra desktop` 的启动器就叫
    ``astra.exe``，而 Unity 播放器也叫 ``Astra.exe``；``taskkill /IM Astra.exe``
    会把壳自己（以及别的 ``astra`` 命令）一起干掉。按完整路径比对才是安全的。
    """
    target = str(path.resolve()).casefold()
    buffer = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not _psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        return []

    found: list[int] = []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    for index in range(count):
        pid = int(buffer[index])
        if pid == 0 or pid == os.getpid():
            continue
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if (
                _kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
                and name.value.casefold() == target
            ):
                found.append(pid)
        finally:
            _kernel32.CloseHandle(handle)
    return found


def kill_process(pid: int) -> bool:
    """按 PID 强杀（PID 来自 :func:`pids_of_exe`，路径已核对过）。"""
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)
