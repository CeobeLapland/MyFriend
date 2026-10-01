"""httpx 异常 → 领域异常的映射（第三方 OpenAI 兼容后端）。

规则与 ``providers/lmstudio/errors.py`` 完全一致，差别是**错误类型由调用方给**：
LLM 需要区分"连不上"（不可重试）与"超时"（可重试一次），而 STT / TTS 各自只有
一个错误类型（`docs/07` §4：语音失败都不重试）。写两份几乎相同的函数不如把
类型参数化——那里的注释解释了 ``ConnectTimeout`` 为什么必须先判断。
"""

from __future__ import annotations

import httpx

from astra.domain.errors import AstraError


def translate(
    exc: httpx.HTTPError,
    *,
    unavailable: type[AstraError],
    backend: str,
    timeout: type[AstraError] | None = None,
) -> AstraError:
    """把 httpx 异常翻译成领域异常。

    Args:
        exc: 原始 httpx 异常。
        unavailable: "不可达 / 非 2xx" 对应的领域异常类型。
        backend: 出现在 message 里的后端名（``llm provider`` / ``stt backend`` …）。
        timeout: 超时对应的类型；为 ``None`` 时与 ``unavailable`` 同型。

    ``httpx.ConnectTimeout`` 必须先于 ``TimeoutException`` 判断：它同时继承两者，
    但"连不上"和"服务太慢"是两种处境——前者重试没有意义。
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return unavailable(f"{backend} is unreachable", detail=str(exc))

    if isinstance(exc, httpx.TimeoutException):
        return (timeout or unavailable)(f"{backend} timed out", detail=str(exc))

    if isinstance(exc, httpx.HTTPStatusError):
        return unavailable(f"{backend} returned HTTP {exc.response.status_code}", detail=str(exc))

    return unavailable(f"{backend} is unreachable", detail=str(exc))
