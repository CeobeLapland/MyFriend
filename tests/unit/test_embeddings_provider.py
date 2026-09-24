"""``LMStudioEmbeddingProvider`` 的单元测试（用 ``httpx.MockTransport``）。

重点：请求体形状、维度校验、非有限值拦截、异常翻译。维度校验是安全关键——
``memory_vectors`` 的 vec0 表把 768 写死在 DDL 里，硬塞错维度会污染整个索引。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from astra.domain.errors import LLMResponseError, LLMTimeoutError, LLMUnavailableError
from astra.providers.lmstudio.embeddings import LMStudioEmbeddingProvider

_DIM = 4


def _provider(handler: Any, **overrides: Any) -> LMStudioEmbeddingProvider:
    params: dict[str, Any] = {
        "base_url": "http://localhost:1234/v1",
        "model": "text-embedding-nomic-embed-text-v1.5",
        "dim": _DIM,
        "api_key": "test-key",
        "connect_timeout_s": 5.0,
        "request_timeout_s": 30.0,
    }
    params.update(overrides)
    return LMStudioEmbeddingProvider(transport=httpx.MockTransport(handler), **params)


def _embedding_response(*vectors: list[float], model: str = "text-embedding-nomic-embed-text-v1.5"):
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": model,
            "data": [
                {"object": "embedding", "index": i, "embedding": vector}
                for i, vector in enumerate(vectors)
            ],
            # 实测这个端点恒返回 0，不该被依赖
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        },
    )


def _raw_response(body: str) -> httpx.Response:
    """绕过 ``json=`` 的序列化——``json.dumps`` 拒绝 NaN/Infinity，
    但 ``json.loads`` 默认接受它们。要测"provider 发了非法字面量"就得手写 body。"""
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


# ── 正常路径 ────────────────────────────────────────────


def test_embed_returns_vectors_in_input_order() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return _embedding_response([1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0])

    result = asyncio.run(_provider(handler).embed(["第一条", "第二条"]))

    assert result == ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0))
    assert seen["url"].endswith("/v1/embeddings")
    assert '"input"' in seen["body"]
    assert "第一条" in seen["body"]


def test_empty_input_short_circuits_without_a_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        msg = "provider must not be called for empty input"
        raise AssertionError(msg)

    assert asyncio.run(_provider(handler).embed([])) == ()


def test_model_and_dim_are_exposed() -> None:
    provider = _provider(lambda _r: _embedding_response([1.0, 0.0, 0.0, 0.0]))

    assert provider.model == "text-embedding-nomic-embed-text-v1.5"
    assert provider.dim == _DIM


def test_integer_components_are_accepted() -> None:
    """JSON 里 `1` 与 `1.0` 都是合法数值，不该被当成非有限值。"""
    result = asyncio.run(_provider(lambda _r: _embedding_response([1, 0, 0, 0])).embed(["x"]))

    assert result == ((1.0, 0.0, 0.0, 0.0),)


# ── 响应形状校验 ────────────────────────────────────────


def test_mismatched_vector_count_is_rejected() -> None:
    """数量对不上就无法把向量与文本对应——静默补齐会写错记忆。"""
    provider = _provider(lambda _r: _embedding_response([1.0, 0.0, 0.0, 0.0]))

    with pytest.raises(LLMResponseError, match="mismatched number"):
        asyncio.run(provider.embed(["a", "b"]))


def test_wrong_dimension_is_rejected() -> None:
    provider = _provider(lambda _r: _embedding_response([1.0, 0.0]))

    with pytest.raises(LLMResponseError, match="dimension"):
        asyncio.run(provider.embed(["a"]))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_components_are_rejected(literal: str) -> None:
    """非有限值会污染余弦距离排序，必须在入口拦掉。

    ``json.loads`` 默认接受这三个字面量，所以 provider 真可能发出来。
    """
    body = f'{{"data":[{{"embedding":[1.0, 0.0, 0.0, {literal}]}}]}}'
    provider = _provider(lambda _r: _raw_response(body))

    with pytest.raises(LLMResponseError, match="non-finite"):
        asyncio.run(provider.embed(["a"]))


def test_non_numeric_component_is_rejected() -> None:
    provider = _provider(lambda _r: _embedding_response([1.0, 0.0, 0.0, "x"]))  # type: ignore[list-item]

    with pytest.raises(LLMResponseError, match="non-finite"):
        asyncio.run(provider.embed(["a"]))


@pytest.mark.parametrize("payload", ["not-a-dict", [], {"data": "nope"}, {"data": [1, 2]}])
def test_malformed_payloads_are_rejected(payload: Any) -> None:
    provider = _provider(lambda _r: httpx.Response(200, json=payload))

    with pytest.raises(LLMResponseError):
        asyncio.run(provider.embed(["a"]))


# ── 异常翻译 ────────────────────────────────────────────


def test_connection_error_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailableError):
        asyncio.run(_provider(handler).embed(["a"]))


def test_read_timeout_becomes_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(LLMTimeoutError):
        asyncio.run(_provider(handler).embed(["a"]))


def test_http_error_status_becomes_unavailable() -> None:
    provider = _provider(lambda _r: httpx.Response(500, text="boom"))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(provider.embed(["a"]))


def test_total_budget_timeout_is_enforced() -> None:
    """httpx 的 read 超时是每阶段默认值，不是总预算——外层 wait_for 必须兜住。"""

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _embedding_response([1.0, 0.0, 0.0, 0.0])

    provider = _provider(slow, request_timeout_s=0.05)

    with pytest.raises(LLMTimeoutError):
        asyncio.run(provider.embed(["a"]))
