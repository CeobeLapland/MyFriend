"""``OpenAICompatibleSTTProvider`` / ``OpenAICompatibleTTSProvider`` 的单元测试。

两条路各有一个"真实世界里最常见的意外"，测试就是为它们写的：

- STT：响应可能不是 JSON（部分兼容服务直接用 ``text/plain`` 回文本）。
- TTS：我们请求了 ``wav``，厂商却忽略这个参数回了 mp3。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from astra.domain.audio import AudioFormat, TTSRequest
from astra.domain.errors import STTUnavailableError, TTSUnavailableError
from astra.infrastructure.audio.codec import encode_wav
from astra.providers.openai_compatible.stt import OpenAICompatibleSTTProvider
from astra.providers.openai_compatible.tts import OpenAICompatibleTTSProvider

_MP3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tone_mono.mp3"
_BASE_URL = "https://api.example.com/v1"
_PCM = b"\x01\x02\x03\x04" * 100


def _wav(sample_rate: int = 16000) -> bytes:
    return encode_wav(_PCM, AudioFormat(sample_rate=sample_rate, channels=1))


# ── STT ─────────────────────────────────────────────────


def _stt(handler: Any, **overrides: Any) -> OpenAICompatibleSTTProvider:
    params: dict[str, Any] = {
        "base_url": _BASE_URL,
        "model": "whisper-1",
        "api_key": "test-key",
        "timeout_s": 120.0,
    }
    params.update(overrides)
    return OpenAICompatibleSTTProvider(transport=httpx.MockTransport(handler), **params)


def _stt_capture(response: httpx.Response, **overrides: Any) -> tuple[Any, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        holder["path"] = request.url.path
        holder["body"] = request.content
        holder["content_type"] = request.headers.get("content-type", "")
        return response

    return _stt(handler, **overrides), holder


def test_stt_uploads_a_wav_as_multipart() -> None:
    provider, holder = _stt_capture(httpx.Response(200, json={"text": "你好"}))

    asyncio.run(provider.transcribe(_PCM, 16000))

    assert holder["path"] == "/v1/audio/transcriptions"
    assert holder["content_type"].startswith("multipart/form-data")
    assert b'name="model"' in holder["body"]
    assert b"whisper-1" in holder["body"]
    # 送上去的必须是我们自己包的 WAV（STT 后端要文件，不是裸 PCM）
    assert b"RIFF" in holder["body"]


def test_stt_returns_transcript_and_language() -> None:
    provider, _ = _stt_capture(httpx.Response(200, json={"text": " 你好 ", "language": "zh"}))

    result = asyncio.run(provider.transcribe(_PCM, 16000))

    assert result.text == "你好"
    assert result.language == "zh"


def test_stt_accepts_plain_text_responses() -> None:
    """部分兼容服务不认 ``response_format``，直接回 ``text/plain``。"""
    provider, _ = _stt_capture(httpx.Response(200, text="你好呀"))

    assert asyncio.run(provider.transcribe(_PCM, 16000)).text == "你好呀"


def test_stt_language_comes_from_the_argument_first() -> None:
    provider, holder = _stt_capture(httpx.Response(200, json={"text": "hi"}), language="zh")

    result = asyncio.run(provider.transcribe(_PCM, 16000, language_hint="en"))

    assert b"en" in holder["body"]
    assert result.language == "en"


def test_stt_configured_language_is_used_when_no_hint() -> None:
    provider, holder = _stt_capture(httpx.Response(200, json={"text": "你好"}), language="zh")

    asyncio.run(provider.transcribe(_PCM, 16000))

    assert b'name="language"' in holder["body"]


def test_stt_silence_is_a_valid_empty_transcript() -> None:
    """静音时后端回空文本是正常结果，不该报成"后端挂了"。"""
    provider, _ = _stt_capture(httpx.Response(200, json={"text": ""}))

    assert asyncio.run(provider.transcribe(_PCM, 16000)).text == ""


def test_stt_unexpected_json_shape_is_an_error() -> None:
    provider, _ = _stt_capture(httpx.Response(200, json={"result": "你好"}))

    with pytest.raises(STTUnavailableError):
        asyncio.run(provider.transcribe(_PCM, 16000))


def test_stt_empty_body_is_an_error() -> None:
    provider, _ = _stt_capture(httpx.Response(200, text=""))

    with pytest.raises(STTUnavailableError):
        asyncio.run(provider.transcribe(_PCM, 16000))


def test_stt_http_error_is_translated() -> None:
    provider, _ = _stt_capture(httpx.Response(401, text="unauthorized"))

    with pytest.raises(STTUnavailableError, match="HTTP 401"):
        asyncio.run(provider.transcribe(_PCM, 16000))


def test_stt_timeout_is_translated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"text": "太慢"})

    with pytest.raises(STTUnavailableError):
        asyncio.run(_stt(handler, timeout_s=0.05).transcribe(_PCM, 16000))


# ── TTS ─────────────────────────────────────────────────


def _tts(handler: Any, **overrides: Any) -> OpenAICompatibleTTSProvider:
    params: dict[str, Any] = {
        "base_url": _BASE_URL,
        "model": "tts-1",
        "api_key": "test-key",
        "timeout_s": 120.0,
    }
    params.update(overrides)
    return OpenAICompatibleTTSProvider(transport=httpx.MockTransport(handler), **params)


def _tts_capture(response: httpx.Response, **overrides: Any) -> tuple[Any, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        holder["path"] = request.url.path
        holder["payload"] = json.loads(request.content.decode("utf-8"))
        return response

    return _tts(handler, **overrides), holder


def _request(voice: str = "alloy") -> TTSRequest:
    return TTSRequest(text="你好呀", voice_id=voice, speed=1.0)


def test_tts_posts_json_and_sends_the_voice() -> None:
    provider, holder = _tts_capture(
        httpx.Response(200, content=_wav(), headers={"content-type": "audio/wav"})
    )

    asyncio.run(provider.synthesize(_request()))

    assert holder["path"] == "/v1/audio/speech"
    assert holder["payload"] == {
        "model": "tts-1",
        "input": "你好呀",
        "voice": "alloy",
        "response_format": "wav",
        "speed": 1.0,
    }


def test_tts_decodes_wav_at_its_own_sample_rate() -> None:
    provider, _ = _tts_capture(httpx.Response(200, content=_wav(24000)))

    artifact = asyncio.run(provider.synthesize(_request()))

    assert artifact.format.sample_rate == 24000
    assert artifact.pcm == _PCM


def test_tts_decodes_mp3_when_the_vendor_ignores_response_format() -> None:
    """我们请求 wav，厂商回了 mp3——按内容分派，照样能播。"""
    provider, _ = _tts_capture(
        httpx.Response(
            200, content=_MP3_FIXTURE.read_bytes(), headers={"content-type": "audio/mpeg"}
        )
    )

    artifact = asyncio.run(provider.synthesize(_request()))

    assert artifact.format.sample_rate == 22050
    assert artifact.format.channels == 1
    assert artifact.pcm


def test_tts_pcm_is_interpreted_with_the_configured_sample_rate() -> None:
    """裸流没有采样率可读，只能按配置补——所以它必须出现在 tts.started 里。"""
    provider, _ = _tts_capture(
        httpx.Response(200, content=_PCM), response_format="pcm", pcm_sample_rate=24000
    )

    artifact = asyncio.run(provider.synthesize(_request()))

    assert artifact.pcm == _PCM
    assert artifact.format.sample_rate == 24000
    assert artifact.format.channels == 1


def test_tts_pcm_drops_a_trailing_half_sample() -> None:
    provider, _ = _tts_capture(httpx.Response(200, content=_PCM + b"\x00"), response_format="pcm")

    assert asyncio.run(provider.synthesize(_request())).pcm == _PCM


def test_tts_empty_body_is_an_error() -> None:
    provider, _ = _tts_capture(httpx.Response(200, content=b""))

    with pytest.raises(TTSUnavailableError):
        asyncio.run(provider.synthesize(_request()))


def test_tts_undecodable_body_is_an_error() -> None:
    provider, _ = _tts_capture(httpx.Response(200, content=b"\x00" * 128))

    with pytest.raises(TTSUnavailableError):
        asyncio.run(provider.synthesize(_request()))


def test_tts_http_error_is_translated() -> None:
    provider, _ = _tts_capture(httpx.Response(429, text="rate limited"))

    with pytest.raises(TTSUnavailableError, match="HTTP 429"):
        asyncio.run(provider.synthesize(_request()))
