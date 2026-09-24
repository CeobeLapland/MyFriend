"""httpx 异常 → 领域异常的映射。

Orchestrator 只认识 :mod:`astra.domain.errors`，不认识 httpx——这样换 HTTP 客户端
时不需要改上层（ADR 0010）。
"""

from __future__ import annotations

import httpx

from astra.domain.errors import AstraError, LLMTimeoutError, LLMUnavailableError


def translate(exc: httpx.HTTPError) -> AstraError:
    """把 httpx 异常翻译成领域异常。

    映射规则（对应 ADR 0011 的重试矩阵）：

    ==================================  ==========================  ==========
    httpx 异常                          领域异常                    可重试
    ==================================  ==========================  ==========
    ``ConnectError`` / ``ConnectTimeout`` ``LLMUnavailableError``     否
    其它 ``TimeoutException``           ``LLMTimeoutError``         **是**
    其它 ``HTTPError``                  ``LLMUnavailableError``     否
    ==================================  ==========================  ==========

    ``ConnectTimeout`` 必须先于 ``TimeoutException`` 判断：它同时继承两者，
    但"连不上"和"模型太慢"是两种处境——前者重试没有意义。

    非 2xx 也归入「不可用」：provider 拒绝服务与连不上，对上层是同一种处境。
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return LLMUnavailableError("llm provider is unreachable", detail=str(exc))

    if isinstance(exc, httpx.TimeoutException):
        return LLMTimeoutError("llm provider timed out", detail=str(exc))

    if isinstance(exc, httpx.HTTPStatusError):
        return LLMUnavailableError(
            f"llm provider returned HTTP {exc.response.status_code}",
            detail=str(exc),
        )

    return LLMUnavailableError("llm provider is unreachable", detail=str(exc))
