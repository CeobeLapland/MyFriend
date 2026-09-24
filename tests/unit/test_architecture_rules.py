"""架构规则门禁（ADR 0008）。

用 AST 扫描 ``src/astra/domain`` 的所有 import，确保领域层不依赖任何框架或
外层实现。这是把 `docs/02_ARCHITECTURE.md` §5 的依赖方向规则

    允许：API -> Application -> Domain -> Infrastructure（实现端口）
    禁止：Domain -> FastAPI / Unity / LLM SDK / SQLite ORM

变成可执行检查的最便宜办法——不需要引入 import-linter 之类的额外依赖。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "astra"
_DOMAIN = _SRC / "domain"

#: 领域层禁止直接依赖的第三方模块
FORBIDDEN_THIRD_PARTY: frozenset[str] = frozenset(
    {
        "aiohttp",
        "alembic",
        "fastapi",
        "httpx",
        "pydantic",
        "pydantic_settings",
        "requests",
        "sqlalchemy",
        "sqlite3",
        "starlette",
        "uvicorn",
    }
)

#: 领域层禁止依赖的本项目外层包（否则会形成反向依赖或循环）
FORBIDDEN_ASTRA_PREFIXES: tuple[str, ...] = (
    "astra.api",
    "astra.application",
    "astra.infrastructure",
    "astra.providers",
    "astra.settings",
)

#: 必须通过端口访问外部服务、不得自己发 HTTP 的层（ADR 0010）。
#: 注意 ``api/app.py`` 是**装配点**，它 import infrastructure 是合法的，
#: 所以这里只禁 HTTP 客户端，不禁 astra.* 前缀。
_HTTP_CLIENT_FREE_LAYERS: tuple[str, ...] = ("domain", "application", "api")

#: application 层禁止反向依赖的包。api/infrastructure/providers 都在它外侧。
_FORBIDDEN_IN_APPLICATION: tuple[str, ...] = (
    "astra.api",
    "astra.infrastructure",
    "astra.providers",
)

FORBIDDEN_HTTP_CLIENTS: frozenset[str] = frozenset({"aiohttp", "httpx", "requests", "urllib3"})


def _layer_files(layer: str) -> list[Path]:
    return sorted((_SRC / layer).rglob("*.py"))


def _domain_files() -> list[Path]:
    return sorted(_DOMAIN.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """返回文件里所有绝对 import 的顶层模块名（忽略相对 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_domain_package_is_not_empty() -> None:
    assert _domain_files(), "domain 包不应为空——否则下面的门禁是空检查"


def test_checker_actually_detects_violations(tmp_path: Path) -> None:
    """自检：确保门禁本身有效，不是永远通过。"""
    sample = tmp_path / "sample.py"
    sample.write_text("import fastapi\nfrom astra.infrastructure.db import x\n", encoding="utf-8")

    imported = _imported_modules(sample)

    assert imported & FORBIDDEN_THIRD_PARTY == {"fastapi"}
    assert [m for m in imported if m.startswith(FORBIDDEN_ASTRA_PREFIXES)]


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_does_not_import_third_party_frameworks(path: Path) -> None:
    offenders = _imported_modules(path) & FORBIDDEN_THIRD_PARTY

    assert not offenders, (
        f"{path.relative_to(_SRC)} 依赖了领域层禁止的模块 {sorted(offenders)}；"
        "实现应放在 infrastructure / providers，领域层只定义 Protocol"
    )


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_does_not_import_outer_layers(path: Path) -> None:
    offenders = [m for m in _imported_modules(path) if m.startswith(FORBIDDEN_ASTRA_PREFIXES)]

    assert not offenders, (
        f"{path.relative_to(_SRC)} 反向依赖了外层包 {sorted(offenders)}；"
        "依赖方向必须保持 API -> Application -> Domain"
    )


_HTTP_GUARDED_FILES = [
    (layer, path) for layer in _HTTP_CLIENT_FREE_LAYERS for path in _layer_files(layer)
]


@pytest.mark.parametrize(
    "path",
    [path for _, path in _HTTP_GUARDED_FILES],
    ids=[f"{layer}/{path.name}" for layer, path in _HTTP_GUARDED_FILES],
)
def test_only_providers_may_import_http_clients(path: Path) -> None:
    """HTTP 客户端只允许出现在 ``astra/providers/**``（ADR 0010）。

    否则「provider 可替换」会在几个月后悄悄失效——某处直接 import httpx
    发请求，换后端时就漏掉了。
    """
    offenders = _imported_modules(path) & FORBIDDEN_HTTP_CLIENTS

    assert not offenders, (
        f"{path.relative_to(_SRC)} 直接依赖了 HTTP 客户端 {sorted(offenders)}；"
        "外部服务必须经由 domain 端口 + providers 适配器访问"
    )


@pytest.mark.parametrize("path", _layer_files("application"), ids=lambda p: p.name)
def test_application_does_not_import_outer_layers(path: Path) -> None:
    """application 层只能依赖 domain，实现由装配点注入。"""
    offenders = [m for m in _imported_modules(path) if m.startswith(_FORBIDDEN_IN_APPLICATION)]

    assert not offenders, (
        f"{path.relative_to(_SRC)} 依赖了外层包 {sorted(offenders)}；"
        "application 只应依赖 domain 的端口与值对象"
    )
