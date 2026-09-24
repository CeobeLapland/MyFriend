"""ASGI 入口。

uvicorn 的目标：``astra.main:app``
"""

from __future__ import annotations

from astra.api.app import create_app
from astra.settings import get_settings

app = create_app(get_settings())
