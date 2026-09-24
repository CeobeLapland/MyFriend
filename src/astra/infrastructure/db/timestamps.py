"""SQLite 时间戳的编解码（``infrastructure/db`` 内共享）。

时间戳一律按 ISO-8601 **UTC** 字符串存取，不使用 ``sqlite3`` 的 ``detect_types``
自动转换：显式转换让"库里到底存了什么"一眼可见，也避免依赖 sqlite3 的隐式时区假设。

``to_iso`` **拒绝 naive datetime**：不带时区的时间戳落库后是有歧义的（同一串字符
可能表示两个不同的时刻），而 SQLite 不会替我们报错。这条检查只在写入侧有意义，
所以放在这里而不是 ``domain``——领域层不该知道存储格式。
"""

from __future__ import annotations

from datetime import UTC, datetime


def to_iso(value: datetime) -> str:
    """序列化为 UTC ISO-8601。

    Raises:
        ValueError: ``value`` 是 naive datetime。
    """
    if value.tzinfo is None:
        msg = "datetime must be timezone-aware; naive timestamps would be ambiguous in the DB"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    """反序列化。``fromisoformat`` 会保留字符串里的偏移量。"""
    return datetime.fromisoformat(value)
