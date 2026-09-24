"""ID 生成：ULID（Universally Unique Lexicographically Sortable Identifier）。

**为什么不用 `uuid4`**：本机时钟分辨率实测为 15.625ms（3000 次 `datetime.now()`
只有 1 个唯一值），同一个 turn 的两条消息 `created_at` 完全相同，
`ORDER BY created_at` 没有区分度。ULID 把毫秒时间戳编进前 10 个字符，
字典序即时间序，因此 `ORDER BY id` 才是可靠的排序依据。

**为什么不用第三方实现**：`python-ulid` 是 35 行可自测的代码换一个依赖；
`uuid.uuid7()` 要 Python 3.13（本项目锁 3.12）。

**关于模块级状态**：`_GENERATOR` 持有一个锁和两个整数，用于实现 ULID 规范的
「同一毫秒内单调递增」模式。这是本模块唯一的状态，它保证同一毫秒内连续生成的
ID 仍按生成顺序排列——没有它，同一毫秒内的两个 ID 会按随机位排序，快速连续写入
和测试都会变得不确定。时钟回拨（NTP 调整、虚拟机挂起）也由同一逻辑兜住。
"""

from __future__ import annotations

import secrets
import threading
import time

#: Crockford Base32：去掉了易混淆的 I / L / O / U
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ULID_LENGTH = 26
_TIME_CHARS = 10
_RANDOM_CHARS = 16
_MAX_TIMESTAMP_MS = (1 << 48) - 1
_MAX_RANDOMNESS = (1 << 80) - 1

_ALPHABET_INDEX: dict[str, int] = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}
# 解码时宽容处理 Crockford 规定可互换的字符
_ALPHABET_INDEX.update({"I": 1, "L": 1, "O": 0, "U": 0})


def encode_ulid(*, now_ms: int, randomness: int) -> str:
    """把时间戳与随机部分编码为 26 字符的 ULID。"""
    if not 0 <= now_ms <= _MAX_TIMESTAMP_MS:
        msg = f"timestamp must be within [0, {_MAX_TIMESTAMP_MS}], got {now_ms}"
        raise ValueError(msg)
    if not 0 <= randomness <= _MAX_RANDOMNESS:
        msg = f"randomness must be within [0, {_MAX_RANDOMNESS}], got {randomness}"
        raise ValueError(msg)

    return _encode_base32(now_ms, _TIME_CHARS) + _encode_base32(randomness, _RANDOM_CHARS)


def decode_ulid(value: str) -> tuple[int, int]:
    """把 ULID 解回 ``(now_ms, randomness)``。"""
    if len(value) != ULID_LENGTH:
        msg = f"ULID must be {ULID_LENGTH} characters, got {len(value)}"
        raise ValueError(msg)

    return (
        _decode_base32(value[:_TIME_CHARS]),
        _decode_base32(value[_TIME_CHARS:]),
    )


def timestamp_ms_of(value: str) -> int:
    """取出 ULID 里编码的毫秒时间戳。"""
    return decode_ulid(value)[0]


def new_id() -> str:
    """生成一个新的 ULID（同一毫秒内单调递增）。"""
    return _GENERATOR.new()


def _encode_base32(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _decode_base32(value: str) -> int:
    result = 0
    for char in value:
        index = _ALPHABET_INDEX.get(char.upper())
        if index is None:
            msg = f"invalid Crockford Base32 character: {char!r}"
            raise ValueError(msg)
        result = (result << 5) | index
    return result


def _now_ms() -> int:
    return int(time.time() * 1000)


class _UlidGenerator:
    """带单调性保证的 ULID 生成器。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_randomness = 0

    def new(self, *, now_ms: int | None = None) -> str:
        with self._lock:
            current_ms = _now_ms() if now_ms is None else now_ms

            if current_ms <= self._last_ms:
                # 同一毫秒内连续生成，或系统时钟回拨：保持时间戳、递增随机部分，
                # 从而保证字典序仍等于生成顺序。
                current_ms = self._last_ms
                self._last_randomness += 1
            else:
                self._last_ms = current_ms
                self._last_randomness = secrets.randbits(80)

            return encode_ulid(now_ms=current_ms, randomness=self._last_randomness)


_GENERATOR = _UlidGenerator()
