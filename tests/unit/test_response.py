"""``astra.domain.response`` 值对象的不变量测试。

这些类型必须"构造即合法"：实测 ``strict: true`` 的 JSON Schema **不约束**
``minimum``/``maximum``（观察到 ``voice.pitch = 200``），所以范围校验只能由
领域类型自己承担。
"""

from __future__ import annotations

import pytest

from astra.domain.response import Action, AssistantResponse, Emotion, Voice


def _voice(**overrides: object) -> Voice:
    params: dict[str, object] = {"style": "warm", "speed": 1.0, "pitch": 0.0}
    params.update(overrides)
    return Voice(**params)  # type: ignore[arg-type]


# ── 合法构造 ────────────────────────────────────────────


def test_valid_construction() -> None:
    response = AssistantResponse(
        reply="你好呀。",
        emotion=Emotion(primary="happy", intensity=0.6),
        action=Action(name="nod", intensity=0.4),
        voice=Voice(style="warm", speed=1.0, pitch=0.0),
    )

    assert response.reply == "你好呀。"
    assert response.emotion.intensity == 0.6


@pytest.mark.parametrize("intensity", [0.0, 0.5, 1.0])
def test_intensity_boundaries_are_inclusive(intensity: float) -> None:
    assert Emotion(primary="neutral", intensity=intensity).intensity == intensity
    assert Action(name="idle", intensity=intensity).intensity == intensity


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_speed_boundaries_are_inclusive(speed: float) -> None:
    assert _voice(speed=speed).speed == speed


@pytest.mark.parametrize("pitch", [-1.0, 0.0, 1.0])
def test_pitch_boundaries_are_inclusive(pitch: float) -> None:
    assert _voice(pitch=pitch).pitch == pitch


# ── 越界拒绝 ────────────────────────────────────────────


@pytest.mark.parametrize("intensity", [-0.01, 1.01, -5.0, 200.0])
def test_emotion_intensity_out_of_range_rejected(intensity: float) -> None:
    with pytest.raises(ValueError, match=r"emotion\.intensity"):
        Emotion(primary="happy", intensity=intensity)


@pytest.mark.parametrize("intensity", [-0.01, 1.01, 200.0])
def test_action_intensity_out_of_range_rejected(intensity: float) -> None:
    with pytest.raises(ValueError, match=r"action\.intensity"):
        Action(name="nod", intensity=intensity)


@pytest.mark.parametrize("speed", [0.49, 2.01, 100.0])
def test_speed_out_of_range_rejected(speed: float) -> None:
    with pytest.raises(ValueError, match=r"voice\.speed"):
        _voice(speed=speed)


@pytest.mark.parametrize("pitch", [-1.01, 1.01, 200.0, -2.0])
def test_pitch_out_of_range_rejected(pitch: float) -> None:
    """实测模型真的返回过 pitch=200，这条不是理论防护。"""
    with pytest.raises(ValueError, match=r"voice\.pitch"):
        _voice(pitch=pitch)


# ── 非有限值 ────────────────────────────────────────────


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected(bad: float) -> None:
    """json.loads 默认接受 NaN / Infinity，必须在领域层拦掉。"""
    with pytest.raises(ValueError, match="finite"):
        Emotion(primary="happy", intensity=bad)

    with pytest.raises(ValueError, match="finite"):
        _voice(pitch=bad)


# ── 不可变性 ────────────────────────────────────────────


def test_response_is_immutable() -> None:
    emotion = Emotion(primary="happy", intensity=0.5)

    with pytest.raises(AttributeError):
        emotion.intensity = 0.9  # type: ignore[misc]
