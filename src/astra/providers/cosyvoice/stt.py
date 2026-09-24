"""CosyVoice 雨落版的 STT 实现（``STTProvider`` 端口）。

走 ``/prompt_wav_recognition``（后端是 SenseVoiceSmall）。

与 TTS 的区别：这个端点收的是**文件**，所以要先上传。Gradio 的上传与调用是
两个不同的端点（``/gradio_api/upload`` 拿服务端路径 → 再把它当 ``filepath``
参数传进去），实测确认过。所以本模块比 ``tts.py`` 多一步 ``upload``。

**只处理单声道**：端口签名（``docs/07`` §2）只有 ``sample_rate``，没有声道数，
所以这里按单声道包 WAV。多声道在 WS 边界就被拒掉了（``audio_input_channels``）。
"""

from __future__ import annotations

import httpx

from astra.domain.audio import AudioFormat, Transcript
from astra.domain.errors import STTUnavailableError
from astra.infrastructure.audio.codec import encode_wav
from astra.providers.cosyvoice.client import GradioCallClient, GradioCallError

#: 雨落版的语音识别端点（SenseVoiceSmall）
_STT_API = "prompt_wav_recognition"


class CosyVoiceSTTProvider:
    """通过 Gradio 的 ``/prompt_wav_recognition`` 做语音识别。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = GradioCallClient(base_url=base_url, timeout_s=timeout_s, transport=transport)

    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        language_hint: str | None = None,
    ) -> Transcript:
        # ``language_hint`` 未被使用：SenseVoice 自己判语种，端点也不接受提示。
        # 参数保留是因为它是端口契约（docs/07 §2），将来换后端可能用得上。
        wav = encode_wav(audio, AudioFormat(sample_rate=sample_rate, channels=1))

        try:
            uploaded = await self._client.upload("utterance.wav", wav, content_type="audio/wav")
            result = await self._client.call(
                _STT_API,
                [{"path": uploaded, "meta": {"_type": "gradio.FileData"}}],
            )
        except GradioCallError as exc:
            raise STTUnavailableError("stt backend failed", detail=str(exc)) from exc

        return Transcript(text=_extract_text(result))


def _extract_text(result: list[object]) -> str:
    if not result:
        msg = "stt backend returned an empty output list"
        raise STTUnavailableError(msg)
    value = result[0]
    if not isinstance(value, str):
        msg = f"stt backend returned {type(value).__name__}, expected text"
        raise STTUnavailableError(msg)
    return value.strip()
