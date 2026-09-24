"""测试用的假实现与构造器。

放在包内（``tests/`` 是包）以便各测试文件共享。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from astra.domain.audio import AudioArtifact, AudioFormat, Transcript, TTSRequest
from astra.domain.errors import LLMUnavailableError
from astra.domain.llm import LLMRequest, LLMResponse, LLMUsage

VALID_ASSISTANT_JSON = json.dumps(
    {
        "reply": "你好呀，今天过得怎么样？",
        "emotion": {"primary": "happy", "intensity": 0.6},
        "action": {"name": "nod", "intensity": 0.4},
        "voice": {"style": "warm", "speed": 1.0, "pitch": 0.0},
    },
    ensure_ascii=False,
)


def llm_response(
    text: str = VALID_ASSISTANT_JSON,
    *,
    model: str = "fake-model",
    finish_reason: str = "stop",
    reasoning_present: bool = False,
) -> LLMResponse:
    return LLMResponse(
        text=text,
        model=model,
        finish_reason=finish_reason,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=20, reasoning_tokens=0),
        latency_ms=1.0,
        reasoning_present=reasoning_present,
        response_id="fake-1",
    )


def assistant_json(**overrides: Any) -> str:
    """构造一段合法但可定制的助手 JSON 文本。"""
    payload = json.loads(VALID_ASSISTANT_JSON)
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class FakeLLMProvider:
    """脚本化的 :class:`astra.domain.ports.LLMProvider`。

    ``script`` 里每一项可以是 :class:`LLMResponse`（正常返回）或
    :class:`Exception`（抛出）。脚本用完后继续调用会抛
    :class:`LLMUnavailableError`，避免测试静默通过。
    """

    def __init__(
        self,
        script: list[LLMResponse | Exception] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        self.requests: list[LLMRequest] = []
        self._script: list[LLMResponse | Exception] = list(script or [])
        self._delay_s = delay_s

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)

        if self._delay_s:
            await asyncio.sleep(self._delay_s)

        if not self._script:
            raise LLMUnavailableError("FakeLLMProvider script is exhausted")

        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)


class FakeEmbeddingProvider:
    """确定性的 :class:`astra.domain.ports.EmbeddingProvider`。

    两条使用路径：

    1. **显式给定向量**（``vectors={"文本": (1.0, 0.0, ...)}``）——去重/检索测试需要
       精确控制相似度，靠哈希编出来的向量无法把"相似度 0.95"这种前提写清楚。
    2. **未给定则按文本哈希生成**——只要"同文本 → 同向量、不同文本 → 不同向量"
       就够用的场景（如"删了就不再检索到"）。

    生成/给定的向量一律 **L2 归一化**：``memory_vectors`` 用的是余弦距离，
    归一化让测试里的期望值可以直接用余弦算，不必手算 L2。
    """

    def __init__(
        self,
        *,
        dim: int = 8,
        vectors: Mapping[str, Sequence[float]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._dim = dim
        self._vectors = {text: _normalize(vector) for text, vector in (vectors or {}).items()}
        self._error = error
        self.calls: list[tuple[str, ...]] = []

    @property
    def model(self) -> str:
        return "fake-embedding"

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(tuple(texts))

        if self._error is not None:
            raise self._error

        return tuple(self._vector_for(text) for text in texts)

    def _vector_for(self, text: str) -> tuple[float, ...]:
        explicit = self._vectors.get(text)
        if explicit is not None:
            if len(explicit) != self._dim:
                msg = f"FakeEmbeddingProvider: explicit vector for {text!r} has wrong dim"
                raise AssertionError(msg)
            return explicit

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [((digest[i % len(digest)] / 255.0) * 2.0) - 1.0 for i in range(self._dim)]
        return _normalize(raw)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeSTTProvider:
    """脚本化的 :class:`astra.domain.ports.STTProvider`。

    默认返回固定文本；测试可以读 ``audio`` 断言"送进来的 PCM 是不是原样"。
    """

    def __init__(
        self,
        *,
        text: str = "你好呀",
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.audio: list[bytes] = []
        self.sample_rates: list[int] = []
        self._text = text
        self._error = error
        self._delay_s = delay_s

    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        language_hint: str | None = None,
    ) -> Transcript:
        self.audio.append(audio)
        self.sample_rates.append(sample_rate)

        if self._delay_s:
            await asyncio.sleep(self._delay_s)

        if self._error is not None:
            raise self._error

        return Transcript(text=self._text)

    @property
    def call_count(self) -> int:
        return len(self.audio)


class FakeTTSProvider:
    """脚本化的 :class:`astra.domain.ports.TTSProvider`。

    默认返回一段可预测的 int16 PCM（``samples`` 个采样，全为 ``value``），
    这样"切了几帧、每帧多大"这类断言可以直接算出来。
    """

    def __init__(
        self,
        *,
        samples: int = 1600,
        value: int = 1000,
        sample_rate: int = 24000,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.requests: list[TTSRequest] = []
        self._pcm = struct.pack(f"<{samples}h", *([value] * samples))
        self._format = AudioFormat(sample_rate=sample_rate, channels=1)
        self._error = error
        self._delay_s = delay_s

    @property
    def pcm(self) -> bytes:
        return self._pcm

    async def synthesize(self, request: TTSRequest) -> AudioArtifact:
        self.requests.append(request)

        if self._delay_s:
            await asyncio.sleep(self._delay_s)

        if self._error is not None:
            raise self._error

        return AudioArtifact(pcm=self._pcm, format=self._format)

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(float(value) for value in vector)
    return tuple(value / norm for value in vector)
