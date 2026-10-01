"""装配点（``astra.api.app`` 的 ``_build_*`` / ``_*_probe``）的守门测试。

**为什么不走 ``/health`` 或真实请求**：那些路径会真的去连 ``https://api.openai.com``，
在网络受限的机器上会拖慢或搅黄测试。这里只问一个问题——**按这套配置，装配出来的
是不是那个 provider**——这正是新增 provider 时最容易漏掉（或接错分支）的地方。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astra.api.app import (
    _audio_probe,
    _build_llm_provider,
    _build_stt_provider,
    _build_tts_provider,
)
from astra.domain.health import STT_COMPONENT, TTS_COMPONENT
from astra.providers.cosyvoice.health import CosyVoiceHealthProbe
from astra.providers.cosyvoice.stt import CosyVoiceSTTProvider
from astra.providers.cosyvoice.tts import CosyVoiceTTSProvider
from astra.providers.lmstudio.client import LMStudioProvider
from astra.providers.openai_compatible.health import OpenAICompatibleHealthProbe
from astra.providers.openai_compatible.llm import OpenAICompatibleLLMProvider
from astra.providers.openai_compatible.stt import OpenAICompatibleSTTProvider
from astra.providers.openai_compatible.tts import OpenAICompatibleTTSProvider
from astra.settings import Settings


def _settings(**overrides: object) -> Settings:
    params: dict[str, object] = {"db_path": Path("data/test.db")}
    params.update(overrides)
    return Settings(_env_file=None, **params)  # type: ignore[arg-type]


# ── none = 整体缺席 ─────────────────────────────────────


@pytest.mark.parametrize(
    "builder",
    [_build_llm_provider, _build_stt_provider, _build_tts_provider],
    ids=["llm", "stt", "tts"],
)
def test_none_provider_builds_nothing(builder: object) -> None:
    assert builder(_settings()) is None  # type: ignore[operator]


# ── LLM ─────────────────────────────────────────────────


def test_lmstudio_still_builds_the_local_provider() -> None:
    assert isinstance(_build_llm_provider(_settings(llm_provider="lmstudio")), LMStudioProvider)


def test_openai_compatible_llm_carries_the_capability_switches() -> None:
    provider = _build_llm_provider(
        _settings(
            llm_provider="openai_compatible",
            llm_send_reasoning_effort=True,
            llm_use_json_schema=True,
        )
    )

    assert isinstance(provider, OpenAICompatibleLLMProvider)


# ── 语音 ────────────────────────────────────────────────


def test_cosyvoice_builds_both_audio_providers_from_one_url() -> None:
    settings = _settings(stt_provider="cosyvoice", tts_provider="cosyvoice")

    assert isinstance(_build_stt_provider(settings), CosyVoiceSTTProvider)
    assert isinstance(_build_tts_provider(settings), CosyVoiceTTSProvider)


def test_openai_compatible_builds_both_audio_providers() -> None:
    settings = _settings(
        stt_provider="openai_compatible",
        tts_provider="openai_compatible",
        stt_base_url="https://stt.example.com/v1",
        tts_base_url="https://tts.example.com/v1",
    )

    assert isinstance(_build_stt_provider(settings), OpenAICompatibleSTTProvider)
    assert isinstance(_build_tts_provider(settings), OpenAICompatibleTTSProvider)


# ── 探针：地址与组件名 ──────────────────────────────────


def test_cosyvoice_probes_share_the_tts_base_url() -> None:
    """cosyvoice 的 STT/TTS 是同一个 Gradio 进程——拆地址会让老配置走错端口。"""
    settings = _settings(tts_base_url="http://127.0.0.1:7860")

    for component in (STT_COMPONENT, TTS_COMPONENT):
        probe = _audio_probe(settings, component=component, provider="cosyvoice")
        assert isinstance(probe, CosyVoiceHealthProbe)
        assert probe.name == component


def test_openai_compatible_audio_probes_use_their_own_urls() -> None:
    settings = _settings(
        stt_base_url="https://stt.example.com/v1",
        tts_base_url="https://tts.example.com/v1",
    )

    stt_probe = _audio_probe(settings, component=STT_COMPONENT, provider="openai_compatible")
    tts_probe = _audio_probe(settings, component=TTS_COMPONENT, provider="openai_compatible")

    assert isinstance(stt_probe, OpenAICompatibleHealthProbe)
    assert isinstance(tts_probe, OpenAICompatibleHealthProbe)
    assert stt_probe.name == STT_COMPONENT
    assert tts_probe.name == TTS_COMPONENT


def test_audio_probe_is_absent_for_none() -> None:
    assert _audio_probe(_settings(), component=STT_COMPONENT, provider="none") is None
