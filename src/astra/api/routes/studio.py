"""``GET /studio`` —— Studio 产品界面的入口页。

**这是应用本体，不是开发工具**（对比 ``routes/dev.py``）：界面上是对话、组件状态、
角色状态与记忆管理，用户日常就用它。

- 零构建、零 npm、零新增 Python 依赖：一个 HTML 文件 + 原生 JS（ADR 0017 / ADR 0025）。
- **无条件挂载**（ADR 0025 §5）：不受 ``ASTRA_ENV`` 门控，设成 ``prod`` 也照常可用。
  安全边界靠默认绑定 ``127.0.0.1``（ADR 0002 单进程本地），与 ``/dev`` 的规则**相反**
  ——这条差异由 ``tests/integration/test_studio_console.py`` 显式钉住。
- ``include_in_schema=False``：它不是对外 API 的一部分。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

_STUDIO_HTML = Path(__file__).resolve().parent.parent / "static" / "studio.html"

router = APIRouter(tags=["studio"])


@router.get("/studio", include_in_schema=False)
async def studio_console() -> FileResponse:
    return FileResponse(_STUDIO_HTML, media_type="text/html; charset=utf-8")
