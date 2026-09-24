"""状态管理器（`docs/02_ARCHITECTURE.md` §2.5 的 State Manager）。

情绪与关系状态的唯一读写入口。Orchestrator 只调这里。

**四条不变量**：

1. **失败即降级**。状态读写失败只记日志并返回基线，**绝不让 turn 失败**——
   LLM 已经成功，用户必须拿到回复（与记忆子系统同一模式，ADR 0014）。
2. **惰性衰减**。情绪的衰减在**读**的时候按 ``now - emotion_updated_at`` 算，
   不落库、不起调度器。DB 里存的是"最后一次显式应用的状态"这个**锚点**，
   有效值是锚点与时刻的纯函数——所以读多少次都不会重复衰减。
3. **情绪比例靠拢，关系只累加**。见 `domain/state.py` 与 ADR 0020。
4. **整个 read-modify-write 在一把专用锁里**。状态是每个 companion 一行，
   会被不同会话的 turn 同时读写；不加锁会丢更新（"称赞了但好感没涨"）。

**状态变化只来自事件**（`docs/03` §5 的硬约束）：用户侧事件规则 + 已表达的情绪。
没有"用户直接写状态"的接口。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from astra.application.event_bus import EventBus
from astra.domain.events import EventType, make_event
from astra.domain.ports import StateStore
from astra.domain.response import Emotion
from astra.domain.state import (
    EMOTION_DIMS,
    REUNION_AFTER_HOURS,
    REUNION_EMOTION_EXTREMES,
    REUNION_EMOTION_STRENGTH,
    REUNION_RELATIONSHIP_DELTAS,
    REUNION_RULE_NAME,
    TRANSITION_RULES,
    EmotionState,
    StateSnapshot,
    baseline_emotion,
    baseline_relationship,
    emotion_targets_for,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StateConfig:
    """``StateService`` 的策略参数（来自 ``Settings``）。"""

    companion_id: str
    #: 情绪衰减半衰期。**这个值缺乏依据**——先给 120 分钟，需要评测调参（docs/10 §5）。
    decay_half_life_minutes: int = 120
    #: 朝"已表达情绪的目标水平"靠拢的比例。0 表示完全不受表达影响。
    pull: float = 0.15


@dataclass(frozen=True, slots=True)
class StateChange:
    """一次状态更新的结果，供 Orchestrator 计数与发事件用。"""

    snapshot: StateSnapshot
    emotion_deltas: Mapping[str, float]
    relationship_deltas: Mapping[str, float]
    #: 命中的规则名（`docs/03` §5 的三条 + 久别重逢）。可观测性用。
    rules: tuple[str, ...]
    #: **实际有没有变**，而不是"有没有规则增量"——"朝表达情绪靠拢"这条路径
    #: 不产生规则增量却会真的改变状态，用前者才能把事件发对。
    emotion_changed: bool
    relationship_changed: bool


class StateService:
    """情绪 / 关系状态的唯一入口。"""

    def __init__(
        self,
        store: StateStore,
        *,
        config: StateConfig,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        #: 没有订阅者时 publish 是空操作，默认自带一个不会产生副作用
        self._bus = bus if bus is not None else EventBus()
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))
        #: 保护整个 read-modify-write，避免并发 turn 丢更新
        self._lock = asyncio.Lock()

    # ── 读 ──────────────────────────────────────────────

    @property
    def companion_id(self) -> str:
        """这个服务负责哪个角色——API 层要把它放进响应里。"""
        return self._config.companion_id

    async def load_snapshot(self) -> StateSnapshot:
        """读当前状态（含惰性衰减）。

        **失败返回基线**：调用方（Layer 4 渲染）拿到基线只是"她现在是平静的"，
        而抛出去会让整轮对话失败。
        """
        try:
            return await self._effective_snapshot()
        except Exception:
            _logger.warning(
                "state load failed; falling back to baseline",
                extra={"event": "state.failed", "detail": {"stage": "load"}},
            )
            moment = self._clock()
            return StateSnapshot(
                emotion=baseline_emotion(),
                relationship=baseline_relationship(),
                emotion_updated_at=moment,
                relationship_updated_at=moment,
            )

    # ── 写 ──────────────────────────────────────────────

    async def apply_turn(
        self,
        *,
        user_text: str,
        emotion: Emotion | None = None,
        correlation_id: str | None = None,
    ) -> StateChange | None:
        """按本轮的用户消息与已表达情绪更新状态并落库。

        ``correlation_id`` 由调用方（Orchestrator）传入**该 turn 的** id——
        ADR 0012 要求"一个 turn 一个 correlation_id，复制进该 turn 的每个事件"。
        不传时（没有 turn 上下文，如调试路径）回落到一个角色级 id。

        返回 ``None`` 表示状态子系统失败（只记日志，调用方继续走）。
        """
        try:
            async with self._lock:
                return await self._apply_locked(
                    user_text=user_text, emotion=emotion, correlation_id=correlation_id
                )
        except Exception:
            _logger.warning(
                "state update failed; turn is unaffected",
                extra={"event": "state.failed", "detail": {"stage": "apply"}},
            )
            return None

    async def reset(self) -> None:
        """把状态清回基线（调试用）。"""
        await self._store.reset(self._config.companion_id)

    # ── 内部 ────────────────────────────────────────────

    async def _apply_locked(
        self, *, user_text: str, emotion: Emotion | None, correlation_id: str | None
    ) -> StateChange:
        moment = self._clock()
        current = await self._effective_snapshot(now=moment)
        before = current.emotion

        extremes: dict[str, float] = {}
        relationship_deltas: dict[str, float] = {}
        strength = 0.0
        rules: list[str] = []

        for rule in TRANSITION_RULES:
            if rule.pattern.search(user_text):
                rules.append(rule.name)
                extremes.update(rule.emotion)
                strength = max(strength, rule.emotion_strength)
                _accumulate(relationship_deltas, rule.relationship)

        if self._is_reunion(current, moment):
            rules.append(REUNION_RULE_NAME)
            extremes.update(REUNION_EMOTION_EXTREMES)
            strength = max(strength, REUNION_EMOTION_STRENGTH)
            _accumulate(relationship_deltas, REUNION_RELATIONSHIP_DELTAS)

        # 先按事件规则推进（比例，不是加法），再朝表达情绪的目标靠拢。
        # 两步都是**收敛**的，所以连读施加同一输入不会把值顶到 1.0（ADR 0020）。
        new_emotion = before.pushed_toward(extremes, strength=strength)
        if emotion is not None:
            targets = emotion_targets_for(emotion.primary, emotion.intensity)
            if targets:
                new_emotion = new_emotion.pulled_toward(targets, pull=self._config.pull)

        # 事件 payload 里的 deltas 报**实际变化量**，而不是规则表里的意图——
        # 两者在"已经接近极值"时差很多，报意图会误导调参。
        emotion_deltas = _actual_deltas(before, new_emotion)

        # 关系只累加、不衰减。没有增量时**不刷新时间戳**——否则"最近更新"会撒谎
        new_relationship = current.relationship
        relationship_at = current.relationship_updated_at
        if relationship_deltas:
            new_relationship = current.relationship.with_deltas(relationship_deltas)
            relationship_at = moment

        updated = StateSnapshot(
            emotion=new_emotion,
            relationship=new_relationship,
            emotion_updated_at=moment,
            relationship_updated_at=relationship_at,
        )
        await self._store.save(self._config.companion_id, updated)

        change = StateChange(
            snapshot=updated,
            emotion_deltas=emotion_deltas,
            relationship_deltas=relationship_deltas,
            rules=tuple(rules),
            emotion_changed=new_emotion != current.emotion,
            relationship_changed=new_relationship != current.relationship,
        )
        self._publish(change, correlation_id=correlation_id)
        return change

    async def _effective_snapshot(self, *, now: datetime | None = None) -> StateSnapshot:
        """读 + 惰性衰减。**不落库**——DB 里的值只是锚点（见模块文档字符串）。"""
        moment = now if now is not None else self._clock()
        stored = await self._store.load(self._config.companion_id)

        if stored is None:
            return StateSnapshot(
                emotion=baseline_emotion(),
                relationship=baseline_relationship(),
                emotion_updated_at=moment,
                relationship_updated_at=moment,
            )

        elapsed_hours = (moment - stored.emotion_updated_at).total_seconds() / 3600.0
        decayed = stored.emotion.decayed(
            elapsed_hours=elapsed_hours,
            half_life_hours=self._config.decay_half_life_minutes / 60.0,
        )
        return replace(stored, emotion=decayed)

    @staticmethod
    def _is_reunion(snapshot: StateSnapshot, moment: datetime) -> bool:
        """久别重逢（`docs/03` §5 的 ``USER_RETURNED_AFTER_LONG_ABSENCE``）。

        以 ``emotion_updated_at`` 为"上次互动时间"的代理——状态每轮都会更新，
        所以它约等于最后一次 turn 的时间，不需要额外查会话历史。
        """
        elapsed_hours = (moment - snapshot.emotion_updated_at).total_seconds() / 3600.0
        return elapsed_hours >= REUNION_AFTER_HOURS

    def _publish(self, change: StateChange, *, correlation_id: str | None) -> None:
        # ADR 0012：一个 turn 一个 correlation_id，该 turn 的每个事件都用它。
        # 没有 turn 上下文时（调试路径）回落到角色级 id。
        correlation = correlation_id or f"state:{self._config.companion_id}"

        if change.emotion_changed:
            self._bus.publish(
                make_event(
                    EventType.STATE_EMOTION_UPDATED,
                    correlation_id=correlation,
                    payload={
                        "companion_id": self._config.companion_id,
                        "emotion": change.snapshot.emotion.as_dict(),
                        "deltas": dict(change.emotion_deltas),
                        "rules": list(change.rules),
                        "updated_at": change.snapshot.emotion_updated_at.isoformat(),
                    },
                )
            )
        if change.relationship_changed:
            self._bus.publish(
                make_event(
                    EventType.STATE_RELATIONSHIP_UPDATED,
                    correlation_id=correlation,
                    payload={
                        "companion_id": self._config.companion_id,
                        "relationship": change.snapshot.relationship.as_dict(),
                        "deltas": dict(change.relationship_deltas),
                        "rules": list(change.rules),
                        "updated_at": change.snapshot.relationship_updated_at.isoformat(),
                    },
                )
            )

        _logger.info(
            "state updated",
            extra={
                "event": "state.updated",
                "detail": {
                    "rules": list(change.rules),
                    "emotion_deltas": dict(change.emotion_deltas),
                    "relationship_deltas": dict(change.relationship_deltas),
                },
            },
        )


def _accumulate(target: dict[str, float], deltas: Mapping[str, float]) -> None:
    for name, value in deltas.items():
        target[name] = target.get(name, 0.0) + value


def _actual_deltas(before: EmotionState, after: EmotionState) -> dict[str, float]:
    """逐维度算实际变化量，只保留真的变了的（供事件 payload 与日志用）。"""
    old, new = before.as_dict(), after.as_dict()
    return {name: new[name] - old[name] for name in EMOTION_DIMS if new[name] != old[name]}
