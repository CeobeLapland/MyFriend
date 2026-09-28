"""``astra serve`` 的监督进程——"一键重启后端"的地基（ADR 0026 §1）。

**为什么需要多一层进程**（本轮实测）：Windows 上 ``os.execv`` 是"另起一个进程 +
原进程立即 ``_exit(0)``"，于是终端命令提前结束、新进程变成脱管的孤儿；而"另起一个
detached 进程"会留下静静占着端口的幽灵后端。监督者是唯一同时满足"终端还在、
Ctrl+C 还管用、日志还看得到、没有孤儿进程"的做法。

**子进程协议只有一个约定**：退出码 :data:`EXIT_RESTART`（75，EX_TEMPFAIL）表示
"请重启我"；其他任何退出码都表示"收摊"，父进程把它原样透传。

阶段 4 的桌面壳天然就是监督者，届时接管这同一套协议即可。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

_logger = logging.getLogger("astra.supervisor")

#: 子进程请求重启的退出码（BSD 的 EX_TEMPFAIL："稍后再试"）
EXIT_RESTART = 75

#: 父进程给子进程设的标记。**用进程环境变量而不是配置文件**：它只对这一次启动有意义，
#: 而且 ``extra="forbid"`` 只作用于 ``.env``（未知的 ASTRA_ 进程变量被忽略）
SUPERVISED_ENV = "ASTRA_SUPERVISED"

#: 收到 Ctrl+C 之后留给子进程体面退出的时间（uvicorn 要关连接、跑完在途请求）
_SHUTDOWN_GRACE_S = 10.0
#: 宽限期到了还不退，就 terminate / kill
_KILL_GRACE_S = 5.0


def is_supervised() -> bool:
    """当前进程有没有人在管它（决定"重启"端点能不能用）。"""
    return os.environ.get(SUPERVISED_ENV) == "1"


def child_command() -> list[str]:
    """父进程拉起子进程的命令：与用户敲的 ``astra serve`` 等价，只是不再监督。

    用 ``sys.executable -m astra.cli`` 而不是 ``sys.argv[0]``：后者是 ``astra.exe``
    这层 launcher，再套一层没有必要（实测 ``python -m astra.cli`` 可直接用）。
    """
    return [sys.executable, "-m", "astra.cli", "serve", "--no-supervisor"]


def should_restart(exit_code: int) -> bool:
    """**只有显式请求（75）才重启。**

    配置写坏时子进程会以非零码退出，再拉一次只会无限重启并刷屏——如实退出、
    把码透传，用户才能在终端看到 pydantic 的原始报错。
    """
    return exit_code == EXIT_RESTART


def run_supervised(
    command: Sequence[str],
    env: dict[str, str],
    *,
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> int:
    """反复拉起子进程，直到它不以 75 退出；返回最终要透传的退出码。

    ``spawn`` 只为测试注入，真实路径就是 :class:`subprocess.Popen`。
    """
    while True:
        process = spawn(list(command), env=env)  # 固定参数，无 shell
        code = _await_child(process)
        if not should_restart(code):
            return code
        _logger.warning("收到重启请求，正在重启后端", extra={"event": "serve.restarting"})


def _await_child(process: subprocess.Popen[bytes]) -> int:
    """等子进程退出；用户按 Ctrl+C 时先让它体面退出。

    同一控制台里的 Ctrl+C 会**同时**送到父子两个进程，所以这里不转发信号，只是等——
    子进程（uvicorn）自己会走优雅关闭。宽限期过后才 terminate / kill。
    """
    try:
        return process.wait()
    except KeyboardInterrupt:
        _logger.warning("收到 Ctrl+C，等待后端退出", extra={"event": "serve.interrupted"})

    try:
        return process.wait(timeout=_SHUTDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        _logger.warning("后端没在宽限期内退出，强制结束", extra={"event": "serve.killed"})
        process.terminate()
        try:
            return process.wait(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
