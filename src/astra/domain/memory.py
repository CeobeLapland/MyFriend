"""记忆的领域值对象（`docs/03_DATA_MODEL.md` §2、`docs/06_MEMORY.md`）。

**与 ``domain/response.py`` 同一套职责划分**：
- **数值范围**由本模块的 ``__post_init__`` 强制（越界即 ``ValueError``）。实测模型
  会给越界数值（M1 的 ``voice.pitch=200``、M2 的 ``importance=10``），所以这些类型
  必须做到"构造即合法"。
- **词表成员资格**由 ``domain/vocabulary.py`` 负责——它有明确的降级策略与日志。

**TTL 策略为什么是模块常量而不是 ``Settings`` 字段**：这是**语义规则**
（`docs/06` §8：episodic 是短期、semantic 是长期），不是运维旋钮。放代码里只有
一处真相，也避免再往已经偏大的 Settings 里塞 5 个字段（见 ADR 0018）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class MemoryType(StrEnum):
    """记忆类型（`docs/03` §2 的 ``type`` 注释）。

    **不采用** ``docs/schemas/assistant_response.schema.json`` 示例里的
    ``goal`` / ``fact``：那个文件自己标注了"只是预设示例"，而 ``docs/03`` 是
    数据模型规格。见 ADR 0018。
    """

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    EVENT = "event"


#: 未知类型降级到它（与 vocabulary 的"未知枚举 → 默认值"策略一致）。
DEFAULT_MEMORY_TYPE = MemoryType.SEMANTIC

#: 各类型的存活天数；``None`` 表示不过期（长期记忆）。
#: `docs/06` §8：episodic 是短期记忆，semantic 通常是长期记忆。
MEMORY_TTL_DAYS: dict[MemoryType, int | None] = {
    MemoryType.EPISODIC: 7,
    MemoryType.EVENT: 30,
    MemoryType.SEMANTIC: None,
    MemoryType.PREFERENCE: None,
    MemoryType.RELATIONSHIP: None,
}


@dataclass(frozen=True, slots=True)
class Memory:
    """一条持久化的记忆。字段与 `docs/03` §2 的 Memory 实体逐一对应。"""

    id: str
    user_id: str
    companion_id: str
    type: MemoryType
    content: str
    created_at: datetime
    importance: float = 0.0
    confidence: float = 0.0
    summary: str | None = None
    source_message_id: str | None = None
    source_event_id: str | None = None
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None
    archived_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_unit_range("importance", self.importance)
        _require_unit_range("confidence", self.confidence)

    @property
    def repetition(self) -> int:
        """该事实被重复观察到的次数（存在 ``metadata`` 里，不是独立列）。

        只用于 importance 启发式（`docs/06` §3 的 ``repetition`` 项），
        不需要独立查询，所以不占一列。
        """
        return _metadata_int(self.metadata, "repetition")

    @property
    def access_count(self) -> int:
        """被检索命中并注入上下文的次数（`docs/06` §4 的 ``access_frequency``）。

        同样存在 ``metadata`` 里，由仓储的 ``touch_accessed`` 用 ``json_set`` 自增。
        不占独立列的理由与 ``repetition`` 相同，而且 ``docs/03`` 的 Memory 实体
        本来就没有这个字段。
        """
        return _metadata_int(self.metadata, "access_count")

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def is_archived(self) -> bool:
        return self.archived_at is not None

    def is_retrievable(self, now: datetime) -> bool:
        """是否可被检索到：未归档且未过期（`docs/06` §7/§8）。"""
        return not self.is_archived() and not self.is_expired(now)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """LLM 从一轮对话里提取出的候选记忆。

    **只有 ``type`` 与 ``content`` 两个字段**——这是实测决定：让模型自评
    ``importance`` / ``confidence`` 会把每轮延迟从 +2.4s 推到 +4.5s，而且它给出的
    值会越界（实测 ``importance=10``）。打分交给 ``application/memory_scoring.py``
    的启发式（`docs/06` §3）。见 ADR 0018。
    """

    type: MemoryType
    content: str


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """一次检索命中。

    ``components`` 保留 ``docs/06`` §4 公式的各分项，供开发者控制台与日志观测
    "为什么这条被检索到"——只有总分的话，调权重就是盲调。
    """

    memory: Memory
    score: float
    components: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryFilter:
    """列表查询的过滤条件（`docs/04` §2 的 ``GET /v1/memories`` 五个参数）。

    单独抽成值对象而不是给仓储方法加 7 个关键字参数：协议签名才不会随
    过滤维度增长而失稳。
    """

    user_id: str
    companion_id: str
    types: tuple[MemoryType, ...] = ()
    min_importance: float | None = None
    created_after: datetime | None = None
    include_archived: bool = False
    include_expired: bool = False
    limit: int | None = None


def expiry_for(memory_type: MemoryType, created_at: datetime) -> datetime | None:
    """按 :data:`MEMORY_TTL_DAYS` 算出过期时刻；``None`` 表示不过期。

    创建与合并都走这个函数，TTL 策略才只有一处真相。
    """
    days = MEMORY_TTL_DAYS[memory_type]
    return None if days is None else created_at + timedelta(days=days)


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int:
    """从自由 JSON 里取一个非负整数，取不到或类型不对都当 0。

    ``metadata`` 是自由 JSON，不能相信它的类型；``isinstance(True, int)`` 又为真，
    所以 bool 必须先排除。
    """
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _require_unit_range(name: str, value: float) -> None:
    # NaN / Infinity 也要拦掉：它们能通过 `0 <= v <= 1` 的直觉检查，却会污染下游排序
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        msg = f"{name} must be a finite number within [0.0, 1.0], got {value!r}"
        raise ValueError(msg)
