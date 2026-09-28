"""动画映射配置（``astra_animation.json``）的结构与校验。

**这是后端的契约面**：Unity 侧用 `AnimationConfig` 反序列化它
（`Assets/Astra/Scripts/AstraAnimationConfig.cs`），Studio 侧编辑它。
字段名保持直白——用户会手改这个文件。

**只校验、不重写**（ADR 0027 §2）：通过校验的原文原样落盘，未知字段保留。
所以这里只回答"这份 JSON 能不能用"，不产出规范化后的结构。
"""

from __future__ import annotations

from dataclasses import dataclass

from astra.domain.vocabulary import ACTIONS

#: 可配三件套的姿态名。**与 Unity 的 ``AstraAnimationConfig.PoseNames`` 对齐**——
#: 将来加姿态要两侧一起改（ADR 0027 §2）
POSES: frozenset[str] = frozenset({"sit", "lie"})

#: 最多回几条问题给 UI——够定位就行，不要把整个列表甩出去
_MAX_ISSUES = 8


@dataclass(frozen=True, slots=True)
class AnimationIssue:
    """一处问题。``path`` 形如 ``actions[2].clips[0].weight``，直接指到字段。"""

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


def validate_animation(payload: object) -> list[AnimationIssue]:
    """校验一份动画配置。返回空列表 = 通过。

    **缺省即空**：``idlePool`` / ``actions`` / ``poses`` 三个键可以缺（Unity 的
    ``Normalize`` 也这样容忍），这样手写一份最小配置也能通过。
    """
    if not isinstance(payload, dict):
        return [AnimationIssue("$", "顶层必须是一个 JSON 对象")]

    issues: list[AnimationIssue] = []
    _check_clip_list(payload.get("idlePool", []), "idlePool", issues)
    _check_actions(payload.get("actions", []), issues)
    _check_poses(payload.get("poses", []), issues)
    return issues[:_MAX_ISSUES]


def _check_clip_list(value: object, path: str, issues: list[AnimationIssue]) -> None:
    if not isinstance(value, list):
        issues.append(AnimationIssue(path, "必须是数组"))
        return
    for index, item in enumerate(value):
        _check_clip_ref(item, f"{path}[{index}]", issues)


def _check_clip_ref(value: object, path: str, issues: list[AnimationIssue]) -> None:
    if not isinstance(value, dict):
        issues.append(AnimationIssue(path, "必须是对象（clip / weight / loop）"))
        return

    clip = value.get("clip")
    if not isinstance(clip, str) or not clip.strip():
        issues.append(AnimationIssue(f"{path}.clip", "clip 名不能为空"))

    weight = value.get("weight")
    if weight is not None:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            issues.append(AnimationIssue(f"{path}.weight", "权重必须是数字"))
        elif weight <= 0:
            issues.append(
                AnimationIssue(f"{path}.weight", "权重要大于 0（否则这个 clip 永远抽不到）")
            )

    loop = value.get("loop")
    if loop is not None and not isinstance(loop, bool):
        issues.append(AnimationIssue(f"{path}.loop", "loop 必须是布尔值"))


def _check_actions(value: object, issues: list[AnimationIssue]) -> None:
    if not isinstance(value, list):
        issues.append(AnimationIssue("actions", "必须是数组"))
        return

    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"actions[{index}]"
        if not isinstance(item, dict):
            issues.append(AnimationIssue(path, "必须是对象（action / clips）"))
            continue

        name = item.get("action")
        if not isinstance(name, str) or not name.strip():
            issues.append(AnimationIssue(f"{path}.action", "动作名不能为空"))
            continue
        if name not in ACTIONS:
            issues.append(
                AnimationIssue(
                    f"{path}.action",
                    f"未知动作名 {name!r}（后端只会发这些：{', '.join(sorted(ACTIONS))}）",
                )
            )
            continue
        if name in seen:
            issues.append(
                AnimationIssue(f"{path}.action", f"动作 {name!r} 重复了（Unity 只认第一条）")
            )
            continue
        seen.add(name)

        clips = item.get("clips", [])
        _check_clip_list(clips, f"{path}.clips", issues)


def _check_poses(value: object, issues: list[AnimationIssue]) -> None:
    if not isinstance(value, list):
        issues.append(AnimationIssue("poses", "必须是数组"))
        return

    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"poses[{index}]"
        if not isinstance(item, dict):
            issues.append(AnimationIssue(path, "必须是对象（pose / enter / loop / exit）"))
            continue

        name = item.get("pose")
        if not isinstance(name, str) or not name.strip():
            issues.append(AnimationIssue(f"{path}.pose", "姿态名不能为空"))
            continue
        if name not in POSES:
            issues.append(
                AnimationIssue(
                    f"{path}.pose", f"未知姿态 {name!r}（Unity 只认：{', '.join(sorted(POSES))}）"
                )
            )
            continue
        if name in seen:
            issues.append(
                AnimationIssue(f"{path}.pose", f"姿态 {name!r} 重复了（Unity 只认第一条）")
            )
            continue
        seen.add(name)

        for slot in ("enter", "exit"):
            slot_value = item.get(slot)
            # 空串 / null 都表示"这一格没配"——与 C# 的 ``string.IsNullOrEmpty`` 判定一致
            if slot_value is not None and not isinstance(slot_value, str):
                issues.append(
                    AnimationIssue(f"{path}.{slot}", "要么给 clip 名（字符串），要么留空")
                )

        _check_clip_list(item.get("loop", []), f"{path}.loop", issues)
