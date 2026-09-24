"""``astra.domain.prompt`` 的分层组装测试。

重点：层序、逐字节确定性、未实现的层被整段省略、词表出现在 Layer 8。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astra.domain.memory import Memory, MemoryType
from astra.domain.persona import Communication, Identity, Persona
from astra.domain.prompt import (
    BASE_HARD_CONSTRAINTS,
    SECTION_HARD_CONSTRAINTS,
    SECTION_IDENTITY,
    SECTION_OUTPUT_FORMAT,
    SECTION_PERSONALITY,
    SECTION_RELEVANT_MEMORIES,
    SECTION_SPEECH_STYLE,
    build_system_prompt,
)
from astra.domain.vocabulary import (
    ACTION_CHOICES,
    EMOTION_CHOICES,
    MEMORY_TYPE_CHOICES,
    VOICE_STYLE_CHOICES,
)
from astra.infrastructure.persona.toml_loader import load_persona

_REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PERSONA = _REPO_ROOT / "personas" / "astra.toml"

#: 尚未实现的层标题。它们绝不能出现（哪怕是空标题）。
#: Layer 5（RELEVANT MEMORIES）在 M2 已实现，所以不在这个列表里。
UNIMPLEMENTED_SECTIONS = ("CURRENT STATE", "TOOLS")


@pytest.fixture(scope="module")
def persona() -> Persona:
    return load_persona(SHIPPED_PERSONA)


def _minimal_persona(**overrides: object) -> Persona:
    params: dict[str, object] = {
        "identity": Identity(
            name="Test",
            display_name="Test",
            pronouns="它",
            self_description="一个测试角色。",
        ),
        "communication": Communication(
            sentence_length="short",
            emoji_frequency="low",
            rhetorical_questions="rare",
            preferred_interaction="conversational",
        ),
    }
    params.update(overrides)
    return Persona(**params)  # type: ignore[arg-type]


def _memory(
    memory_id: str = "01J000000000000000000000AA",
    *,
    content: str = "用户喜欢深夜写代码",
    importance: float = 0.80,
    confidence: float = 0.90,
    last_accessed_at: object = None,
) -> Memory:
    return Memory(
        id=memory_id,
        user_id="local",
        companion_id="astra",
        type=MemoryType.PREFERENCE,
        content=content,
        created_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        importance=importance,
        confidence=confidence,
        last_accessed_at=last_accessed_at,  # type: ignore[arg-type]
    )


# ── 确定性 ──────────────────────────────────────────────


def test_output_is_byte_identical_for_same_input(persona: Persona) -> None:
    """docs/10 §2 要求 prompt 组装对同一状态是确定的。"""
    first = build_system_prompt(persona)
    second = build_system_prompt(persona)

    assert first == second


def test_trait_order_is_independent_of_mapping_order() -> None:
    """traits 必须排序渲染，否则 dict 插入顺序会污染 prompt。"""
    a = _minimal_persona(traits={"zebra": 0.5, "alpha": 0.1})
    b = _minimal_persona(traits={"alpha": 0.1, "zebra": 0.5})

    assert build_system_prompt(a) == build_system_prompt(b)
    assert build_system_prompt(a).index("alpha") < build_system_prompt(a).index("zebra")


# ── 层序 ────────────────────────────────────────────────


def test_sections_appear_in_documented_order(persona: Persona) -> None:
    prompt = build_system_prompt(persona, memories=(_memory(),))

    positions = [
        prompt.index(SECTION_HARD_CONSTRAINTS),
        prompt.index(SECTION_IDENTITY),
        prompt.index(SECTION_PERSONALITY),
        prompt.index(SECTION_SPEECH_STYLE),
        prompt.index(SECTION_RELEVANT_MEMORIES),
        prompt.index(SECTION_OUTPUT_FORMAT),
    ]

    assert positions == sorted(positions)


def test_unimplemented_layers_are_omitted(persona: Persona) -> None:
    """层 4/7 仍要整段省略——空标题会误导模型以为存在相应信息。"""
    prompt = build_system_prompt(persona, memories=(_memory(),))

    for section in UNIMPLEMENTED_SECTIONS:
        assert section not in prompt


def test_no_empty_section_headers(persona: Persona) -> None:
    prompt = build_system_prompt(persona)

    # 每个标题后面必须紧跟内容，不能出现标题 + 连续两个空行
    for section in (
        SECTION_HARD_CONSTRAINTS,
        SECTION_IDENTITY,
        SECTION_PERSONALITY,
        SECTION_SPEECH_STYLE,
        SECTION_OUTPUT_FORMAT,
    ):
        assert f"{section}\n\n" not in prompt


# ── Layer 0 ─────────────────────────────────────────────


def test_base_constraints_always_present() -> None:
    """基础硬约束在代码里，不能被一个人格文件移除。"""
    prompt = build_system_prompt(_minimal_persona())

    for constraint in BASE_HARD_CONSTRAINTS:
        assert constraint in prompt


def test_persona_constraints_are_appended() -> None:
    prompt = build_system_prompt(_minimal_persona(hard_constraints=("自定义约束",)))

    assert "自定义约束" in prompt
    assert prompt.index(BASE_HARD_CONSTRAINTS[0]) < prompt.index("自定义约束")


# ── Layer 1 / 2 / 3 ─────────────────────────────────────


def test_identity_is_rendered(persona: Persona) -> None:
    prompt = build_system_prompt(persona)

    assert persona.identity.name in prompt
    assert persona.identity.self_description in prompt
    assert persona.identity.pronouns in prompt


def test_backstory_is_optional() -> None:
    without = build_system_prompt(_minimal_persona())
    with_backstory = build_system_prompt(
        _minimal_persona(
            identity=Identity(
                name="Test",
                display_name="Test",
                pronouns="它",
                self_description="x",
                backstory="从前有个测试角色。",
            )
        )
    )

    assert "从前有个测试角色。" not in without
    assert "从前有个测试角色。" in with_backstory


def test_behavior_rules_are_rendered() -> None:
    prompt = build_system_prompt(_minimal_persona(behavior_rules=("不要卖萌。",)))

    assert "不要卖萌。" in prompt


def test_traits_are_rendered_with_two_decimals() -> None:
    prompt = build_system_prompt(_minimal_persona(traits={"curious": 0.876}))

    assert "- curious: 0.88" in prompt


def test_communication_is_rendered(persona: Persona) -> None:
    prompt = build_system_prompt(persona)

    assert persona.communication.language in prompt
    assert persona.communication.sentence_length in prompt


# ── Layer 8 ─────────────────────────────────────────────


def test_vocabulary_is_rendered_in_output_format(persona: Persona) -> None:
    """实测模型会写中文情绪，只靠代码归一化会让情绪全部降级——词表必须进 prompt。"""
    prompt = build_system_prompt(persona)
    output_section = prompt[prompt.index(SECTION_OUTPUT_FORMAT) :]

    for emotion in EMOTION_CHOICES:
        assert emotion in output_section
    for action in ACTION_CHOICES:
        assert action in output_section
    for style in VOICE_STYLE_CHOICES:
        assert style in output_section


def test_output_format_lists_all_five_fields(persona: Persona) -> None:
    prompt = build_system_prompt(persona)
    output_section = prompt[prompt.index(SECTION_OUTPUT_FORMAT) :]

    for field in ("reply", "emotion", "action", "voice", "memory_candidates"):
        assert field in output_section

    # 仍然延后的字段不应出现在 prompt 里，避免模型多写无用 token
    for absent in ("state_delta", "tool_calls"):
        assert absent not in output_section


def test_output_format_lists_memory_type_vocabulary(persona: Persona) -> None:
    """候选的 type 也必须给英文词表——与情绪/动作同理，否则会被归一化掉。"""
    output_section = build_system_prompt(persona)[
        build_system_prompt(persona).index(SECTION_OUTPUT_FORMAT) :
    ]

    for memory_type in MEMORY_TYPE_CHOICES:
        assert memory_type in output_section


def test_output_format_discourages_always_emitting_candidates(persona: Persona) -> None:
    """docs/05 §8：候选**不是每轮都创建**。必须明说，否则模型会硬编一条。"""
    output_section = build_system_prompt(persona)[
        build_system_prompt(persona).index(SECTION_OUTPUT_FORMAT) :
    ]

    assert "空数组" in output_section


def test_prompt_does_not_embed_raw_json_schema(persona: Persona) -> None:
    """硬约束交给 response_format，prompt 里贴整段 schema 是浪费 token。"""
    prompt = build_system_prompt(persona)

    assert "additionalProperties" not in prompt
    assert '"type": "object"' not in prompt


# ── Layer 5：RELEVANT MEMORIES（M2）─────────────────────


def test_no_memories_omits_the_layer_entirely(persona: Persona) -> None:
    """空则整段省略——空标题会让模型以为"系统知道些什么但没说"。"""
    prompt = build_system_prompt(persona)

    assert SECTION_RELEVANT_MEMORIES not in prompt


def test_memories_are_rendered_with_their_scores(persona: Persona) -> None:
    """docs/05 §5：必须给出 importance / confidence，模型才能决定表述的确定程度。"""
    prompt = build_system_prompt(persona, memories=(_memory(),))

    assert SECTION_RELEVANT_MEMORIES in prompt
    assert "content: 用户喜欢深夜写代码" in prompt
    assert "importance: 0.80" in prompt
    assert "confidence: 0.90" in prompt


def test_memory_rendering_is_deterministic(persona: Persona) -> None:
    memories = (_memory("m1", content="事实一"), _memory("m2", content="事实二"))

    assert build_system_prompt(persona, memories=memories) == build_system_prompt(
        persona, memories=memories
    )


def test_memories_keep_the_order_they_were_given(persona: Persona) -> None:
    """排序由 MemoryService 负责（按总分）；prompt 不重排，否则会出现两份排序逻辑。"""
    prompt = build_system_prompt(
        persona, memories=(_memory("m1", content="排第一"), _memory("m2", content="排第二"))
    )

    assert prompt.index("排第一") < prompt.index("排第二")


def test_last_accessed_is_rendered_when_present(persona: Persona) -> None:
    accessed = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)

    prompt = build_system_prompt(persona, memories=(_memory(last_accessed_at=accessed),))

    assert "last_accessed: 2026-09-20" in prompt


def test_last_accessed_is_omitted_when_never_accessed(persona: Persona) -> None:
    prompt = build_system_prompt(persona, memories=(_memory(last_accessed_at=None),))

    assert "last_accessed" not in prompt


def test_memory_budget_drops_the_lowest_ranked_first(persona: Persona) -> None:
    memories = (
        _memory("m1", content="第一条" * 5),
        _memory("m2", content="第二条" * 5),
        _memory("m3", content="第三条" * 5),
    )

    prompt = build_system_prompt(persona, memories=memories, memory_max_chars=80)

    assert "第一条" in prompt
    assert "第三条" not in prompt


def test_first_memory_survives_an_impossible_budget(persona: Persona) -> None:
    """预算再小也要留第一条——否则等于白检索一场。"""
    prompt = build_system_prompt(persona, memories=(_memory(),), memory_max_chars=1)

    assert SECTION_RELEVANT_MEMORIES in prompt
    assert "content: 用户喜欢深夜写代码" in prompt
