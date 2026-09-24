"""``astra.infrastructure.audio.vad`` 的测试。

VAD 是**占位实现**，测试锁住的是它的**契约**而不是"它识别得多准"：

- 全程静音**永远不**判"说完了"（否则会空转写）；
- 出现过语音之后，静音达到阈值才判"说完了"；
- 关闭时恒为 False（只剩 push-to-talk 一条结束路径）。
"""

from __future__ import annotations

import struct

import pytest

from astra.infrastructure.audio.vad import EnergyVad, VadConfig

SAMPLE_RATE = 16000


def _pcm(sample_count: int, value: int) -> bytes:
    return struct.pack(f"<{sample_count}h", *([value] * sample_count))


def _silence(ms: int) -> bytes:
    return _pcm(SAMPLE_RATE * ms // 1000, 0)


def _speech(ms: int, amplitude: int = 8000) -> bytes:
    return _pcm(SAMPLE_RATE * ms // 1000, amplitude)


def _vad(*, enabled: bool = True, silence_ms: int = 800, threshold: float = 0.01) -> EnergyVad:
    return EnergyVad(
        VadConfig(enabled=enabled, silence_ms=silence_ms, energy_threshold=threshold),
        sample_rate=SAMPLE_RATE,
        channels=1,
    )


def test_pure_silence_never_finishes_the_utterance() -> None:
    """关键：没有语音就没有"说完了"。

    否则一段环境噪声静音会让空 utterance 反复触发，调用方收到一串空转写。
    """
    vad = _vad(silence_ms=200)

    assert [vad.feed(_silence(100)) for _ in range(10)] == [False] * 10
    assert vad.has_speech is False


def test_speech_then_long_silence_finishes_the_utterance() -> None:
    vad = _vad(silence_ms=300)

    assert vad.feed(_speech(200)) is False
    assert vad.has_speech is True
    assert vad.feed(_silence(200)) is False
    assert vad.feed(_silence(200)) is True


def test_short_pause_between_words_does_not_finish_the_utterance() -> None:
    """说话中间的自然停顿不该被当成"说完了"。"""
    vad = _vad(silence_ms=500)

    assert vad.feed(_speech(200)) is False
    assert vad.feed(_silence(100)) is False
    assert vad.feed(_speech(200)) is False

    assert vad.trailing_silence_ms == 0.0


def test_quiet_frame_below_threshold_counts_as_silence() -> None:
    """阈值是可调的——低幅度噪声不该被当成语音。"""
    vad = _vad(silence_ms=100, threshold=0.5)

    assert vad.feed(_pcm(1600, 1000)) is False
    assert vad.has_speech is False


def test_disabled_vad_never_finishes_but_still_tracks_speech() -> None:
    """关闭只关掉"自动结束"，**不能**把"有没有语音"一起关掉。

    后者是调用方判断"要不要送去识别"的依据；一起关掉的话，所有语音都会被
    当成"全程静音"丢掉。M4 的集成测试真的抓到过这个 bug。
    """
    vad = _vad(enabled=False, silence_ms=1)

    assert vad.feed(_speech(100)) is False
    assert vad.has_speech is True
    assert vad.feed(_silence(5000)) is False


def test_reset_clears_both_speech_flag_and_silence_counter() -> None:
    vad = _vad(silence_ms=300)
    vad.feed(_speech(200))
    vad.feed(_silence(200))

    vad.reset()

    assert vad.has_speech is False
    assert vad.trailing_silence_ms == 0.0


def test_empty_and_odd_length_frames_are_tolerated() -> None:
    """客户端分包可能送来半个采样；丢尾字节而不是抛异常。"""
    vad = _vad()

    assert vad.feed(b"") is False
    assert vad.feed(b"\x00") is False


@pytest.mark.parametrize("channels", [1, 2])
def test_frame_duration_accounts_for_channel_count(channels: int) -> None:
    """立体声一帧的采样数是单声道的两倍，时长必须按"每声道采样数"算。

    算错会让静音计时走一半/一倍，VAD 提前或延后触发。
    """
    vad = EnergyVad(
        VadConfig(enabled=True, silence_ms=300, energy_threshold=0.01),
        sample_rate=SAMPLE_RATE,
        channels=channels,
    )
    speech = _pcm(SAMPLE_RATE * 200 // 1000 * channels, 8000)
    silence = _pcm(SAMPLE_RATE * 200 // 1000 * channels, 0)

    assert vad.feed(speech) is False
    assert vad.feed(silence) is False
    assert vad.feed(silence) is True
