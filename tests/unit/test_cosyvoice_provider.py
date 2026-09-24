"""``astra.providers.cosyvoice`` 的单元测试（用 ``httpx.MockTransport``）。

**刻意不连真实的雨落版进程**：它要占 GPU、冷启要几秒，CI 不该依赖它。
真实后端在真机验收里跑。这里锁住的是**协议接线**：

- 调用序列对不对（TTS 是"POST 拿 event_id → GET SSE → GET 文件"，
  STT 多一步"先上传"）；
- float32 WAV 是否被转成 int16 PCM；
- 心跳事件会不会被误当成结果；
- 后端各种失败是否都被翻译成 ``STTUnavailableError`` / ``TTSUnavailableError``。
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from astra.domain.audio import TTSRequest
from astra.domain.errors import STTUnavailableError, TTSUnavailableError
from astra.domain.health import HealthStatus
from astra.providers.cosyvoice.client import GradioCallClient, GradioCallError
from astra.providers.cosyvoice.health import CosyVoiceHealthProbe
from astra.providers.cosyvoice.stt import CosyVoiceSTTProvider
from astra.providers.cosyvoice.tts import CosyVoiceTTSProvider

BASE_URL = "http://tts.test"
FILE_URL = f"{BASE_URL}/gradio_api/file=out.wav"
SFT_PATH = "/gradio_api/call/rainfall_gen_sft"
STT_PATH = "/gradio_api/call/prompt_wav_recognition"


def _sse(*events: tuple[str, str]) -> bytes:
    chunks = [f"event: {name}\ndata: {payload}\n\n" for name, payload in events]
    return "".join(chunks).encode()


def _float32_wav(samples: list[float], *, sample_rate: int = 24000) -> bytes:
    data = struct.pack(f"<{len(samples)}f", *samples)
    return (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (3).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 4).to_bytes(4, "little")
        + (4).to_bytes(2, "little")
        + (32).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )


def _pcm16_wav(*values: int, sample_rate: int = 16000) -> bytes:
    data = struct.pack(f"<{len(values)}h", *values)
    return (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 2).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )


def _tts(handler: Any, **overrides: Any) -> CosyVoiceTTSProvider:
    params: dict[str, Any] = {
        "base_url": BASE_URL,
        "voice_id": "开拓者",
        "timeout_s": 30.0,
    }
    params.update(overrides)
    return CosyVoiceTTSProvider(transport=httpx.MockTransport(handler), **params)


def _stt(handler: Any, **overrides: Any) -> CosyVoiceSTTProvider:
    params: dict[str, Any] = {"base_url": BASE_URL, "timeout_s": 30.0}
    params.update(overrides)
    return CosyVoiceSTTProvider(transport=httpx.MockTransport(handler), **params)


def _happy_tts_handler(seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == SFT_PATH:
            return httpx.Response(200, json={"event_id": "evt-1"})
        if request.url.path == f"{SFT_PATH}/evt-1":
            return httpx.Response(
                200,
                content=_sse(
                    ("heartbeat", "null"),
                    ("complete", json.dumps([{"path": "out.wav", "url": FILE_URL}])),
                ),
            )
        if request.url.path == "/gradio_api/file=out.wav":
            return httpx.Response(
                200, content=_float32_wav([0.0, 1.0, -1.0]), headers={"content-type": "audio/wav"}
            )
        return httpx.Response(404)

    return handler


# ── TTS ─────────────────────────────────────────────────


def test_tts_converts_float32_wav_to_int16_pcm() -> None:
    seen: list[httpx.Request] = []
    artifact = asyncio.run(
        _tts(_happy_tts_handler(seen)).synthesize(TTSRequest(text="你好。", voice_id="开拓者"))
    )

    assert artifact.format.sample_rate == 24000
    assert artifact.format.channels == 1
    assert artifact.format.encoding == "pcm16"
    assert struct.unpack("<3h", artifact.pcm) == (0, 32767, -32767)


def test_tts_sends_text_voice_and_speed_in_order() -> None:
    """Gradio 的 ``data`` 是**位置参数**，顺序错一个就会拿文本当音色名。"""
    seen: list[httpx.Request] = []
    asyncio.run(
        _tts(_happy_tts_handler(seen)).synthesize(
            TTSRequest(text="你好。", voice_id="开拓者", speed=1.25)
        )
    )

    body = json.loads(seen[0].content)
    assert body["data"][0] == "你好。"
    assert body["data"][1] == "开拓者"
    assert body["data"][2] == 1.25
    assert body["data"][5] == "wav"


def test_tts_ignores_heartbeat_events() -> None:
    """心跳的 ``data`` 是 ``null``；把它当结果会解析出 None 而不是文件引用。"""
    seen: list[httpx.Request] = []
    artifact = asyncio.run(
        _tts(_happy_tts_handler(seen)).synthesize(TTSRequest(text="你好。", voice_id="v"))
    )

    assert artifact.pcm


def test_tts_error_event_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SFT_PATH:
            return httpx.Response(200, json={"event_id": "evt-1"})
        return httpx.Response(200, content=_sse(("error", "boom")))

    with pytest.raises(TTSUnavailableError):
        asyncio.run(_tts(handler).synthesize(TTSRequest(text="你好。", voice_id="v")))


def test_tts_connection_failure_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TTSUnavailableError, match="backend failed"):
        asyncio.run(_tts(handler).synthesize(TTSRequest(text="你好。", voice_id="v")))


def test_tts_undecodable_audio_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SFT_PATH:
            return httpx.Response(200, json={"event_id": "evt-1"})
        if request.url.path == f"{SFT_PATH}/evt-1":
            return httpx.Response(200, content=_sse(("complete", json.dumps([{"url": FILE_URL}]))))
        return httpx.Response(200, content=b"definitely not a wav")

    with pytest.raises(TTSUnavailableError, match="undecodable"):
        asyncio.run(_tts(handler).synthesize(TTSRequest(text="你好。", voice_id="v")))


def test_tts_missing_file_reference_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SFT_PATH:
            return httpx.Response(200, json={"event_id": "evt-1"})
        return httpx.Response(200, content=_sse(("complete", json.dumps([None]))))

    with pytest.raises(TTSUnavailableError):
        asyncio.run(_tts(handler).synthesize(TTSRequest(text="你好。", voice_id="v")))


# ── TTS：zero-shot 音色克隆（配了参考音频时）────────────

ZERO_SHOT_PATH = "/gradio_api/call/rainfall_gen_zero_shot"


def _reference_file(tmp_path: Path) -> Path:
    path = tmp_path / "voice.wav"
    path.write_bytes(_pcm16_wav(1, 2, 3))
    return path


def _zero_shot_handler(seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=["server/voice.wav"])
        if request.url.path == "/gradio_api/call/prompt_wav_recognition":
            return httpx.Response(200, json={"event_id": "rec-1"})
        if request.url.path == "/gradio_api/call/prompt_wav_recognition/rec-1":
            return httpx.Response(
                200, content=_sse(("complete", json.dumps(["这是参考音频的内容。"])))
            )
        if request.url.path == ZERO_SHOT_PATH:
            return httpx.Response(200, json={"event_id": "zs-1"})
        if request.url.path == f"{ZERO_SHOT_PATH}/zs-1":
            return httpx.Response(
                200,
                content=_sse(("complete", json.dumps([{"path": "out.wav", "url": FILE_URL}]))),
            )
        if request.url.path == "/gradio_api/file=out.wav":
            return httpx.Response(200, content=_float32_wav([0.0, 0.5]))
        return httpx.Response(404)

    return handler


def test_zero_shot_is_used_when_a_reference_audio_is_configured(tmp_path: Path) -> None:
    """配了参考音频就走克隆，且**先上传再识别再合成**——顺序错了拿不到 prompt_text。"""
    seen: list[httpx.Request] = []
    provider = _tts(_zero_shot_handler(seen), reference_audio=_reference_file(tmp_path))

    artifact = asyncio.run(provider.synthesize(TTSRequest(text="你好。", voice_id="x")))

    assert artifact.pcm
    assert [r.url.path for r in seen] == [
        "/gradio_api/upload",
        "/gradio_api/call/prompt_wav_recognition",
        "/gradio_api/call/prompt_wav_recognition/rec-1",
        ZERO_SHOT_PATH,
        f"{ZERO_SHOT_PATH}/zs-1",
        "/gradio_api/file=out.wav",
    ]


def test_zero_shot_passes_the_recognised_prompt_text(tmp_path: Path) -> None:
    """Gradio 的 ``data`` 是位置参数：``[文本, 参考音频, 参考文本, 语速, ...]``。"""
    seen: list[httpx.Request] = []
    provider = _tts(_zero_shot_handler(seen), reference_audio=_reference_file(tmp_path))

    asyncio.run(provider.synthesize(TTSRequest(text="你好。", voice_id="x", speed=1.25)))

    body = json.loads(seen[3].content)
    assert body["data"][0] == "你好。"
    assert body["data"][1]["path"] == "server/voice.wav"
    assert body["data"][1]["meta"]["_type"] == "gradio.FileData"
    assert body["data"][2] == "这是参考音频的内容。"
    assert body["data"][3] == 1.25
    assert body["data"][6] == "wav"


def test_reference_audio_is_uploaded_only_once(tmp_path: Path) -> None:
    """上传与识别只在首次合成时做——每轮都重来是两次无谓的往返。"""
    seen: list[httpx.Request] = []
    provider = _tts(_zero_shot_handler(seen), reference_audio=_reference_file(tmp_path))

    asyncio.run(provider.synthesize(TTSRequest(text="第一句", voice_id="x")))
    asyncio.run(provider.synthesize(TTSRequest(text="第二句", voice_id="x")))

    uploads = [r for r in seen if r.url.path == "/gradio_api/upload"]
    assert len(uploads) == 1


def test_missing_reference_file_becomes_unavailable(tmp_path: Path) -> None:
    """参考音频路径写错时要给出明确错误，而不是让整个 turn 静默失败。"""
    seen: list[httpx.Request] = []
    provider = _tts(_zero_shot_handler(seen), reference_audio=tmp_path / "nope.wav")

    with pytest.raises(TTSUnavailableError, match="reference audio"):
        asyncio.run(provider.synthesize(TTSRequest(text="你好。", voice_id="x")))


def test_empty_reference_transcript_becomes_unavailable(tmp_path: Path) -> None:
    """识别不出参考音频的文本时，zero-shot 没法用——要明确报错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=["server/voice.wav"])
        if request.url.path == "/gradio_api/call/prompt_wav_recognition":
            return httpx.Response(200, json={"event_id": "rec-1"})
        return httpx.Response(200, content=_sse(("complete", json.dumps(["   "]))))

    provider = _tts(handler, reference_audio=_reference_file(tmp_path))

    with pytest.raises(TTSUnavailableError, match="empty transcript"):
        asyncio.run(provider.synthesize(TTSRequest(text="你好。", voice_id="x")))


# ── STT ─────────────────────────────────────────────────


def test_stt_uploads_before_calling() -> None:
    """这个端点收的是**文件**：不上传就调，Gradio 会报错。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=[r"C:\tmp\uploaded.wav"])
        if request.url.path == STT_PATH:
            return httpx.Response(200, json={"event_id": "evt-2"})
        if request.url.path == f"{STT_PATH}/evt-2":
            return httpx.Response(200, content=_sse(("complete", json.dumps(["你好呀。"]))))
        return httpx.Response(404)

    transcript = asyncio.run(_stt(handler).transcribe(b"\x00\x00" * 100, sample_rate=16000))

    assert transcript.text == "你好呀。"
    assert [r.url.path for r in seen] == [
        "/gradio_api/upload",
        STT_PATH,
        f"{STT_PATH}/evt-2",
    ]


def test_stt_sends_the_uploaded_path_as_a_gradio_filedata() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=[r"C:\tmp\uploaded.wav"])
        if request.url.path == STT_PATH:
            return httpx.Response(200, json={"event_id": "evt-2"})
        return httpx.Response(200, content=_sse(("complete", json.dumps(["嗯。"]))))

    asyncio.run(_stt(handler).transcribe(b"\x00\x00" * 10, sample_rate=16000))

    call_body = json.loads(seen[1].content)
    assert call_body["data"][0]["path"] == r"C:\tmp\uploaded.wav"
    assert call_body["data"][0]["meta"]["_type"] == "gradio.FileData"


def test_stt_wraps_pcm_into_a_wav_before_uploading() -> None:
    """上传的必须是 WAV 容器，不是裸 PCM。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=["p.wav"])
        if request.url.path == STT_PATH:
            return httpx.Response(200, json={"event_id": "evt-2"})
        return httpx.Response(200, content=_sse(("complete", json.dumps(["嗯。"]))))

    asyncio.run(_stt(handler).transcribe(b"\x01\x00" * 10, sample_rate=16000))

    uploaded = seen[0].content
    assert b"RIFF" in uploaded
    assert b"WAVE" in uploaded


def test_stt_strips_whitespace_from_transcript() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=["p.wav"])
        if request.url.path == STT_PATH:
            return httpx.Response(200, json={"event_id": "e"})
        return httpx.Response(200, content=_sse(("complete", json.dumps(["  你好  "]))))

    transcript = asyncio.run(_stt(handler).transcribe(b"\x00\x00" * 10, sample_rate=16000))

    assert transcript.text == "你好"


def test_stt_non_text_result_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gradio_api/upload":
            return httpx.Response(200, json=["p.wav"])
        if request.url.path == STT_PATH:
            return httpx.Response(200, json={"event_id": "e"})
        return httpx.Response(200, content=_sse(("complete", json.dumps([{"nope": 1}]))))

    with pytest.raises(STTUnavailableError) as excinfo:
        asyncio.run(_stt(handler).transcribe(b"\x00\x00" * 10, sample_rate=16000))

    # 具体原因在 detail 里——外层消息是稳定的"backend failed"
    assert "expected text" in (excinfo.value.detail or "")


def test_stt_upload_failure_becomes_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(STTUnavailableError, match="backend failed"):
        asyncio.run(_stt(handler).transcribe(b"\x00\x00" * 10, sample_rate=16000))


# ── client 本身 ─────────────────────────────────────────


def test_client_reports_a_stream_that_ends_without_a_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SFT_PATH:
            return httpx.Response(200, json={"event_id": "e"})
        return httpx.Response(200, content=_sse(("heartbeat", "null")))

    client = GradioCallClient(
        base_url=BASE_URL, timeout_s=5.0, transport=httpx.MockTransport(handler)
    )

    with pytest.raises(GradioCallError, match="without a result"):
        asyncio.run(client.call("rainfall_gen_sft", ["x"]))


def test_client_rejects_a_missing_event_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    client = GradioCallClient(
        base_url=BASE_URL, timeout_s=5.0, transport=httpx.MockTransport(handler)
    )

    with pytest.raises(GradioCallError, match="event_id"):
        asyncio.run(client.call("rainfall_gen_sft", ["x"]))


# ── health ──────────────────────────────────────────────


def test_health_probe_is_ok_when_the_app_answers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"named_endpoints": {}})

    probe = CosyVoiceHealthProbe(
        component="tts", base_url=BASE_URL, timeout_s=2.0, transport=httpx.MockTransport(handler)
    )
    health = asyncio.run(probe.check())

    assert probe.name == "tts"
    assert health.status is HealthStatus.OK


def test_health_probe_is_down_when_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    probe = CosyVoiceHealthProbe(
        component="stt", base_url=BASE_URL, timeout_s=2.0, transport=httpx.MockTransport(handler)
    )
    health = asyncio.run(probe.check())

    assert health.status is HealthStatus.DOWN
    assert health.detail == {"error": "ConnectError"}
