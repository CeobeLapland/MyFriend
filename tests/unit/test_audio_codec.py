"""``astra.infrastructure.audio.codec`` 的测试。

重点覆盖三件容易出错的事：

1. **float32 → int16 的削波**：CosyVoice 雨落版实际返回的就是 float32 WAV
   （实测 ``audioformat=3``），而 stdlib ``wave`` 读不了它。
2. **RIFF 分块遍历**：真实文件里常夹着 ``LIST`` / ``fact`` 等 chunk，
   只按固定偏移读会读出噪声。
3. **mp3 的按内容分派**（第三方 TTS）：声明 ``wav`` 却收到 mp3 时也必须能解，
   而且**不能**让 miniaudio 用它的默认参数（44100 Hz 立体声）重采样。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from astra.domain.audio import AudioFormat
from astra.domain.errors import AudioFormatError
from astra.infrastructure.audio.codec import decode_audio, decode_wav, encode_wav

#: 一段 0.2 秒、440 Hz 正弦波的 mp3（1 KB）。用 ``lameenc`` 一次性生成：
#: ``set_bit_rate(32)`` / ``set_in_sample_rate(24000)`` / ``set_channels(1)``；
#: 实际落地为 22050 Hz 单声道（LAME 取最接近的合法采样率）。
_MP3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tone_mono.mp3"


def _float32_wav(samples: list[float], *, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """构造一个 IEEE float32 WAV（就是雨落版 TTS 的实际输出形态）。"""
    data = struct.pack(f"<{len(samples)}f", *samples)
    block_align = channels * 4
    return (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (3).to_bytes(2, "little")  # IEEE float
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * block_align).to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (32).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )


def _pcm16(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


# ── encode_wav / decode_wav 往返 ────────────────────────


def test_round_trip_preserves_samples_and_format() -> None:
    pcm = _pcm16(0, 1000, -1000, 32767, -32768)
    fmt = AudioFormat(sample_rate=16000, channels=1)

    decoded = decode_wav(encode_wav(pcm, fmt))

    assert decoded.pcm == pcm
    assert decoded.format == fmt


def test_encoded_wav_has_the_expected_riff_skeleton() -> None:
    blob = encode_wav(_pcm16(1, 2), AudioFormat(sample_rate=16000))

    assert blob[:4] == b"RIFF"
    assert blob[8:12] == b"WAVE"
    assert blob[12:16] == b"fmt "
    assert blob[36:40] == b"data"


def test_encode_rejects_empty_pcm_round_trip_of_pcm16() -> None:
    """空载荷是合法的 WAV（44 字节纯头）——空音频的拦截在 ``AudioArtifact`` 那层。"""
    blob = encode_wav(b"", AudioFormat(sample_rate=16000))

    assert len(blob) == 44
    assert decode_wav(blob).pcm == b""


def test_odd_length_payload_gets_a_padding_byte() -> None:
    """RIFF 要求奇数长度的 chunk 后补一个填充字节。"""
    blob = encode_wav(_pcm16(1, 2, 3) + b"\x00", AudioFormat(sample_rate=16000))

    assert len(blob) % 2 == 0


# ── float32 解码（雨落版的实际形态）─────────────────────


def test_float32_wav_is_decoded_to_int16() -> None:
    decoded = decode_wav(_float32_wav([0.0, 0.5, -0.5, 1.0]))

    assert decoded.format.sample_rate == 24000
    assert decoded.format.channels == 1
    assert struct.unpack("<4h", decoded.pcm) == (0, 16383, -16383, 32767)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.5, 32767), (-1.5, -32767), (2.0, 32767), (-2.0, -32767)],
)
def test_out_of_range_floats_are_clipped_not_wrapped(value: float, expected: int) -> None:
    """回绕会把一个响亮的音节变成一声爆响——必须削波。"""
    decoded = decode_wav(_float32_wav([value]))

    assert struct.unpack("<h", decoded.pcm)[0] == expected


def test_float32_wav_preserves_channels_and_sample_rate() -> None:
    decoded = decode_wav(_float32_wav([0.1, 0.2, 0.3, 0.4], sample_rate=44100, channels=2))

    assert decoded.format == AudioFormat(sample_rate=44100, channels=2)
    assert len(decoded.pcm) == 4 * 2


# ── 分块遍历与错误路径 ──────────────────────────────────


def test_unknown_chunks_are_skipped() -> None:
    """真实文件常夹着 LIST/fact；按固定偏移读会读出噪声。"""
    pcm = _pcm16(11, 22, 33)
    base = encode_wav(pcm, AudioFormat(sample_rate=16000))
    # 在 fmt 与 data 之间插入一个 LIST chunk（偶数长度，不触发填充）
    injected = b"LIST" + (4).to_bytes(4, "little") + b"INFO"
    fmt_end = 12 + 8 + 16
    patched = base[:fmt_end] + injected + base[fmt_end:]
    patched = patched[:4] + (len(patched) - 8).to_bytes(4, "little") + patched[8:]

    assert decode_wav(patched).pcm == pcm


@pytest.mark.parametrize(
    "blob",
    [b"", b"NOPE", b"RIFF\x00\x00\x00\x00NOPE", b"RIFF\x10\x00\x00\x00WAVE"],
)
def test_non_wave_input_is_rejected(blob: bytes) -> None:
    with pytest.raises(AudioFormatError):
        decode_wav(blob)


def test_wav_without_data_chunk_is_rejected() -> None:
    header_only = (
        b"RIFF"
        + (36).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (16000).to_bytes(4, "little")
        + (32000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )

    with pytest.raises(AudioFormatError, match="data chunk"):
        decode_wav(header_only)


def test_truncated_chunk_is_rejected() -> None:
    blob = encode_wav(_pcm16(1, 2, 3), AudioFormat(sample_rate=16000))

    with pytest.raises(AudioFormatError, match="truncated"):
        decode_wav(blob[:-4])


def test_unsupported_bit_depth_is_rejected() -> None:
    """8-bit PCM 不在支持范围内——静默当成 16-bit 会得到一半长度的噪声。"""
    data = b"\x80\x81\x82\x83"
    blob = (
        b"RIFF"
        + (36 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8000).to_bytes(4, "little")
        + (8000).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (8).to_bytes(2, "little")
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )

    with pytest.raises(AudioFormatError, match="unsupported WAV encoding"):
        decode_wav(blob)


# ── decode_audio：按内容分派（WAV / mp3）─────────────────


def test_decode_audio_sniffs_wav() -> None:
    pcm = _pcm16(1, 2, 3, 4)
    blob = encode_wav(pcm, AudioFormat(sample_rate=16000))

    decoded = decode_audio(blob)

    assert decoded.pcm == pcm
    assert decoded.format.sample_rate == 16000


def test_decode_audio_decodes_mp3_at_its_own_sample_rate() -> None:
    """**不能**用 miniaudio 的默认参数：它会把 22050 单声道重采样成 44100 立体声
    （实测采样数从 5760 涨到 23040），白白翻倍还改了音质。"""
    decoded = decode_audio(_MP3_FIXTURE.read_bytes())

    assert decoded.format.sample_rate == 22050
    assert decoded.format.channels == 1
    assert len(decoded.pcm) == 5760 * 2  # int16 → 两字节一个采样


def test_decode_audio_rejects_garbage() -> None:
    """既不是 WAV 也不是 mp3 → 稳定错误码，而不是抛 miniaudio 的异常。"""
    with pytest.raises(AudioFormatError):
        decode_audio(b"\x00" * 64)


def test_decode_audio_rejects_empty_input() -> None:
    with pytest.raises(AudioFormatError):
        decode_audio(b"")
