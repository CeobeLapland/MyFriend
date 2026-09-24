"""从 TOML 文件加载人格配置（ADR 0015）。

- 使用 stdlib ``tomllib``，零新增依赖。**必须以二进制模式打开**——
  ``tomllib.load`` 只接受二进制文件对象。
- 用 Pydantic 校验文件结构（infrastructure 层允许依赖 Pydantic），
  再转成 ``domain`` 的 frozen dataclass。
- 加载失败一律抛 :class:`PersonaLoadError`：人格是配置不是外部服务，
  缺文件等于应用没有身份，应当快速失败而不是降级启动。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from astra.domain.persona import Communication, Identity, Persona

#: 性格倾向的取值范围
TraitValue = Annotated[float, Field(ge=0.0, le=1.0)]


class PersonaLoadError(RuntimeError):
    """人格文件缺失、不可读或结构非法。"""


class _IdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    pronouns: str
    self_description: str
    version: str = "1"
    backstory: str | None = None


class _CommunicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentence_length: str
    emoji_frequency: str
    rhetorical_questions: str
    preferred_interaction: str
    language: str = "zh-CN"


class _BehaviorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[str] = Field(default_factory=list)


class _PromptModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_constraints: list[str] = Field(default_factory=list)


class _ErrorsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_unavailable: str | None = None


class _PersonaFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: _IdentityModel
    communication: _CommunicationModel
    traits: dict[str, TraitValue] = Field(default_factory=dict)
    behavior: _BehaviorModel = Field(default_factory=_BehaviorModel)
    prompt: _PromptModel = Field(default_factory=_PromptModel)
    errors: _ErrorsModel = Field(default_factory=_ErrorsModel)


def load_persona(path: Path) -> Persona:
    """读取并校验人格文件。

    Raises:
        PersonaLoadError: 文件不存在 / 不可读 / 不是合法 TOML / 结构不合法。
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
        parsed = _PersonaFile.model_validate(data)
    except ValidationError as exc:
        msg = f"persona file failed validation: {path}\n{exc}"
        raise PersonaLoadError(msg) from exc

    return _to_domain(parsed)


def _to_domain(parsed: _PersonaFile) -> Persona:
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
