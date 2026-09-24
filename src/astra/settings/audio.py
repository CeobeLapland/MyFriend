"""音频子系统配置（M4）。

见 ``settings/base.py`` 的模块文档字符串：这是 mixin，不是独立 BaseSettings。

**关于显存（8 GB 硬约束）**：M4 的实测结论是 LLM 与 TTS 无法同时常驻 GPU 且都快
（LLM 满 GPU 时 TTS 从 2–4 s/句 劣化到 10–30 s/句）。采用的策略是
**让 LM Studio 把 LLM 放在 CPU 上**（``lms load <model> --gpu off``），
把 GPU 整个让给音频模型。

**这条策略是环境配置，不是代码能保证的**——代码侧不做模型生命周期管理
（那会耦合 LM Studio 专有 API，与 ADR 0001 的 provider-agnostic 原则冲突）。
所以忘记 ``--gpu off`` 时，TTS 会慢到不可用，而代码不会报错。
见 ADR 0022 与 README 的"已知限制"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from astra.settings.base import blank_to_none

AudioProviderName = Literal["none", "cosyvoice"]


class AudioSettingsMixin:
    """STT / TTS provider、音频格式与 VAD 参数。"""

    # ── STT / TTS provider ──────────────────────────────
    # none = 不接入（/health 诚实上报 not_configured，文本路径完全不受影响）
    # cosyvoice = CosyVoice3 雨落版整合包（Gradio 应用，见 ADR 0009/0022）
    stt_provider: AudioProviderName = "none"
    tts_provider: AudioProviderName = "none"
    #: 雨落版 Gradio 应用的地址。STT 与 TTS 是同一个进程，所以共用一个 URL。
    tts_base_url: str = "http://127.0.0.1:7860"
    #: `/rainfall_gen_sft` 的预训练音色名。雨落版当前只列出 "开拓者"。
    #: **实测这个预置音色合成出来是乱码**（见下方 `tts_reference_audio`），
    #: 所以它只在没配参考音频时才会被用到。
    tts_voice: str = "开拓者"
    #: **zero-shot 音色克隆的参考音频**（≤30 秒的干净人声，单声道 wav 最好）。
    #:
    #: 配了它就走 `/rainfall_gen_zero_shot`（每轮用这段音频克隆音色）；
    #: 留空则退回 `/rainfall_gen_sft` 的预置音色。
    #:
    #: M4 真机验收实测：博主整合包的预置音色「开拓者」念出来像乱码，
    #: 而 zero-shot 同一段文本效果正常——所以**推荐配一个参考音频**。
    #: 参考音频对应的文本由 `/prompt_wav_recognition` 自动识别，不用手打。
    tts_reference_audio: Path | None = None
    #: 首次调用含模型加载（实测 TTS 冷启 ~5 s、STT 冷启 ~4.9 s），别设太紧。
    tts_timeout_s: float = Field(default=120.0, gt=0)
    stt_timeout_s: float = Field(default=120.0, gt=0)

    # ── 音频格式 ────────────────────────────────────────
    #: 客户端采集格式。16 kHz 单声道 int16 是 SenseVoice 的输入惯例。
    #: 服务端按客户端在 `input.audio.started` 里声明的格式接收，
    #: 与这两个值不符则回 `error.protocol`（不做服务端重采样）。
    audio_input_sample_rate: int = Field(default=16000, gt=0)
    audio_input_channels: int = Field(default=1, ge=1, le=2)
    #: 单个 WS 二进制帧的字节上限。防止客户端 bug 一帧塞进整个录音把内存吃满。
    audio_chunk_max_bytes: int = Field(default=65536, ge=1024)
    #: 一次 utterance 的时长上限；超过就截断并记日志（同样是防呆，不是功能）。
    audio_max_utterance_seconds: float = Field(default=60.0, gt=0)

    # ── VAD ─────────────────────────────────────────────
    #: **占位实现**：固定阈值的 RMS 能量检测 + 静音超时自动结束。
    #: 没有降噪、没有自适应噪声门限。客户端 push-to-talk 松手优先于它。
    #:
    #: **默认关闭**——这是 M4 真机验收打回来的一处默认值。实测里
    #: "你好呀，今天过得怎么样？"这句话在逗号处停顿时就被 800 ms 的静音阈值
    #: 判成"说完了"，只送了 1.1 秒音频给 STT，转写直接变成乱码。
    #: 既然 M4 的输入路径本来就是 push-to-talk（客户端一定会发
    #: ``input.audio.stopped``），开着它只会**主动截断**正常对话。
    #: 想做"免按键"再说，届时它需要的是正经 VAD 而不是调大这个阈值。
    vad_enabled: bool = False
    #: 连续静音超过它就判定"说完了"。
    vad_silence_ms: int = Field(default=800, ge=100)
    #: 归一化 RMS 阈值（0..1）。环境嘈杂时可调高。
    vad_energy_threshold: float = Field(default=0.01, ge=0.0, le=1.0)

    @field_validator("tts_reference_audio", mode="before")
    @classmethod
    def _blank_reference_to_none(cls, value: object) -> object:
        """``ASTRA_TTS_REFERENCE_AUDIO=`` 表示"不启用 zero-shot"，不是"当前目录"。"""
        return blank_to_none(value)
