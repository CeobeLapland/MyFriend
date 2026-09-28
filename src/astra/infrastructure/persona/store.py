"""人格文件（``personas/<id>.toml``）的读写（ADR 0029）。

- 一位人格 = 人格目录下的一个 ``<id>.toml``；目录 = ``ASTRA_PERSONA_PATH`` 的父目录
  （约定，见 ADR 0029 §2——不新增配置键）。
- **同步实现**：路由侧用 ``asyncio.to_thread`` 调用（与 SQLite 仓储同一模式）。
- 保存 = **整份重写**（:mod:`astra.infrastructure.persona.toml_writer` 的规范形态），
  旧文件先备份成 ``<id>.toml.bak``；注释不保留（ADR 0029 的明确取舍）。
- **只删文件**：该 id 名下的会话 / 记忆 / 状态在数据库里，不归这里管。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from astra.infrastructure.persona.toml_loader import (
    PersonaDocument,
    PersonaLoadError,
    read_document,
)
from astra.infrastructure.persona.toml_writer import render_persona

#: 人格 id：小写字母开头，后跟小写字母 / 数字 / 下划线 / 连字符，最长 32 位。
#: id 会变成文件名与 ``companion_id``，所以必须严格。
_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
_ID_RE = re.compile(_ID_PATTERN)


class InvalidPersonaIdError(ValueError):
    """id 不合法。"""


def validate_persona_id(persona_id: str) -> str:
    """校验并返回 id；不合法抛 :class:`InvalidPersonaIdError`。"""
    if not _ID_RE.match(persona_id):
        msg = f"persona id must match {_ID_PATTERN}: {persona_id!r}"
        raise InvalidPersonaIdError(msg)
    return persona_id


@dataclass(frozen=True, slots=True)
class PersonaEntry:
    """人格列表里的一行。"""

    id: str
    path: Path
    #: 读不出来时为 ``None``（``error`` 里写清原因）
    display_name: str | None
    #: 解析 / id 非法时的原因。**列表不能因为一个坏文件整体失败**——
    #: 用户要靠它看到"哪个文件坏了、坏在哪"。
    error: str | None = None


class PersonaStore:
    """:class:`Path` 目录里的人格文件仓库。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        """人格目录。Studio 会显示它——用户要知道文件究竟在哪。"""
        return self._directory

    def path_for(self, persona_id: str) -> Path:
        """该 id 对应的文件路径（id 非法时抛 :class:`InvalidPersonaIdError`）。"""
        return self._directory / f"{validate_persona_id(persona_id)}.toml"

    def exists(self, persona_id: str) -> bool:
        return self.path_for(persona_id).is_file()

    def list(self) -> tuple[PersonaEntry, ...]:
        """列出目录里的全部人格（按 id 排序）。

        ``*.toml.bak`` 不会被 ``*.toml`` 匹配到，所以备份不会出现在列表里。
        """
        if not self._directory.is_dir():
            return ()

        entries: list[PersonaEntry] = []
        for path in sorted(self._directory.glob("*.toml")):
            stem = path.stem
            if not _ID_RE.match(stem):
                entries.append(
                    PersonaEntry(
                        id=stem,
                        path=path,
                        display_name=None,
                        error=f"persona id must match {_ID_PATTERN}: {stem!r}",
                    )
                )
                continue
            try:
                document = read_document(path)
            except PersonaLoadError as exc:
                entries.append(PersonaEntry(id=stem, path=path, display_name=None, error=str(exc)))
                continue
            entries.append(
                PersonaEntry(id=stem, path=path, display_name=document.identity.display_name)
            )
        return tuple(entries)

    def read(self, persona_id: str) -> PersonaDocument:
        """读取一份人格文档。

        Raises:
            PersonaLoadError: 文件不存在 / 不可读 / 非法。
        """
        return read_document(self.path_for(persona_id))

    def create(self, persona_id: str, document: PersonaDocument) -> Path:
        """新建一份人格文件。

        Raises:
            FileExistsError: 该 id 已经有文件——**不覆盖**（改走 :meth:`write`）。
        """
        path = self.path_for(persona_id)
        if path.exists():
            raise FileExistsError(f"persona already exists: {persona_id}")
        return self._write(path, document)

    def write(self, persona_id: str, document: PersonaDocument) -> Path:
        """整份重写该人格；文件已存在时先备份成 ``<id>.toml.bak``。"""
        path = self.path_for(persona_id)
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak"))
        return self._write(path, document)

    def delete(self, persona_id: str) -> bool:
        """**只删这个文件**。返回文件是否存在过。"""
        path = self.path_for(persona_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def _write(self, path: Path, document: PersonaDocument) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        path.write_text(render_persona(document), encoding="utf-8")
        return path
