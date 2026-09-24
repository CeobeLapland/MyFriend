"""能量阈值 VAD（voice activity detection）。

**这是占位实现，不要把它当成正经 VAD。** 它只做两件事：

1. 按帧算归一化 RMS；
2. 在"已经出现过语音"之后，连续静音超过阈值就判定"说完了"。

它**没有**降噪、没有自适应噪声门限、没有频谱特征、没有 hangover。
嘈杂环境里会误判——所以它是**可关闭的**，而且客户端 push-to-talk
（``input.audio.stopped``）**优先于它**：那个信号来自用户的真实意图，
比任何能量检测都可靠。

`docs/07` §1 只要求管线里有 VAD 这一环，M4 按最低可用标准交付。
"""

from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass

from astra.domain.audio import BYTES_PER_SAMPLE

#: int16 的满量程，用来把 RMS 归一化到 0..1
_INT16_SCALE = 32768.0


@dataclass(frozen=True, slots=True)
class VadConfig:
    """VAD 参数（来自 ``Settings``）。"""

    enabled: bool = True
    #: 连续静音超过它就判定"说完了"
    silence_ms: int = 800
    #: 归一化 RMS 阈值（0..1）
    energy_threshold: float = 0.01


class EnergyVad:
    """一个 utterance 一个实例。**不是线程安全的**，也不需要是。

    典型用法：

    ```python
    vad = EnergyVad(config, sample_rate=16000, channels=1)
    for chunk in incoming_pcm_chunks:
        if vad.feed(chunk):
            ...  # 用户说完了
    ```
    """

    def __init__(self, config: VadConfig, *, sample_rate: int, channels: int = 1) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._channels = channels
        self._has_speech = False
        self._silence_ms = 0.0

    @property
    def has_speech(self) -> bool:
        """这段 utterance 里是否至少有一帧超过能量阈值。

        调用方用它决定"要不要送去识别"——全程静音不必浪费一次 STT。
        """
        return self._has_speech

    @property
    def trailing_silence_ms(self) -> float:
        return self._silence_ms

    def reset(self) -> None:
        self._has_speech = False
        self._silence_ms = 0.0

    def feed(self, pcm: bytes) -> bool:
        """喂一帧 PCM，返回"是否判定说完了"。

        **关闭时仍然统计能量**，只是不返回"说完了"。这一点很重要：
        ``has_speech`` 是调用方用来决定"要不要送去识别"的依据——
        如果关闭 VAD 就把它一起关掉，所有语音都会被当成"全程静音"丢掉
        （M4 集成测试真的抓到过这个 bug）。
        """
        samples = _to_samples(pcm)
        if not samples:
            return False

        frame_ms = (len(samples) / self._channels) / self._sample_rate * 1000.0

        if _normalized_rms(samples) >= self._config.energy_threshold:
            self._has_speech = True
            self._silence_ms = 0.0
            return False

        self._silence_ms += frame_ms
        if not self._config.enabled:
            return False

        # 没有出现过语音就永远不结束：否则一段环境噪声静音会把空 utterance
        # 反复判成"说完了"，调用方会收到一串空转写。
        return self._has_speech and self._silence_ms >= self._config.silence_ms


def _to_samples(pcm: bytes) -> array[int]:
    samples: array[int] = array("h")
    # 末尾可能是半个采样（客户端 bug / 分包），丢掉而不是抛异常
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)])
    if sys.byteorder == "big":  # pragma: no cover - 只在非 x86 上走到
        samples.byteswap()
    return samples


def _normalized_rms(samples: array[int]) -> float:
    """归一化 RMS（0..1）。空输入为 0。"""
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples)) / _INT16_SCALE
