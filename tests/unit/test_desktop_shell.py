"""桌面壳的单元测试（ADR 0030）。

GUI、托盘、进程真机行为由用户验收；这里钉住的是**能自动化、而且真会坏**的几条：
依赖边界（``astra serve`` 不许沾 GUI 包）、URL / 日志路径这些纯函数、窄桥的契约。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from astra.desktop.app import DesktopApi, icon_path, local_host, log_path, studio_url
from astra.desktop.avatar import AvatarHost, compute_placement, placement_scales
from astra.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    params: dict[str, object] = {
        "db_path": tmp_path / "astra.db",
        "persona_path": _REPO_ROOT / "personas" / "astra.toml",
        "log_level": "WARNING",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


# ── 依赖边界（ADR 0030 §4）─────────────────────────────


def test_cli_import_does_not_pull_the_gui_stack() -> None:
    """``astra serve`` / ``astra chat`` 的路径不许沾 pywebview / pystray。

    用子进程验：同进程里别的测试可能已经导入过 GUI 包，``sys.modules`` 就脏了。
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, astra.cli; print('webview' in sys.modules, 'pystray' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "False False"


def test_desktop_subcommand_is_registered() -> None:
    """``astra desktop`` 必须出现在子命令表里（GUI 部分不进测试面）。"""
    completed = subprocess.run(
        [sys.executable, "-m", "astra.cli", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "desktop" in completed.stdout


# ── 纯函数 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),  # noqa: S104 - 只是字符串，不是监听
        ("::", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.1.5", "192.168.1.5"),
    ],
)
def test_local_host_maps_wildcards(raw: str, expected: str) -> None:
    """监听地址写 ``0.0.0.0`` 时，壳自己得连得来——换成回环地址。"""
    assert local_host(raw) == expected


def test_studio_url_points_at_the_local_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path, http_host="0.0.0.0", http_port=8123)  # noqa: S104 - 只是配置值

    assert studio_url(settings) == "http://127.0.0.1:8123/studio"


def test_log_path_defaults_to_the_data_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path, data_dir=tmp_path / "data")

    assert log_path(settings) == tmp_path / "data" / "astra-desktop.log"


def test_log_path_respects_the_configured_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path, log_file=tmp_path / "custom.log")

    assert log_path(settings) == tmp_path / "custom.log"


def test_tray_icon_asset_exists() -> None:
    """托盘图标进仓库（缺了壳也能起，但托盘上会是个占位）。"""
    assert icon_path().is_file()


# ── 窄桥（ADR 0030 §3）──────────────────────────────────


def test_avatar_rect_is_remembered_as_payload() -> None:
    api = DesktopApi(lambda: None)

    assert api.last_avatar_rect is None
    payload = {"x": 24, "y": 150, "width": 300, "height": 400, "visible": True, "enabled": True}
    # 没有 AvatarHost（比如测试环境）时如实返回 False，但载荷照样记下来
    assert api.avatar_rect(payload) is False
    assert api.last_avatar_rect == payload
    assert api.avatar_state() == {
        "available": False,
        "running": False,
        "message": "这个进程不是桌面壳",
    }


def test_quit_app_does_not_block_the_js_caller() -> None:
    """从 JS 线程调进来时不能阻塞——pywebview 是同步等返回的。"""
    done = threading.Event()
    api = DesktopApi(done.set)

    api.quit_app()

    assert done.wait(timeout=2.0) is True


# ── 角色区：几何与状态（ADR 0031）────────────────────────


def test_placement_scales_are_measured_not_assumed() -> None:
    """换算比按"客户区 ÷ 页面视口"实测（实测 WebView2 视口与客户区不成 DPI 关系）。"""
    rect = {"viewport_width": 1600, "viewport_height": 1000}

    assert placement_scales(rect, client_size=(800, 500), fallback_scale=1.5) == (0.5, 0.5)


def test_placement_scales_fall_back_to_dpi_without_a_viewport() -> None:
    assert placement_scales({}, client_size=(800, 600), fallback_scale=1.5) == (1.5, 1.5)


def test_placement_scales_css_pixels() -> None:
    placement = compute_placement(
        {"x": 24, "y": 150, "width": 300, "height": 400, "visible": True},
        scales=(1.5, 1.5),
        client_size=(1600, 1200),
    )

    assert placement == (36, 225, 450, 600)


@pytest.mark.parametrize(
    "rect",
    [
        {"x": 0, "y": 0, "width": 300, "height": 400, "visible": False},
        {"x": 0, "y": 0, "width": 20, "height": 400, "visible": True},  # 太窄
        {"x": 0, "y": 0, "width": 300, "height": 10, "visible": True},  # 太扁
    ],
)
def test_placement_hides_when_it_should_not_show(rect: dict[str, object]) -> None:
    """不可见 / 太小的区域 → 返回 None，调用方据此把窗口藏起来。"""
    assert compute_placement(rect, scales=(1.0, 1.0), client_size=(800, 600)) is None


def test_placement_clamps_into_the_client_area() -> None:
    """clamp 用"挪进来"而不是"缩小"——Unity 的画面比例不该被挤变形。"""
    placement = compute_placement(
        {"x": 900, "y": 700, "width": 300, "height": 400, "visible": True},
        scales=(1.0, 1.0),
        client_size=(1000, 800),
    )

    assert placement == (700, 400, 300, 400)


def test_avatar_state_is_honest_about_a_missing_player(tmp_path: Path) -> None:
    unset = AvatarHost(player=None)
    assert unset.state().available is False
    assert "ASTRA_AVATAR_PLAYER_PATH" in unset.state().message

    missing = AvatarHost(player=tmp_path / "nope.exe")
    assert missing.state().available is False
    assert "不在" in missing.state().message


def test_avatar_apply_does_nothing_until_the_shell_window_is_known(tmp_path: Path) -> None:
    """还没 attach（不知道壳窗口句柄）时，页面的报点不该拉起任何进程。"""
    player = tmp_path / "Astra.exe"
    player.write_bytes(b"")
    host = AvatarHost(player=player)

    assert host.state().available is True
    payload = {"x": 0, "y": 0, "width": 300, "height": 400, "visible": True, "enabled": True}
    assert host.apply(payload) is False
    assert host.state().running is False
