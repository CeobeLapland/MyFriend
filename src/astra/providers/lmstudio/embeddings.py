"""LM Studio 的 ``EmbeddingProvider`` 实现（``/v1/embeddings``）。

与 ``client.py`` 同属 **唯一允许 import httpx 的层**（ADR 0010）。

实测背景（ADR 0018）：
- ``text-embedding-nomic-embed-text-v1.5`` → **768 维**，标准 OpenAI 格式
  ``{data: [{embedding: [...]}], model, usage}``。
- 该端点的 ``usage.prompt_tokens`` / ``total_tokens`` **恒为 0**，不能用来做观测。
- 请求体用 ``input`` 传**字符串数组**即可批量；顺序与响应 ``data`` 一一对应。

**失败一律降级**：这里的异常由 ``MemoryService`` 捕获并转成"不检索、不写入"，
绝不让记忆子系统把 turn 打挂。因此**不做重试**——embedding 是本地调用，
重试的收益远小于把失败暴露出来的价值。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

import httpx

from astra.domain.errors import LLMResponseError
from astra.providers.lmstudio.errors import translate


class LMStudioEmbeddingProvider:
    """通过 ``/v1/embeddings`` 调用 LM Studio 的向量化接口。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str,
        connect_timeout_s: float,
        request_timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._api_key = api_key
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        #: 测试用：注入 ``httpx.MockTransport`` 以避免真实网络
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()

        payload = {"model": self._model, "input": list(texts)}

        try:
            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.post("/embeddings", json=payload),
                    timeout=self._request_timeout_s,
                )
                response.raise_for_status()
                data = response.json()
        except TimeoutError as exc:
            # asyncio.wait_for 的总预算超时（httpx 自己的超时走下面的 HTTPError 分支）
            from astra.domain.errors import LLMTimeoutError

            raise LLMTimeoutError(
                "embedding request exceeded total budget", detail=str(exc)
            ) from exc
        except httpx.HTTPError as exc:
            raise translate(exc) from exc

        return self._parse(data, expected=len(texts))

    # ── 内部 ────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(
                connect=self._connect_timeout_s,
                read=self._request_timeout_s,
                write=self._request_timeout_s,
                pool=self._connect_timeout_s,
            ),
            transport=self._transport,
        )

    def _parse(self, data: object, *, expected: int) -> tuple[tuple[float, ...], ...]:
        if not isinstance(data, dict):
            msg = "embedding provider returned a non-object response"
            raise LLMResponseError(msg, detail=type(data).__name__)

        items = data.get("data")
        if not isinstance(items, list):
            msg = "embedding provider returned no data array"
            raise LLMResponseError(msg, detail=str(data)[:300])

        if len(items) != expected:
            # 数量对不上就无法把向量与文本对应起来——静默补齐会写错记忆
            msg = "embedding provider returned a mismatched number of vectors"
            raise LLMResponseError(msg, detail=f"expected {expected}, got {len(items)}")

        vectors: list[tuple[float, ...]] = []
        for index, item in enumerate(items):
            vectors.append(self._parse_item(item, index=index))

        return tuple(vectors)

    def _parse_item(self, item: object, *, index: int) -> tuple[float, ...]:
        if not isinstance(item, dict):
            msg = "embedding item is not an object"
            raise LLMResponseError(msg, detail=f"index={index} type={type(item).__name__}")

        raw = item.get("embedding")
        if not isinstance(raw, list):
            msg = "embedding item has no embedding array"
            raise LLMResponseError(msg, detail=f"index={index}")

        if len(raw) != self._dim:
            # 维度不符必须失败：vec0 表把 768 写死在 DDL 里，硬塞会污染整个索引
            msg = "embedding dimension does not match the configured dim"
            raise LLMResponseError(msg, detail=f"index={index} expected={self._dim} got={len(raw)}")

        vector: list[float] = []
        for value in raw:
            number = float(value) if isinstance(value, int | float) else math.nan
            if not math.isfinite(number):
                msg = "embedding contains a non-finite value"
                raise LLMResponseError(msg, detail=f"index={index}")
            vector.append(number)

        return tuple(vector)
