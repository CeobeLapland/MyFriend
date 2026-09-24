"""FastAPI 应用装配。

``create_app()`` 是**唯一的依赖装配点**：Settings 在此转成具体的探针、provider
与服务实现，并挂到 ``app.state`` 上供路由通过依赖注入取用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from astra import __version__
from astra.api.connection_manager import ConnectionManager
from astra.api.routes.chat import router as chat_router
from astra.api.routes.chat_ws import router as chat_ws_router
from astra.api.routes.config import router as config_router
from astra.api.routes.conversations import router as conversations_router
from astra.api.routes.dev import router as dev_router
from astra.api.routes.health import router as health_router
from astra.api.routes.memories import router as memories_router
from astra.api.routes.state import router as state_router
from astra.application.conversation_service import ConversationService
from astra.application.event_bus import EventBus
from astra.application.health_service import HealthService
from astra.application.memory_service import (
    MemoryConfig,
    MemoryService,
    RetrievalWeights,
)
from astra.application.orchestrator import Orchestrator, TurnConfig
from astra.application.prompt_builder import PromptBudget
from astra.application.state_service import StateConfig, StateService
from astra.domain.errors import ConversationNotFoundError
from astra.domain.health import DATABASE_COMPONENT, STT_COMPONENT, TTS_COMPONENT
from astra.domain.persona import Persona
from astra.domain.ports import EmbeddingProvider, HealthProbe, LLMProvider, STTProvider, TTSProvider
from astra.infrastructure.db.conversation_repository import SQLiteConversationRepository
from astra.infrastructure.db.health import DatabaseHealthProbe
from astra.infrastructure.db.memory_repository import VECTOR_INDEX_DIM, SQLiteMemoryStore
from astra.infrastructure.db.migrations import upgrade
from astra.infrastructure.db.state_repository import SQLiteStateStore
from astra.infrastructure.persona.toml_loader import load_persona
from astra.providers.cosyvoice.health import CosyVoiceHealthProbe
from astra.providers.cosyvoice.stt import CosyVoiceSTTProvider
from astra.providers.cosyvoice.tts import CosyVoiceTTSProvider
from astra.providers.lmstudio.client import LMStudioProvider
from astra.providers.lmstudio.embeddings import LMStudioEmbeddingProvider
from astra.providers.lmstudio.health import LMStudioHealthProbe
from astra.settings import Settings

_logger = logging.getLogger(__name__)

_LMSTUDIO = "lmstudio"
_COSYVOICE = "cosyvoice"


def create_app(
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    embeddings: EmbeddingProvider | None = None,
    stt: STTProvider | None = None,
    tts: TTSProvider | None = None,
) -> FastAPI:
    """按给定配置构造 FastAPI 应用。

    Args:
        settings: 运行时配置。
        llm: 可选的 provider 注入点。为 ``None`` 时按 ``settings.llm_provider``
            构造；显式注入主要用于测试，也方便将来接别的后端。
        embeddings: 可选的向量化 provider 注入点，语义同上。
        stt: 可选的语音识别 provider 注入点，语义同上。
        tts: 可选的语音合成 provider 注入点，语义同上。

    Raises:
        PersonaLoadError: 人格文件缺失或非法——人格是应用的身份，缺失应当快速失败。
        ValueError: ``embedding_dim`` 与向量索引的维度不一致。迁移把维度写死在 DDL
            里（不可改），所以这里必须快速失败，而不是等运行时插入才报错（ADR 0018）。
    """
    persona = load_persona(settings.persona_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 启动时确保 schema 就绪（M0 验收项在运行时的保证）
        applied = await asyncio.to_thread(upgrade, settings.db_path)
        if applied:
            _logger.info(
                "applied migrations: %s",
                ", ".join(applied),
                extra={"event": "db.migrated", "detail": applied},
            )
        yield

    app = FastAPI(title="Astra", version=__version__, lifespan=lifespan)

    _install_health(app, settings)
    _install_conversation_stack(app, settings, persona, llm, embeddings, stt, tts)

    app.state.settings = settings
    app.state.persona = persona

    app.add_exception_handler(ConversationNotFoundError, _conversation_not_found_handler)

    app.include_router(health_router)
    app.include_router(config_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(memories_router)
    app.include_router(state_router)
    app.include_router(chat_ws_router)

    # 开发者控制台只在 dev 环境挂载——绑到 0.0.0.0 时把 ASTRA_ENV 设成 prod 即可关掉
    if settings.env == "dev":
        app.include_router(dev_router)

    return app


async def _conversation_not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    """未知 / 已结束的会话统一返回 404，而不是静默返回空列表。"""
    assert isinstance(exc, ConversationNotFoundError)  # noqa: S101 - 由 add_exception_handler 保证
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": {"code": exc.code.value, "message": exc.message}},
    )


def _install_health(app: FastAPI, settings: Settings) -> None:
    probes: list[HealthProbe] = [DatabaseHealthProbe(settings.db_path)]

    # provider=none 时不注册探针，/health 保持 M0 的 not_configured（ADR 0006）。
    # **只改这些组件的值，不改 key 集合**——stt/tts 始终在 CANONICAL_COMPONENTS 里。
    if settings.llm_provider == _LMSTUDIO:
        probes.append(
            LMStudioHealthProbe(
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key.get_secret_value(),
                timeout_s=settings.health_probe_timeout_s,
            )
        )

    # STT 与 TTS 是同一个 Gradio 进程，探针逻辑共用，只是组件名不同
    for component, provider_name in (
        (STT_COMPONENT, settings.stt_provider),
        (TTS_COMPONENT, settings.tts_provider),
    ):
        if provider_name == _COSYVOICE:
            probes.append(
                CosyVoiceHealthProbe(
                    component=component,
                    base_url=settings.tts_base_url,
                    timeout_s=settings.health_probe_timeout_s,
                )
            )

    required = frozenset({DATABASE_COMPONENT}) if settings.health_db_required else frozenset()

    app.state.health_service = HealthService(probes, version=__version__, required=required)


def _install_conversation_stack(
    app: FastAPI,
    settings: Settings,
    persona: Persona,
    llm: LLMProvider | None,
    embeddings: EmbeddingProvider | None,
    stt: STTProvider | None,
    tts: TTSProvider | None,
) -> None:
    repository = SQLiteConversationRepository(settings.db_path)
    conversations = ConversationService(
        repository,
        user_id=settings.user_id,
        companion_id=settings.companion_id,
        idle_timeout=timedelta(minutes=settings.conversation_idle_minutes),
    )

    # 事件总线是单进程内的广播点：Orchestrator / StateService 往里发，
    # ConnectionManager 往外推
    bus = EventBus()

    provider = llm if llm is not None else _build_llm_provider(settings)
    memories = _build_memory_service(settings, embeddings)
    state = _build_state_service(settings, bus)
    tts = tts if tts is not None else _build_tts_provider(settings)
    stt = stt if stt is not None else _build_stt_provider(settings)

    orchestrator = Orchestrator(
        llm=provider,
        conversations=conversations,
        persona=persona,
        bus=bus,
        memory=memories,
        state=state,
        tts=tts,
        config=TurnConfig(
            user_id=settings.user_id,
            companion_id=settings.companion_id,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            request_timeout_s=settings.llm_request_timeout_s,
            reasoning_effort=settings.llm_reasoning_effort,
            history_budget=PromptBudget(
                max_history_messages=settings.llm_history_max_messages,
                max_history_chars=settings.llm_history_max_chars,
                memory_max_chars=settings.memory_context_max_chars,
            ),
            tts_voice=settings.tts_voice,
            audio_chunk_bytes=settings.audio_chunk_max_bytes,
        ),
    )

    app.state.conversations = conversations
    app.state.orchestrator = orchestrator
    app.state.memories = memories
    app.state.state_service = state
    app.state.event_bus = bus
    app.state.connection_manager = ConnectionManager(bus)
    #: WS 层的语音输入入口。``None`` 表示没配 STT，语音输入整体缺席
    app.state.stt_provider = stt


def _build_tts_provider(settings: Settings) -> TTSProvider | None:
    """``tts_provider=none`` 时返回 ``None``——语音输出整体缺席，文本照常。"""
    if settings.tts_provider != _COSYVOICE:
        return None

    return CosyVoiceTTSProvider(
        base_url=settings.tts_base_url,
        voice_id=settings.tts_voice,
        timeout_s=settings.tts_timeout_s,
    )


def _build_stt_provider(settings: Settings) -> STTProvider | None:
    """``stt_provider=none`` 时返回 ``None``——键盘输入仍然可用（`docs/01` §3）。"""
    if settings.stt_provider != _COSYVOICE:
        return None

    return CosyVoiceSTTProvider(
        base_url=settings.tts_base_url,
        timeout_s=settings.stt_timeout_s,
    )


def _build_state_service(settings: Settings, bus: EventBus) -> StateService:
    """装配状态子系统。

    状态**没有"未配置"这一档**：情绪与关系是这个角色的基本属性，不需要外部服务，
    所以永远装配。``state_emotion_pull=0`` 可以关掉"情绪随表达变化"那条路径。
    """
    return StateService(
        SQLiteStateStore(settings.db_path),
        config=StateConfig(
            companion_id=settings.companion_id,
            decay_half_life_minutes=settings.state_emotion_decay_half_life_minutes,
            pull=settings.state_emotion_pull,
        ),
        bus=bus,
    )


def _build_memory_service(
    settings: Settings, embeddings: EmbeddingProvider | None
) -> MemoryService:
    """装配记忆子系统。

    **维度必须与向量索引一致**：``memory_vectors`` 的 ``float[768]`` 写在迁移里
    （不可改），维度不符时插入会失败。在这里快速失败，报错信息直接告诉用户该改什么。
    """
    if settings.embedding_dim != VECTOR_INDEX_DIM:
        msg = (
            f"ASTRA_EMBEDDING_DIM={settings.embedding_dim} does not match the vector index "
            f"dimension ({VECTOR_INDEX_DIM}) baked into migration 0003. "
            "Changing the embedding model requires a new migration and a re-index."
        )
        raise ValueError(msg)

    store = SQLiteMemoryStore(
        settings.db_path,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
    )

    return MemoryService(
        store,
        embeddings if embeddings is not None else _build_embedding_provider(settings),
        config=MemoryConfig(
            user_id=settings.user_id,
            companion_id=settings.companion_id,
            enabled=settings.memory_enabled,
            top_k=settings.memory_top_k,
            candidate_limit=settings.memory_candidate_limit,
            max_candidates_per_turn=settings.memory_max_candidates_per_turn,
            dedup_threshold=settings.memory_dedup_threshold,
            min_similarity=settings.memory_min_similarity,
            context_max_chars=settings.memory_context_max_chars,
            weights=RetrievalWeights(
                semantic=settings.memory_weight_semantic,
                importance=settings.memory_weight_importance,
                recency=settings.memory_weight_recency,
                access=settings.memory_weight_access,
                relationship=settings.memory_weight_relationship,
            ),
        ),
    )


def _build_llm_provider(settings: Settings) -> LLMProvider | None:
    if settings.llm_provider != _LMSTUDIO:
        return None

    return LMStudioProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        connect_timeout_s=settings.llm_connect_timeout_s,
        request_timeout_s=settings.llm_request_timeout_s,
        default_max_tokens=settings.llm_max_tokens,
        default_temperature=settings.llm_temperature,
        default_reasoning_effort=settings.llm_reasoning_effort,
    )


def _build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """``embedding_provider=none`` 时返回 ``None``——记忆子系统随之整体缺席。"""
    if settings.embedding_provider != _LMSTUDIO:
        return None

    return LMStudioEmbeddingProvider(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=settings.llm_api_key.get_secret_value(),
        connect_timeout_s=settings.llm_connect_timeout_s,
        request_timeout_s=settings.embedding_timeout_s,
    )
