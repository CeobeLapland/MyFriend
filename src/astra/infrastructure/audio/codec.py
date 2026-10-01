"""音频字节 ↔ int16 PCM 的编解码。

**为什么手写 WAV 而不是用 stdlib ``wave``**：CosyVoice 雨落版的 TTS 返回的是
**32-bit IEEE float WAV**（实测 ``audioformat=3``），而 stdlib ``wave``
**不支持浮点**——``wave.open`` 会直接抛 ``Error('unknown format: 3')``（实测）。
所以必须自己走一遍 RIFF 分块。

**为什么不用 ``audioop``**：它在 Python 3.13 已被移除。虽然本项目跑在 3.12 上、
现在还能 import，但新代码不该依赖一个正在消失的模块。用 ``array`` 手工转换。

对外只有两个入口：**解码成 int16 PCM**（:func:`decode_wav` / :func:`decode_audio`）
与 **把 int16 PCM 包成 WAV**（:func:`encode_wav`）。浮点、容器、字节序的差异
全部止步于此——上层永远拿 int16。

**压缩格式（第三方 TTS）**：第三方 TTS 常返回 mp3 / opus，WAV 解析器读不了，
所以 :func:`decode_audio` 在 RIFF 之外用 ``miniaudio`` 兜底（它自带单头解码器，
不需要外部 ffmpeg 二进制）。
"""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass

from astra.domain.audio import BYTES_PER_SAMPLE, CHANNEL_CHOICES, AudioFormat
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
class DecodedAudio:
    """解码结果：int16 PCM + 它自己的格式。"""

    pcm: bytes
    format: AudioFormat


def decode_wav(data: bytes) -> DecodedAudio:
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
        return DecodedAudio(pcm=raw, format=fmt)
    if audio_format == _FORMAT_IEEE_FLOAT and bits == 32:
        return DecodedAudio(pcm=_float32_to_pcm16(raw), format=fmt)

    msg = f"unsupported WAV encoding: format={audio_format} bits={bits}"
    raise AudioFormatError(msg)


def decode_audio(data: bytes) -> DecodedAudio:
    """解码一段**带容器**的音频字节（WAV 或 mp3），返回 int16 PCM。

    **按内容分派，不按声明**：先看是不是 ``RIFF``——WAV 是自描述的，走上面那条
    零依赖的手写解析；否则交给 :func:`_decode_mp3`。这样"厂商忽略我们请求的
    ``response_format``、照样回 mp3"这种常见情况也能正常工作。

    ``pcm``（裸流）不在这里处理——它没有采样率可读，得由调用方按后端约定补上。

    Raises:
        AudioFormatError: 既不是合法 WAV，也不是能解出来的 mp3。
    """
    if data[:4] == _RIFF:
        return decode_wav(data)
    return _decode_mp3(data)


def _decode_mp3(data: bytes) -> DecodedAudio:
    """用 ``miniaudio`` 解 mp3。

    **必须先用 ``mp3_get_info`` 读源格式再解码**：``miniaudio.decode`` 的默认参数是
    "44100 Hz 立体声"，直接用会把 24 kHz 单声道**重采样 + 升混**（实测 5760 个采样
    变成 23040 个），既浪费带宽又改变音质。把源采样率透传过去才是无损搬运。

    **延迟 import**：``codec`` 被 STT 与 TTS 两条路共用，而只有"后端返回了 mp3"
    这一种情况才需要这个依赖——提到模块顶层会让只用本地 WAV 后端的用户也付出
    导入成本。上面那条 ``_RIFF`` 嗅探已经分走了 WAV 流量，这里是真正的冷路径。
    """
    try:
        import miniaudio
    except ImportError as exc:  # pragma: no cover - 依赖已声明，只在装坏了时走到
        msg = "decoding mp3 audio requires the 'miniaudio' package"
        raise AudioFormatError(msg) from exc

    try:
        info = miniaudio.mp3_get_info(data)
        channels = int(info.nchannels)
        if channels not in CHANNEL_CHOICES:
            msg = f"unsupported channel count {channels}; supported: {CHANNEL_CHOICES}"
            raise AudioFormatError(msg)
        decoded = miniaudio.decode(data, nchannels=channels, sample_rate=int(info.sample_rate))
    except miniaudio.DecodeError as exc:
        msg = "the audio is neither WAV nor a decodable mp3"
        raise AudioFormatError(msg, detail=str(exc)) from exc

    # miniaudio 默认输出 SIGNED16，字节序是**本机序**（与 _float32_to_pcm16 同）
    samples = array("h", decoded.samples)
    if sys.byteorder == "big":  # pragma: no cover - 只在非 x86 上走到
        samples.byteswap()

    return DecodedAudio(
        pcm=samples.tobytes(),
        format=AudioFormat(sample_rate=int(info.sample_rate), channels=channels),
    )


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
