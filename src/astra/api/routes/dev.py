"""``GET /dev`` —— 开发者控制台。

**这是开发工具，不是产品 UI。** 它展示的是协议层与运行状态（原始事件流、health、
词表、stub/重试/耗时），与将来的产品 UI（角色形象、对话气泡、设置页）是**两个
不同的东西**——即使产品 UI 做出来了，这个控制台依然有用。

- 零构建、零 npm、零新增 Python 依赖：一个 HTML 文件 + 原生 JS。
- **只在 ``ASTRA_ENV=dev``（默认）时挂载**。把 ``ASTRA_ENV`` 设成 ``prod``
  或把服务绑到 ``0.0.0.0`` 时，它不会出现在路由表里——不需要额外配置项。
- ``include_in_schema=False``：它不是对外 API 的一部分。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

_DEV_HTML = Path(__file__).resolve().parent.parent / "static" / "dev.html"

router = APIRouter(tags=["dev"])


@router.get("/dev", include_in_schema=False)
async def dev_console() -> FileResponse:
    return FileResponse(_DEV_HTML, media_type="text/html; charset=utf-8")
