"""Gradio ``/gradio_api/call/*`` 的最小客户端。

调用序列（实测确认）：

```text
POST /gradio_api/call/{api_name}   {"data": [...]}      -> {"event_id": "..."}
GET  /gradio_api/call/{api_name}/{event_id}             -> SSE 流
     event: complete
     data: [<第一个输出>, ...]                            <- 我们要的就是它
GET  <结果里的 url>                                       -> 文件字节
```

**为什么用 ``asyncio.wait_for`` 兜总时长**：httpx 的 ``timeout=`` 是各阶段
共用的默认值，不是总预算。SSE 流可以一直不结束（后端卡住），
只有外层 wait_for 能保证"设了 120s 就真的 120s 结束"——与
``providers/lmstudio/client.py`` 同一处理。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx


class GradioCallError(RuntimeError):
    """Gradio 应用返回了 ``error`` 事件、响应不是预期结构，或流提前结束。

    这是**后端层面**的错误，由 ``stt`` / ``tts`` 翻译成对应的领域异常
    （``STTUnavailableError`` / ``TTSUnavailableError``），不直接漏到上层。
    """


@dataclass(frozen=True, slots=True)
class GradioFileRef:
    """Gradio 返回的文件引用。

    ``url`` 指向 Gradio 自己的文件端点，**同一个进程内就能下载**，
    所以不依赖"输出目录在本地磁盘上"这件事——这是能换远端后端的前提。
    """

    path: str
    url: str

    @classmethod
    def from_payload(cls, payload: object) -> GradioFileRef:
        if not isinstance(payload, dict) or "url" not in payload:
            msg = f"expected a file reference, got {payload!r}"
            raise GradioCallError(msg)
        return cls(path=str(payload.get("path", "")), url=str(payload["url"]))


class GradioCallClient:
    """一个 Gradio 应用的 call API 封装。线程安全（无状态）。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        #: 测试用：注入 ``httpx.MockTransport`` 以避免真实网络
        self._transport = transport

    async def call(self, api_name: str, data: Sequence[object]) -> list[object]:
        """调用一个端点并返回它的输出数组。

        Raises:
            GradioCallError: 超时、HTTP 错误、``error`` 事件、或流提前结束。
        """
        try:
            return await asyncio.wait_for(
                self._call_inner(api_name, data),
                timeout=self._timeout_s,
            )
        except TimeoutError as exc:
            msg = f"{api_name} exceeded its {self._timeout_s}s budget"
            raise GradioCallError(msg) from exc

    async def download(self, url: str) -> bytes:
        """下载结果文件（Gradio 的 ``/gradio_api/file=...`` 端点）。

        ``url`` 是绝对地址，httpx 会直接用它，不再拼 ``base_url``。
        """
        try:
            async with self._client() as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            msg = f"failed to download {url!r}: {exc}"
            raise GradioCallError(msg) from exc

    async def upload(
        self,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        """上传一个文件，返回 Gradio 服务端的路径（供 ``filepath`` 类参数使用）。

        这是 STT 那条路必须的：``/prompt_wav_recognition`` 收的是**文件**，
        而它的上传与下载是两个不同的端点（实测确认）。
        """
        try:
            async with self._client() as client:
                response = await client.post(
                    "/gradio_api/upload",
                    files={"files": (filename, content, content_type)},
                )
                response.raise_for_status()
                payload: object = response.json()
        except httpx.HTTPError as exc:
            msg = f"failed to upload {filename!r}: {exc}"
            raise GradioCallError(msg) from exc

        if not isinstance(payload, list) or not payload:
            msg = f"expected the upload to return a non-empty list, got {payload!r}"
            raise GradioCallError(msg)
        return str(payload[0])

    # ── 内部 ────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_s),
            transport=self._transport,
        )

    async def _call_inner(self, api_name: str, data: Sequence[object]) -> list[object]:
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/gradio_api/call/{api_name}", json={"data": list(data)}
                )
                response.raise_for_status()
                event_id = _extract_event_id(response.json())

                async with client.stream(
                    "GET", f"/gradio_api/call/{api_name}/{event_id}"
                ) as stream:
                    stream.raise_for_status()
                    async for event, payload in _iter_sse(stream.aiter_lines()):
                        if event == "complete":
                            return _parse_data(payload)
                        if event == "error":
                            msg = f"{api_name} reported an error: {payload}"
                            raise GradioCallError(msg)
        except httpx.HTTPError as exc:
            msg = f"{api_name} request failed: {exc}"
            raise GradioCallError(msg) from exc

        msg = f"{api_name} stream ended without a result"
        raise GradioCallError(msg)


async def _iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """把 SSE 行流解析成 ``(event, data)``。

    按 SSE 规范实现（而不是"读到 ``data:`` 就当一条"）：同一事件的多行
    ``data:`` 要用换行拼接，空行才是事件边界。Gradio 现在只发单行，
    但按规范写才不会在它哪天改格式时静默解析出半截 JSON。
    """
    event = "message"
    data_lines: list[str] = []

    async for line in lines:
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # 注释 / 心跳
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)

    if data_lines:
        yield event, "\n".join(data_lines)


def _extract_event_id(payload: object) -> str:
    if not isinstance(payload, dict) or "event_id" not in payload:
        msg = f"expected an event_id, got {payload!r}"
        raise GradioCallError(msg)
    return str(payload["event_id"])


def _parse_data(payload: str) -> list[object]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = f"result is not valid JSON: {payload[:120]!r}"
        raise GradioCallError(msg) from exc
    if not isinstance(parsed, list):
        msg = f"expected the result to be a list, got {type(parsed).__name__}"
        raise GradioCallError(msg)
    return parsed
