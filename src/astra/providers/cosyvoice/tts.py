"""CosyVoice 雨落版的 TTS 实现（``TTSProvider`` 端口）。

走 ``/rainfall_gen_sft``：**预训练音色**，不需要参考音频。
（``/rainfall_gen_zero_shot`` 才需要一段 ≤30 s 的音色样本。）

**输出必须转成 int16 PCM**：雨落版返回的是 **32-bit IEEE float WAV**
（实测 ``audioformat=3``，24000 Hz 单声道），直接推给客户端会让体积翻倍，
而且浏览器 / Unity 端还要自己处理浮点。转换在 ``infrastructure.audio.codec`` 里做，
上层永远只见 int16（见 ``domain/audio.py`` 的模块文档字符串）。
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

import httpx

from astra.domain.audio import AudioArtifact, TTSRequest
from astra.domain.errors import AudioFormatError, TTSUnavailableError
from astra.domain.ids import new_id
from astra.infrastructure.audio.codec import decode_wav
from astra.providers.cosyvoice.client import (
    GradioCallClient,
    GradioCallError,
    GradioFileRef,
)

#: 雨落版的预训练音色合成端点
_SFT_API = "rainfall_gen_sft"

#: 雨落版要求一个可写的输出目录，但我们**不用它取音频**——
#: 真正取字节走的是 Gradio 自己的文件端点（``ref.url``）。所以放临时目录，
#: 下载完就删，不把机器专属的绝对路径写进配置。
_OUTPUT_SUBDIR = "astra-cosyvoice"


class CosyVoiceTTSProvider:
    """通过 Gradio 的 ``/rainfall_gen_sft`` 合成语音。"""

    def __init__(
        self,
        *,
        base_url: str,
        voice_id: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = GradioCallClient(base_url=base_url, timeout_s=timeout_s, transport=transport)
        self._voice_id = voice_id

    async def synthesize(self, request: TTSRequest) -> AudioArtifact:
        output_dir = _ensure_output_dir()
        name = f"astra-{new_id()}"

        try:
            result = await self._client.call(
                _SFT_API,
                [
                    request.text,
                    request.voice_id,
                    request.speed,
                    str(output_dir),
                    name,
                    "wav",
                ],
            )
            ref = GradioFileRef.from_payload(_first(result))
            raw = await self._client.download(ref.url)
        except GradioCallError as exc:
            raise TTSUnavailableError("tts backend failed", detail=str(exc)) from exc
        finally:
            # 雨落版另存的那一份是纯垃圾，删掉免得临时目录无限增长。
            # 删失败无所谓，不该影响已经拿到的音频。
            _discard(output_dir / f"{name}.wav")

        try:
            decoded = decode_wav(raw)
        except AudioFormatError as exc:
            raise TTSUnavailableError(
                "tts backend returned undecodable audio", detail=str(exc)
            ) from exc

        return AudioArtifact(pcm=decoded.pcm, format=decoded.format)


def _first(result: list[object]) -> object:
    if not result:
        msg = "tts backend returned an empty output list"
        raise GradioCallError(msg)
    return result[0]


def _ensure_output_dir() -> Path:
    path = Path(tempfile.gettempdir()) / _OUTPUT_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
