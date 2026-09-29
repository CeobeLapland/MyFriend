"""``astra.supervisor`` 的单元测试。

监督进程的真机行为（Ctrl+C、重启）要人来做，但**决策逻辑**必须在这里钉死：
"只有退出码 75 才重启"——写错这一条，配置写坏时后端会无限重启刷屏。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any

import pytest

from astra.supervisor import (
    EXIT_RESTART,
    SUPERVISED_ENV,
    child_command,
    is_supervised,
    run_supervised,
    should_restart,
)


class _FakeProcess:
    """按剧本依次返回 ``wait()`` 的结果（可以是异常，用来模拟被打断/超时）。"""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        assert self._outcomes, "wait() 被调用的次数超过了剧本"
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, int)
        return outcome

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def spawner() -> Any:
    """返回 ``(spawn, calls)``：``calls`` 按顺序记下每次拉起的进程与参数。"""
    processes: list[_FakeProcess] = []
    calls: list[dict[str, Any]] = []

    def spawn(command: list[str], *, env: dict[str, str], **kwargs: Any) -> _FakeProcess:
        process = processes.pop(0)
        calls.append({"command": command, "env": env, "process": process, "kwargs": kwargs})
        return process

    def prepare(*items: _FakeProcess) -> Any:
        processes.extend(items)
        return spawn

    return prepare, calls


# ── 决策：只有 75 才重启 ────────────────────────────────


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (EXIT_RESTART, True),
        (0, False),
        (1, False),
        (130, False),
    ],
)
def test_should_restart_only_for_the_restart_code(exit_code: int, expected: bool) -> None:
    assert should_restart(exit_code) is expected


def test_restart_code_is_ex_tempfail() -> None:
    """75 是 BSD 的 EX_TEMPFAIL——不要改成 0 或 1 这种会被误判的码。"""
    assert EXIT_RESTART == 75


def test_child_command_disables_supervision() -> None:
    """子进程必须带 ``--no-supervisor``，否则会无限套娃。"""
    command = child_command()

    assert command == [sys.executable, "-m", "astra.cli", "serve", "--no-supervisor"]
    assert "--no-supervisor" in command


def test_is_supervised_reads_the_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISED_ENV, raising=False)
    assert is_supervised() is False

    monkeypatch.setenv(SUPERVISED_ENV, "1")
    assert is_supervised() is True


# ── 监督循环 ────────────────────────────────────────────


def test_restarts_until_child_stops_asking(spawner: Any) -> None:
    prepare, calls = spawner
    spawn = prepare(_FakeProcess(EXIT_RESTART), _FakeProcess(EXIT_RESTART), _FakeProcess(0))

    assert run_supervised(["astra"], {"A": "1"}, spawn=spawn) == 0
    assert len(calls) == 3


def test_propagates_other_exit_codes_without_restarting(spawner: Any) -> None:
    """配置写坏时子进程以 1 退出——不能重启，否则会无限刷屏。"""
    prepare, calls = spawner
    spawn = prepare(_FakeProcess(1))

    assert run_supervised(["astra"], {"A": "1"}, spawn=spawn) == 1
    assert len(calls) == 1


def test_passes_env_and_command_through(spawner: Any) -> None:
    prepare, calls = spawner
    spawn = prepare(_FakeProcess(0))
    env = {"ASTRA_SUPERVISED": "1"}

    run_supervised(["python", "-m", "astra.cli"], env, spawn=spawn)

    assert calls[0]["command"] == ["python", "-m", "astra.cli"]
    assert calls[0]["env"] == env


def test_ctrl_c_waits_for_the_child_instead_of_killing_it(spawner: Any) -> None:
    """Ctrl+C 会同时送到父子两个进程——父进程只等，不抢着杀。"""
    prepare, calls = spawner
    process = _FakeProcess(KeyboardInterrupt(), 0)
    spawn = prepare(process)

    assert run_supervised(["astra"], {}, spawn=spawn) == 0
    assert process.terminated is False
    assert process.killed is False
    assert len(calls) == 1


def test_uses_force_only_after_the_child_ignores_the_grace_period(spawner: Any) -> None:
    prepare, _ = spawner
    process = _FakeProcess(
        KeyboardInterrupt(),
        subprocess.TimeoutExpired("astra", 10.0),
        subprocess.TimeoutExpired("astra", 5.0),
        9,
    )
    spawn = prepare(process)

    assert run_supervised(["astra"], {}, spawn=spawn) == 9
    assert process.terminated is True
    assert process.killed is True


# ── 停机事件与输出重定向（ADR 0030：桌面壳接管监督者）───


def test_popen_kwargs_are_forwarded_to_the_child(spawner: Any) -> None:
    """桌面壳靠它把子进程输出重定向到日志文件（GUI 场景没有控制台）。"""
    prepare, calls = spawner
    spawn = prepare(_FakeProcess(0))

    run_supervised(["astra"], {}, spawn=spawn, popen_kwargs={"stdout": "log", "stderr": "log"})

    assert calls[0]["kwargs"] == {"stdout": "log", "stderr": "log"}


def test_stop_set_before_the_child_exits_prevents_a_restart(spawner: Any) -> None:
    """停机请求置位后，**哪怕子进程带着 75 退出也不再拉起**（ADR 0030 §1）。"""
    prepare, calls = spawner
    stop = threading.Event()
    stop.set()
    spawn = prepare(_FakeProcess(EXIT_RESTART))

    assert run_supervised(["astra"], {}, stop=stop, spawn=spawn) == EXIT_RESTART
    assert len(calls) == 1


def test_stop_during_the_wait_terminates_the_child(spawner: Any) -> None:
    """停机请求在等待期间到达 → terminate → 返回它的退出码，不再重启。"""
    prepare, calls = spawner
    stop = threading.Event()

    class _SlowProcess(_FakeProcess):
        """第一次等待期间"用户点了退出"，同时按剧本抛超时。"""

        def wait(self, timeout: float | None = None) -> int:
            stop.set()
            return super().wait(timeout=timeout)

    process = _SlowProcess(subprocess.TimeoutExpired("astra", 0.5), 17)
    spawn = prepare(process)

    assert run_supervised(["astra"], {}, stop=stop, spawn=spawn) == 17
    assert process.terminated is True
    assert process.killed is False
    assert len(calls) == 1


def test_without_a_stop_event_the_wait_path_is_unchanged(spawner: Any) -> None:
    """``stop=None``（``astra serve`` 的路径）不进入轮询循环——行为与从前一致。"""
    prepare, _ = spawner
    process = _FakeProcess(0)
    spawn = prepare(process)

    assert run_supervised(["astra"], {}, spawn=spawn) == 0
    assert process.terminated is False
