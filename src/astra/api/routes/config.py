"""``GET /v1/config`` —— 非敏感运行配置 + 协议词表。

词表（emotions / actions / voice_styles / memory_types）在这里**分发给客户端**：
它们是协议契约，必须有单一真相（ADR 0015）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from astra.api.dependencies import get_app_settings, get_memory_service
from astra.api.schemas import ConfigBody
from astra.application.memory_service import MemoryService
from astra.domain.vocabulary import (
    ACTION_CHOICES,
    EMOTION_CHOICES,
    MEMORY_TYPE_CHOICES,
    VOICE_STYLE_CHOICES,
)
from astra.settings import Settings

router = APIRouter(prefix="/v1", tags=["config"])

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
MemoryDep = Annotated[MemoryService, Depends(get_memory_service)]


@router.get("/config", response_model=ConfigBody, summary="非敏感运行配置")
async def read_config(settings: SettingsDep, memories: MemoryDep) -> ConfigBody:
    return ConfigBody(
        companion_id=settings.companion_id,
        user_id=settings.user_id,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        # M4 / M5 接入时改为读 Settings；现在写常量，避免提前引入未使用的配置字段
        stt_provider="none",
        tts_provider="none",
        avatar_enabled=False,
        emotions=list(EMOTION_CHOICES),
        actions=list(ACTION_CHOICES),
        voice_styles=list(VOICE_STYLE_CHOICES),
        # 读运行时开关而不是 Settings：用户可以在不重启的情况下禁用它
        memory_enabled=await memories.is_enabled(),
        memory_types=list(MEMORY_TYPE_CHOICES),
    )
