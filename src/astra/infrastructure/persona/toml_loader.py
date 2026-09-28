"""从 TOML 文件加载人格配置（ADR 0015 / 0029）。

- 使用 stdlib ``tomllib``，零新增依赖。**必须以二进制模式打开**——
  ``tomllib.load`` 只接受二进制文件对象。
- 用 Pydantic 校验文件结构（infrastructure 层允许依赖 Pydantic），
  再转成 ``domain`` 的 frozen dataclass。
- 加载失败一律抛 :class:`PersonaLoadError`：人格是配置不是外部服务，
  缺文件等于应用没有身份，应当快速失败而不是降级启动。

**ADR 0029 起，``*Document`` 模型是公共的**：Studio 的人格页从 JSON 提交文档、
``PersonaStore`` 写回文件，两者都与这里**同一套校验**——校验规则只有一处，
不会出现"UI 放过了、启动时才炸"的漂移。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from astra.domain.persona import Communication, Identity, Persona

#: 性格倾向的取值范围
TraitValue = Annotated[float, Field(ge=0.0, le=1.0)]


class PersonaLoadError(RuntimeError):
    """人格文件缺失、不可读或结构非法。"""


class IdentityDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    pronouns: str
    self_description: str
    version: str = "1"
    backstory: str | None = None


class CommunicationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentence_length: str
    emoji_frequency: str
    rhetorical_questions: str
    preferred_interaction: str
    language: str = "zh-CN"


class BehaviorDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[str] = Field(default_factory=list)


class PromptDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_constraints: list[str] = Field(default_factory=list)


class ErrorsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_unavailable: str | None = None


class PersonaDocument(BaseModel):
    """一份人格文档——与 TOML 文件同构，也是 Studio 人格页编辑的对象。"""

    model_config = ConfigDict(extra="forbid")

    identity: IdentityDocument
    communication: CommunicationDocument
    traits: dict[str, TraitValue] = Field(default_factory=dict)
    behavior: BehaviorDocument = Field(default_factory=BehaviorDocument)
    prompt: PromptDocument = Field(default_factory=PromptDocument)
    errors: ErrorsDocument = Field(default_factory=ErrorsDocument)


def load_persona(path: Path) -> Persona:
    """读取并校验人格文件，转成领域对象。

    Raises:
        PersonaLoadError: 文件不存在 / 不可读 / 不是合法 TOML / 结构不合法。
    """
    return _to_domain(read_document(path))


def read_document(path: Path) -> PersonaDocument:
    """读取并校验一份人格文件（不转领域对象）。

    Raises:
        PersonaLoadError: 同上。
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        msg = f"persona file is not readable: {path}"
        raise PersonaLoadError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"persona file is not valid TOML: {path}: {exc}"
        raise PersonaLoadError(msg) from exc

    try:
        return PersonaDocument.model_validate(data)
    except ValidationError as exc:
        msg = f"persona file failed validation: {path}\n{exc}"
        raise PersonaLoadError(msg) from exc


def validate_document(data: Mapping[str, Any]) -> PersonaDocument:
    """校验一份人格文档（Studio 提交的 JSON 走这里，与文件加载同一套规则）。

    Raises:
        PersonaLoadError: 结构不合法；消息是压缩过的字段路径 + 原因，
            可以直接展示给用户。
    """
    try:
        return PersonaDocument.model_validate(data)
    except ValidationError as exc:
        msg = f"persona document failed validation: {validation_message(exc)}"
        raise PersonaLoadError(msg) from exc


def validation_message(exc: ValidationError) -> str:
    """把 pydantic 的错误压成 ``字段路径: 原因; …`` 的一行。

    **刻意不吐整块结构**：那是给程序看的，给用户看的要能一眼定位到哪个字段。
    """
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _to_domain(parsed: PersonaDocument) -> Persona:
    identity = parsed.identity
    communication = parsed.communication

    error_messages: dict[str, str] = {}
    if parsed.errors.llm_unavailable is not None:
        error_messages["llm_unavailable"] = parsed.errors.llm_unavailable

    return Persona(
        identity=Identity(
            name=identity.name,
            display_name=identity.display_name,
            pronouns=identity.pronouns,
            self_description=identity.self_description.strip(),
            version=identity.version,
            backstory=identity.backstory.strip() if identity.backstory else None,
        ),
        communication=Communication(
            sentence_length=communication.sentence_length,
            emoji_frequency=communication.emoji_frequency,
            rhetorical_questions=communication.rhetorical_questions,
            preferred_interaction=communication.preferred_interaction,
            language=communication.language,
        ),
        traits=dict(parsed.traits),
        behavior_rules=tuple(parsed.behavior.rules),
        hard_constraints=tuple(parsed.prompt.hard_constraints),
        error_messages=error_messages,
    )
