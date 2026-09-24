"""CosyVoice 雨落版的 TTS 实现（``TTSProvider`` 端口）。

两条路径，按**有没有配参考音频**选：

| 配置 | 端点 | 说明 |
|---|---|---|
| 配了 ``ASTRA_TTS_REFERENCE_AUDIO`` | ``/rainfall_gen_zero_shot`` | 用那段音频克隆音色（推荐） |
| 没配 | ``/rainfall_gen_sft`` | 用预置音色 ``tts_voice`` |

**为什么推荐配参考音频**：M4 真机验收实测，雨落版的预置音色「开拓者」合成出来
像乱码，而 zero-shot 对同一段文本效果正常。同一条管道、同一个 WAV 解码、
同一套推流，只有端点不同——所以问题在那个预置音色本身（用户单独用整合包时也复现过）。

zero-shot 需要参考音频**对应的文本**，这个由 ``/prompt_wav_recognition``
自动识别，不用手打。上传与识别只在**首次合成时做一次**并缓存——
Gradio 进程重启会让缓存的服务端路径失效，那时重启本服务即可。

**输出必须转成 int16 PCM**：雨落版返回的是 **32-bit IEEE float WAV**
（实测 ``audioformat=3``，24000 Hz 单声道），直接推给客户端会让体积翻倍，
而且浏览器 / Unity 端还要自己处理浮点。转换在 ``infrastructure.audio.codec`` 里做，
上层永远只见 int16（见 ``domain/audio.py`` 的模块文档字符串）。
"""

from __future__ import annotations

import asyncio
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
    extract_text,
)

#: 预置音色合成（不需要参考音频）
_SFT_API = "rainfall_gen_sft"

#: zero-shot 音色克隆（需要一段 ≤30 s 的参考音频）
_ZERO_SHOT_API = "rainfall_gen_zero_shot"

#: 识别参考音频对应的文本（zero-shot 的 ``prompt_text`` 参数）
_RECOGNISE_API = "prompt_wav_recognition"

#: 雨落版要求一个可写的输出目录，但我们**不用它取音频**——
#: 真正取字节走的是 Gradio 自己的文件端点（``ref.url``）。所以放临时目录，
#: 下载完就删，不把机器专属的绝对路径写进配置。
_OUTPUT_SUBDIR = "astra-cosyvoice"


class CosyVoiceTTSProvider:
    """通过 Gradio 合成语音；配了参考音频就走 zero-shot 克隆。"""

    def __init__(
        self,
        *,
        base_url: str,
        voice_id: str,
        timeout_s: float,
        reference_audio: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = GradioCallClient(base_url=base_url, timeout_s=timeout_s, transport=transport)
        self._voice_id = voice_id
        self._reference_audio = reference_audio
        #: 懒加载缓存 ``(Gradio 服务端路径, 参考文本)``
        self._reference: tuple[str, str] | None = None

    async def synthesize(self, request: TTSRequest) -> AudioArtifact:
        output_dir = _ensure_output_dir()
        name = f"astra-{new_id()}"

        try:
            api_name, data = await self._build_call(request, output_dir, name)
            result = await self._client.call(api_name, data)
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

    # ── 内部 ────────────────────────────────────────────

    async def _build_call(
        self, request: TTSRequest, output_dir: Path, name: str
    ) -> tuple[str, list[object]]:
        """按配置决定调哪个端点，并组装它的**位置参数**。

        Gradio 的 ``data`` 是位置参数，顺序错一个就会拿文本当音色名——所以这里
        显式列出来，不靠记忆。
        """
        if self._reference_audio is None:
            return _SFT_API, [
                request.text,
                request.voice_id,
                request.speed,
                str(output_dir),
                name,
                "wav",
            ]

        prompt_path, prompt_text = await self._ensure_reference()
        return _ZERO_SHOT_API, [
            request.text,
            {"path": prompt_path, "meta": {"_type": "gradio.FileData"}},
            prompt_text,
            request.speed,
            str(output_dir),
            name,
            "wav",
        ]

    async def _ensure_reference(self) -> tuple[str, str]:
        """上传参考音频并识别它的文本。**只做一次**，之后走缓存。"""
        if self._reference is not None:
            return self._reference

        path = self._reference_audio
        assert path is not None  # noqa: S101 - 调用方已经判过 None

        try:
            # 读盘是阻塞的，别卡住事件循环
            content = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            msg = f"cannot read the tts reference audio: {path}"
            raise TTSUnavailableError(msg, detail=str(exc)) from exc

        try:
            uploaded = await self._client.upload(path.name, content, content_type="audio/wav")
            result = await self._client.call(
                _RECOGNISE_API,
                [{"path": uploaded, "meta": {"_type": "gradio.FileData"}}],
            )
            prompt_text = extract_text(result)
        except GradioCallError as exc:
            raise TTSUnavailableError(
                "failed to prepare the reference voice", detail=str(exc)
            ) from exc

        if not prompt_text:
            msg = "the tts reference audio produced an empty transcript"
            raise TTSUnavailableError(msg)

        self._reference = (uploaded, prompt_text)
        return self._reference


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
