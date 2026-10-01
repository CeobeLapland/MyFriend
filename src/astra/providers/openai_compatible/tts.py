"""OpenAI 兼容的语音合成实现（``TTSProvider`` 端口）。

走 ``POST /audio/speech``：请求体是 JSON（``model`` / ``input`` / ``voice`` /
``response_format`` / ``speed``），**响应体直接就是音频字节**（不是文件路径、
不是 JSON 包一层）。

**格式策略**：默认请求 ``wav``。第三方 `pcm` 是裸流、不带采样率，用它就得硬编码
一个厂商专属的采样率；WAV 自带采样率，对"任何兼容服务"这个目标更稳。
拿到什么容器由 ``decode_audio`` 解码成 int16（``docs/07`` §3：上层只认 PCM）。

**音色名是后端专属的**：``TTSRequest.voice_id`` 直接就是厂商的音色标识
（如 ``alloy``），不在这里做跨厂商映射——那需要一张没有依据的对照表。
"""

from __future__ import annotations

import asyncio

import httpx

from astra.domain.audio import BYTES_PER_SAMPLE, AudioArtifact, AudioFormat, TTSRequest
from astra.domain.errors import AudioFormatError, TTSUnavailableError
from astra.infrastructure.audio.codec import decode_audio
from astra.providers.openai_compatible.errors import translate


class OpenAICompatibleTTSProvider:
    """通过 OpenAI 兼容的 ``/audio/speech`` 合成语音。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float,
        response_format: str = "wav",
        pcm_sample_rate: int = 24000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._response_format = response_format
        #: 只在 ``response_format=pcm`` 时有意义——裸流没有采样率可读
        self._pcm_sample_rate = pcm_sample_rate
        self._transport = transport

    async def synthesize(self, request: TTSRequest) -> AudioArtifact:
        payload = {
            "model": self._model,
            "input": request.text,
            "voice": request.voice_id,
            "response_format": self._response_format,
            "speed": request.speed,
        }

        try:
            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.post("/audio/speech", json=payload),
                    timeout=self._timeout_s,
                )
                response.raise_for_status()
                raw = response.content
        except TimeoutError as exc:
            raise TTSUnavailableError("tts request exceeded total budget", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise translate(exc, unavailable=TTSUnavailableError, backend="tts backend") from exc

        return self._to_artifact(raw)

    # ── 内部 ────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(self._timeout_s),
            transport=self._transport,
        )

    def _to_artifact(self, raw: bytes) -> AudioArtifact:
        """把响应字节变成 int16 PCM。

        ``pcm`` 是唯一的短路分支：裸流没有容器可解，采样率只能按约定补。
        其余格式交给 ``decode_audio`` 按**内容**分派（WAV 或 mp3）——厂商忽略
        ``response_format`` 仍然回 mp3 时也能正常工作。
        """
        if not raw:
            raise TTSUnavailableError("tts backend returned an empty body")

        if self._response_format == "pcm":
            # 裸流没有容器可解，采样率只能按约定补；奇数字节意味着尾部有半个采样
            aligned = len(raw) - len(raw) % BYTES_PER_SAMPLE
            if aligned == 0:
                raise TTSUnavailableError("tts backend returned a partial sample")
            return AudioArtifact(
                pcm=raw[:aligned],
                format=AudioFormat(sample_rate=self._pcm_sample_rate, channels=1),
            )

        try:
            decoded = decode_audio(raw)
        except AudioFormatError as exc:
            raise TTSUnavailableError(
                "tts backend returned undecodable audio", detail=str(exc)
            ) from exc

        return AudioArtifact(pcm=decoded.pcm, format=decoded.format)
