"""OpenAI 兼容的语音识别实现（``STTProvider`` 端口）。

走 ``POST /audio/transcriptions``：**multipart 上传一个 WAV**，与 CosyVoice 那条路
的差别只在"怎么送字节"（那边走 Gradio 的上传端点）与"怎么读结果"。

端口收进来的是已经解码的 int16 PCM（``docs/07`` §2），所以这里用
:func:`astra.infrastructure.audio.codec.encode_wav` 按单声道包成 WAV——
16 kHz 单声道正是这些后端的输入惯例，不需要重采样。

**响应形状不假设**：不显式要 ``response_format``（部分厂商不认这个参数），
读回来先当 JSON 找 ``text``，不是 JSON 就整段当纯文本——两种在真实服务里都见过。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from astra.domain.audio import AudioFormat, Transcript
from astra.domain.errors import STTUnavailableError
from astra.infrastructure.audio.codec import encode_wav
from astra.providers.openai_compatible.errors import translate


class OpenAICompatibleSTTProvider:
    """通过 OpenAI 兼容的 ``/audio/transcriptions`` 做语音识别。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float,
        language: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        #: 配置里的默认语言提示；``transcribe`` 的入参优先于它
        self._language = language
        self._transport = transport

    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        language_hint: str | None = None,
    ) -> Transcript:
        wav = encode_wav(audio, AudioFormat(sample_rate=sample_rate, channels=1))
        language = language_hint or self._language

        data: dict[str, str] = {"model": self._model}
        if language:
            data["language"] = language

        try:
            async with self._client() as client:
                response = await asyncio.wait_for(
                    client.post(
                        "/audio/transcriptions",
                        data=data,
                        files={"file": ("utterance.wav", wav, "audio/wav")},
                    ),
                    timeout=self._timeout_s,
                )
                response.raise_for_status()
        except TimeoutError as exc:
            raise STTUnavailableError("stt request exceeded total budget", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise translate(exc, unavailable=STTUnavailableError, backend="stt backend") from exc

        return _to_transcript(response, requested_language=language)

    # ── 内部 ────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(self._timeout_s),
            transport=self._transport,
        )


def _to_transcript(response: httpx.Response, *, requested_language: str | None) -> Transcript:
    """把响应变成 :class:`Transcript`。

    Raises:
        STTUnavailableError: 结构完全对不上（JSON 里没有 ``text`` 字段，
            或响应体是空的）。**空转写（静音）不算错**——那是合法结果，
            照实返回空字符串，由上层决定要不要提示用户重说。
    """
    body = response.text

    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        raw = parsed.get("text")
        if not isinstance(raw, str):
            msg = "stt backend returned an unexpected response shape"
            raise STTUnavailableError(msg, detail=body[:200])
        language = parsed.get("language")
        return Transcript(
            text=raw.strip(),
            language=language if isinstance(language, str) and language else requested_language,
        )

    stripped = body.strip()
    if not stripped:
        raise STTUnavailableError("stt backend returned an empty response")

    # 非 JSON 就整段当文本（部分兼容服务直接用 text/plain 回)
    return Transcript(text=stripped, language=requested_language)
