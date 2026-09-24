"""音频的领域值对象（M4）。

纯数据 + 不变量，不依赖 httpx / fastapi / numpy。

**这里刻意只表示"已经解码成 PCM"的音频。** 容器（WAV 头）、浮点采样、
provider 私有的返回格式全部挡在 ``providers`` 层里——上层拿到的
:class:`AudioArtifact` 永远是 **int16 PCM**。理由：

- CosyVoice 雨落版实际返回的是 **32-bit float WAV**（实测 ``audioformat=3``），
  而线上协议用 int16。这个差异是 provider 的实现细节，不该漏到应用层。
- 将来换第三方 API（用户已明确可能这么做）时返回格式大概率又不一样，
  但端口形状与上层代码不用动。

**关于 STT 的 ``emotion_hint``**：`docs/07` §2 明文规定"STT 的 emotion hint 只能
作为辅助信号，不得直接覆写 AI 状态"。所以它在这里只是一个普通字段，
**没有任何自动接入状态系统的通路**——要用它必须在调用点显式写出来，
而调用点（``orchestrator``）刻意不写。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

#: 线上音频编码。M4 只有一种；新增编码（如 opus）要同时改协议、客户端与本文档。
AudioEncoding = Literal["pcm16"]

#: 受支持的编码集合。**运行时也要校验**——这个字段来自客户端在
#: ``input.audio.started`` 里声明的 JSON，静态类型管不到那一步。
SUPPORTED_ENCODINGS: tuple[str, ...] = ("pcm16",)

#: pcm16 = 16-bit signed little-endian。这是本模块所有字节的约定。
BYTES_PER_SAMPLE = 2

#: 合成语速的允许范围。与 ``domain/response.py`` 的 ``SPEED_RANGE`` 一致，
#: 但刻意不互相 import——两个领域概念（模型表达 vs 合成参数）不该绑在一起。
TTS_SPEED_RANGE = (0.5, 2.0)

#: 支持的声道数。单声道是常态；双声道保留给将来的立体声 TTS。
CHANNEL_CHOICES = (1, 2)


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """一段 PCM 音频的格式描述。

    这个对象会被序列化进 ``tts.started`` / ``input.audio.started`` 的 payload，
    所以字段名就是协议字段名。
    """

    sample_rate: int
    channels: int = 1
    encoding: AudioEncoding = "pcm16"

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            msg = f"audio sample_rate must be positive, got {self.sample_rate!r}"
            raise ValueError(msg)
        if self.channels not in CHANNEL_CHOICES:
            msg = f"audio channels must be one of {CHANNEL_CHOICES}, got {self.channels!r}"
            raise ValueError(msg)
        if self.encoding not in SUPPORTED_ENCODINGS:
            msg = f"unsupported audio encoding {self.encoding!r}; supported: {SUPPORTED_ENCODINGS}"
            raise ValueError(msg)

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * BYTES_PER_SAMPLE

    def duration_ms(self, byte_count: int) -> float:
        """给定字节数对应的时长（毫秒）。"""
        if byte_count <= 0:
            return 0.0
        return round(byte_count / self.bytes_per_second * 1000, 3)

    def to_dict(self) -> dict[str, object]:
        """序列化为协议字段（进事件 payload）。"""
        return {
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


@dataclass(frozen=True, slots=True)
class Transcript:
    """一次语音识别的结果。"""

    text: str
    language: str | None = None
    confidence: float | None = None
    #: STT 给出的情绪提示。**只作辅助信号，不得覆写 AI 状态**（`docs/07` §2）。
    emotion_hint: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            msg = f"transcript confidence must be within [0, 1], got {self.confidence!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TTSRequest:
    """一次合成请求。"""

    text: str
    voice_id: str
    speed: float = 1.0
    #: 来自 ``AssistantResponse.voice.style``。**M4 的 CosyVoice 后端不消费它**
    #: （见 ADR 0022）：``/rainfall_gen_instruct`` 的 ``instruct_text`` 期待
    #: "湖南话"这类中文指令，英文风格词（"warm"/"gentle"）的效果未经实测。
    style: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            msg = "tts text must not be blank"
            raise ValueError(msg)
        if not self.voice_id.strip():
            msg = "tts voice_id must not be blank"
            raise ValueError(msg)
        low, high = TTS_SPEED_RANGE
        if not math.isfinite(self.speed) or not low <= self.speed <= high:
            msg = f"tts speed must be within [{low}, {high}], got {self.speed!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AudioArtifact:
    """合成出来的音频。``pcm`` 永远是 int16 PCM（见模块文档字符串）。"""

    pcm: bytes
    format: AudioFormat

    def __post_init__(self) -> None:
        if not self.pcm:
            msg = "audio artifact must not be empty"
            raise ValueError(msg)

    @property
    def duration_ms(self) -> float:
        return self.format.duration_ms(len(self.pcm))
