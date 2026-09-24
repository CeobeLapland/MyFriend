"""``astra.domain.audio`` 值对象的不变量测试。

这些类型必须"构造即合法"——它们是跨层（provider → application → WS）传递的
数据，任何一层的 bug 都不该变成"下游拿到一个 sample_rate=0 的格式"。
"""

from __future__ import annotations

import pytest

from astra.domain.audio import (
    AudioArtifact,
    AudioFormat,
    Transcript,
    TTSRequest,
)

# ── AudioFormat ─────────────────────────────────────────


def test_default_format_is_16k_mono_pcm16() -> None:
    fmt = AudioFormat(sample_rate=16000)

    assert fmt.channels == 1
    assert fmt.encoding == "pcm16"
    assert fmt.bytes_per_second == 16000 * 2


def test_format_serializes_to_protocol_fields() -> None:
    """字段名就是协议字段名（进 ``tts.started`` / ``input.audio.started``）。"""
    assert AudioFormat(sample_rate=24000).to_dict() == {
        "encoding": "pcm16",
        "sample_rate": 24000,
        "channels": 1,
    }


@pytest.mark.parametrize("sample_rate", [0, -1, -16000])
def test_non_positive_sample_rate_rejected(sample_rate: int) -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        AudioFormat(sample_rate=sample_rate)


@pytest.mark.parametrize("channels", [0, 3, -1])
def test_unsupported_channel_count_rejected(channels: int) -> None:
    with pytest.raises(ValueError, match="channels"):
        AudioFormat(sample_rate=16000, channels=channels)


def test_unsupported_encoding_rejected_at_runtime() -> None:
    """这个字段来自客户端声明的 JSON，静态类型管不到——必须运行时拦。"""
    with pytest.raises(ValueError, match="encoding"):
        AudioFormat(sample_rate=16000, encoding="opus")  # type: ignore[arg-type]


def test_duration_ms_is_derived_from_byte_count() -> None:
    fmt = AudioFormat(sample_rate=16000, channels=1)

    # 16000 Hz * 2 byte * 1 声道 = 32000 byte/s；1 秒的字节数正好 1000 ms
    assert fmt.duration_ms(32000) == pytest.approx(1000.0)
    assert fmt.duration_ms(16000) == pytest.approx(500.0)


def test_duration_ms_of_empty_audio_is_zero() -> None:
    assert AudioFormat(sample_rate=16000).duration_ms(0) == 0.0


# ── Transcript ──────────────────────────────────────────


def test_transcript_allows_unknown_language_and_confidence() -> None:
    """provider 不一定给得出语言与置信度；缺了不该算错误。"""
    transcript = Transcript(text="你好呀")

    assert transcript.language is None
    assert transcript.confidence is None
    assert transcript.emotion_hint is None


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_transcript_confidence_boundaries_are_inclusive(confidence: float) -> None:
    assert Transcript(text="嗯", confidence=confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf")])
def test_transcript_rejects_out_of_range_confidence(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        Transcript(text="嗯", confidence=confidence)


# ── TTSRequest ──────────────────────────────────────────


def test_tts_request_defaults_to_normal_speed_without_style() -> None:
    request = TTSRequest(text="你好呀。", voice_id="开拓者")

    assert request.speed == 1.0
    assert request.style is None


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_tts_speed_boundaries_are_inclusive(speed: float) -> None:
    assert TTSRequest(text="嗯", voice_id="v", speed=speed).speed == speed


@pytest.mark.parametrize("speed", [0.49, 2.01, float("nan"), float("inf")])
def test_tts_rejects_out_of_range_speed(speed: float) -> None:
    with pytest.raises(ValueError, match="speed"):
        TTSRequest(text="嗯", voice_id="v", speed=speed)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_tts_rejects_blank_text(text: str) -> None:
    """空文本送给 TTS 是纯浪费——后端会白跑一次模型加载。"""
    with pytest.raises(ValueError, match="text"):
        TTSRequest(text=text, voice_id="v")


@pytest.mark.parametrize("voice_id", ["", "  "])
def test_tts_rejects_blank_voice_id(voice_id: str) -> None:
    with pytest.raises(ValueError, match="voice_id"):
        TTSRequest(text="嗯", voice_id=voice_id)


# ── AudioArtifact ───────────────────────────────────────


def test_artifact_duration_comes_from_its_own_format() -> None:
    artifact = AudioArtifact(pcm=b"\x00\x00" * 8000, format=AudioFormat(sample_rate=16000))

    assert artifact.duration_ms == pytest.approx(500.0)


def test_artifact_rejects_empty_pcm() -> None:
    """空音频是"合成成功但什么都没产出"，必须当成失败，不能悄悄当成静音。"""
    with pytest.raises(ValueError, match="empty"):
        AudioArtifact(pcm=b"", format=AudioFormat(sample_rate=16000))
