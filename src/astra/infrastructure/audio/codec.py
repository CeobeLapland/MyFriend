"""WAV ↔ int16 PCM 的编解码。

**为什么手写而不是用 stdlib ``wave``**：CosyVoice 雨落版的 TTS 返回的是
**32-bit IEEE float WAV**（实测 ``audioformat=3``），而 stdlib ``wave``
**不支持浮点**——``wave.open`` 会直接抛 ``Error('unknown format: 3')``（实测）。
所以必须自己走一遍 RIFF 分块。

**为什么不用 ``audioop``**：它在 Python 3.13 已被移除。虽然本项目跑在 3.12 上、
现在还能 import，但新代码不该依赖一个正在消失的模块。用 ``array`` 手工转换。

本模块只认两件事：**解码成 int16 PCM** 与 **把 int16 PCM 包成 WAV**。
浮点、容器、字节序的差异全部止步于此——上层永远拿 int16。
"""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass

from astra.domain.audio import BYTES_PER_SAMPLE, AudioFormat
from astra.domain.errors import AudioFormatError

_RIFF = b"RIFF"
_WAVE = b"WAVE"
_CHUNK_FMT = b"fmt "
_CHUNK_DATA = b"data"

#: ``fmt`` chunk 里的编码编号
_FORMAT_PCM = 1
_FORMAT_IEEE_FLOAT = 3

#: int16 满量程。浮点转定点时用它，而不是 32768——32767 保证不溢出到 -32768。
_INT16_MAX = 32767
_FLOAT_SCALE = 32768.0


@dataclass(frozen=True, slots=True)
class DecodedWav:
    """解码结果：int16 PCM + 它自己的格式。"""

    pcm: bytes
    format: AudioFormat


def decode_wav(data: bytes) -> DecodedWav:
    """把 WAV 字节解码成 int16 PCM。

    支持 ``PCM 16-bit`` 与 ``IEEE float 32-bit``（雨落版 TTS 的输出）。
    其它编码一律 :class:`AudioFormatError`——**不猜**，因为猜错会得到噪声。

    Raises:
        AudioFormatError: 不是合法 RIFF/WAVE、缺少 ``fmt``/``data`` chunk、
            或编码/位深不受支持。
    """
    if len(data) < 12 or data[:4] != _RIFF or data[8:12] != _WAVE:
        msg = "not a RIFF/WAVE file"
        raise AudioFormatError(msg)

    audio_format: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    bits: int | None = None
    raw: bytes | None = None

    pos = 12
    total = len(data)
    while pos + 8 <= total:
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body_start = pos + 8
        body_end = body_start + size
        if body_end > total:
            msg = f"truncated WAV chunk {chunk_id!r}: needs {size} bytes, has {total - body_start}"
            raise AudioFormatError(msg)
        body = data[body_start:body_end]

        if chunk_id == _CHUNK_FMT:
            if size < 16:
                msg = f"fmt chunk is too short: {size} bytes"
                raise AudioFormatError(msg)
            audio_format = int.from_bytes(body[0:2], "little")
            channels = int.from_bytes(body[2:4], "little")
            sample_rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
        elif chunk_id == _CHUNK_DATA:
            raw = body

        # chunk 按字对齐：奇数长度会补一个填充字节
        pos = body_end + (size & 1)

    if audio_format is None or channels is None or sample_rate is None or bits is None:
        msg = "WAV is missing its fmt chunk"
        raise AudioFormatError(msg)
    if raw is None:
        msg = "WAV is missing its data chunk"
        raise AudioFormatError(msg)

    fmt = AudioFormat(sample_rate=sample_rate, channels=channels)

    if audio_format == _FORMAT_PCM and bits == 16:
        return DecodedWav(pcm=raw, format=fmt)
    if audio_format == _FORMAT_IEEE_FLOAT and bits == 32:
        return DecodedWav(pcm=_float32_to_pcm16(raw), format=fmt)

    msg = f"unsupported WAV encoding: format={audio_format} bits={bits}"
    raise AudioFormatError(msg)


def encode_wav(pcm: bytes, fmt: AudioFormat) -> bytes:
    """把 int16 PCM 包成 WAV 容器。

    STT 后端（雨落版 ``/prompt_wav_recognition``）要的是**文件**，
    所以由 provider 用这个函数把它包起来。

    ``fmt.encoding`` 必然是 ``"pcm16"``——:class:`AudioFormat` 在构造时已经
    强制过，所以这里不再重复校验（那种检查永远不会触发）。
    """
    block_align = fmt.channels * BYTES_PER_SAMPLE
    byte_rate = fmt.sample_rate * block_align
    header = (
        _RIFF
        + (36 + len(pcm)).to_bytes(4, "little")
        + _WAVE
        + _CHUNK_FMT
        + (16).to_bytes(4, "little")
        + _FORMAT_PCM.to_bytes(2, "little")
        + fmt.channels.to_bytes(2, "little")
        + fmt.sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + _CHUNK_DATA
        + len(pcm).to_bytes(4, "little")
        + pcm
    )
    # RIFF 要求奇数长度的 data 后补一个填充字节
    return header + (b"\x00" if len(pcm) % 2 else b"")


def _float32_to_pcm16(raw: bytes) -> bytes:
    """IEEE float32 → int16，带削波。

    超出 [-1, 1] 的采样必须**削波**而不是回绕——回绕会把一个响亮的音节
    变成一声爆响。
    """
    samples = array("f")
    samples.frombytes(raw[: len(raw) - (len(raw) % 4)])
    if sys.byteorder == "big":  # pragma: no cover - 只在非 x86 上走到
        samples.byteswap()

    out = array("h", bytes(len(samples) * BYTES_PER_SAMPLE))
    for index, value in enumerate(samples):
        clipped = 1.0 if value > 1.0 else (-1.0 if value < -1.0 else value)
        out[index] = int(clipped * _INT16_MAX)
    if sys.byteorder == "big":  # pragma: no cover - 同上
        out.byteswap()

    return out.tobytes()
