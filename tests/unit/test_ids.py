"""``astra.domain.ids`` 的单元测试。"""

from __future__ import annotations

import pytest

from astra.domain.ids import (
    CROCKFORD_ALPHABET,
    ULID_LENGTH,
    decode_ulid,
    encode_ulid,
    new_id,
    timestamp_ms_of,
)

_MS = 1_790_000_000_000


def test_length_is_26() -> None:
    assert len(new_id()) == ULID_LENGTH


def test_alphabet_is_crockford() -> None:
    for _ in range(50):
        assert set(new_id()) <= set(CROCKFORD_ALPHABET)


def test_alphabet_excludes_ambiguous_characters() -> None:
    for char in "ILOU":
        assert char not in CROCKFORD_ALPHABET


def test_encode_decode_roundtrip() -> None:
    value = encode_ulid(now_ms=_MS, randomness=12345)

    assert decode_ulid(value) == (_MS, 12345)


def test_timestamp_is_recoverable() -> None:
    assert timestamp_ms_of(encode_ulid(now_ms=_MS, randomness=0)) == _MS


def test_lexicographic_order_follows_timestamp() -> None:
    """ULID 的核心价值：字典序即时间序。"""
    early = encode_ulid(now_ms=_MS, randomness=(1 << 80) - 1)
    late = encode_ulid(now_ms=_MS + 1, randomness=0)

    assert early < late


def test_same_millisecond_orders_by_randomness() -> None:
    low = encode_ulid(now_ms=_MS, randomness=0)
    high = encode_ulid(now_ms=_MS, randomness=1)

    assert low < high


def test_new_id_is_monotonic_within_same_millisecond() -> None:
    """同一毫秒内连续生成必须严格递增（否则 ORDER BY id 不可靠）。"""
    from astra.domain.ids import _UlidGenerator

    generator = _UlidGenerator()
    ids = [generator.new(now_ms=_MS) for _ in range(200)]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_generator_handles_clock_rollback() -> None:
    """时钟回拨（NTP 调整 / 虚拟机挂起）不能让 ID 倒退。"""
    from astra.domain.ids import _UlidGenerator

    generator = _UlidGenerator()
    forward = generator.new(now_ms=_MS)
    backward = generator.new(now_ms=_MS - 5_000)

    assert backward > forward
    assert timestamp_ms_of(backward) == _MS


def test_new_id_is_unique_across_many_calls() -> None:
    ids = {new_id() for _ in range(2000)}

    assert len(ids) == 2000


def test_new_id_encodes_current_time() -> None:
    import time

    before = int(time.time() * 1000)
    value = new_id()
    after = int(time.time() * 1000)

    assert before <= timestamp_ms_of(value) <= after


@pytest.mark.parametrize("now_ms", [-1, (1 << 48)])
def test_encode_rejects_out_of_range_timestamp(now_ms: int) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        encode_ulid(now_ms=now_ms, randomness=0)


def test_encode_rejects_out_of_range_randomness() -> None:
    with pytest.raises(ValueError, match="randomness"):
        encode_ulid(now_ms=_MS, randomness=1 << 80)


@pytest.mark.parametrize("value", ["", "ABC", "0" * 25, "0" * 27])
def test_decode_rejects_wrong_length(value: str) -> None:
    with pytest.raises(ValueError, match="26 characters"):
        decode_ulid(value)


def test_decode_rejects_invalid_characters() -> None:
    with pytest.raises(ValueError, match="invalid Crockford"):
        decode_ulid("!" + "0" * 25)


def test_decode_tolerates_crockford_confusables() -> None:
    """Crockford 规范允许把 I/L 当 1、O 当 0。"""
    assert decode_ulid("I" + "0" * 25) == decode_ulid("1" + "0" * 25)
    assert decode_ulid("O" + "0" * 25) == decode_ulid("0" * 26)
