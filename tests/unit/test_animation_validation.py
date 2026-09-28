"""``astra.domain.animation`` 的单元测试。

校验规则是"Studio 能不能写坏 Unity 的映射"的守门人，所以逐条钉住：
结构、clip 名、权重、动作名（必须落在协议词表里）、姿态名、重复项。
"""

from __future__ import annotations

from astra.domain.animation import validate_animation

_MINIMAL = {
    "idlePool": [{"clip": "Idle_1", "weight": 1.0, "loop": True}],
    "actions": [
        {"action": "wave", "clips": [{"clip": "Breakdance Ready", "weight": 1.0, "loop": False}]}
    ],
    "poses": [
        {"pose": "sit", "enter": "Sit Down", "loop": [{"clip": "Sitting Idle"}], "exit": "Stand Up"}
    ],
}


def test_accepts_a_full_config() -> None:
    assert validate_animation(_MINIMAL) == []


def test_accepts_missing_blocks() -> None:
    """三个块可以缺（Unity 的 Normalize 也这样容忍）——最小配置 {} 也算合法。"""
    assert validate_animation({}) == []
    assert validate_animation({"idlePool": []}) == []


def test_rejects_non_object() -> None:
    issues = validate_animation([1, 2, 3])

    assert len(issues) == 1
    assert issues[0].path == "$"


def test_rejects_empty_clip_name() -> None:
    issues = validate_animation({"idlePool": [{"clip": "   "}]})

    assert issues[0].path == "idlePool[0].clip"


def test_rejects_non_positive_weight() -> None:
    issues = validate_animation({"idlePool": [{"clip": "Idle_1", "weight": 0}]})

    assert "权重要大于 0" in issues[0].message

    issues = validate_animation({"idlePool": [{"clip": "Idle_1", "weight": -1}]})
    assert issues[0].path == "idlePool[0].weight"


def test_rejects_non_numeric_weight() -> None:
    issues = validate_animation({"idlePool": [{"clip": "Idle_1", "weight": "heavy"}]})

    assert issues[0].path == "idlePool[0].weight"


def test_rejects_non_boolean_loop() -> None:
    issues = validate_animation({"idlePool": [{"clip": "Idle_1", "loop": "yes"}]})

    assert issues[0].path == "idlePool[0].loop"


def test_rejects_unknown_action_name() -> None:
    """后端只会发协议词表里的动作——写个它永远不发的名字等于配了个死条目。"""
    issues = validate_animation({"actions": [{"action": "dance", "clips": []}]})

    assert issues[0].path == "actions[0].action"
    assert "未知动作名" in issues[0].message


def test_rejects_duplicate_action() -> None:
    issues = validate_animation(
        {"actions": [{"action": "wave", "clips": []}, {"action": "wave", "clips": []}]}
    )

    assert issues[0].path == "actions[1].action"
    assert "重复" in issues[0].message


def test_rejects_unknown_pose() -> None:
    issues = validate_animation({"poses": [{"pose": "sleep"}]})

    assert issues[0].path == "poses[0].pose"
    assert "未知姿态" in issues[0].message


def test_accepts_empty_pose_slots() -> None:
    """空串 / null = "这一格没配"（与 C# 的 ``string.IsNullOrEmpty`` 判定一致）。"""
    assert validate_animation({"poses": [{"pose": "sit", "enter": "", "exit": None}]}) == []


def test_rejects_non_string_pose_slot() -> None:
    issues = validate_animation({"poses": [{"pose": "sit", "enter": 3}]})

    assert issues[0].path == "poses[0].enter"


def test_reports_nested_paths() -> None:
    issues = validate_animation(
        {"actions": [{"action": "wave", "clips": [{"clip": "ok"}, {"clip": ""}]}]}
    )

    assert issues[0].path == "actions[0].clips[1].clip"


def test_reports_type_errors_for_blocks() -> None:
    issues = validate_animation({"idlePool": "nope", "actions": 3, "poses": {}})

    assert [issue.path for issue in issues] == ["idlePool", "actions", "poses"]
