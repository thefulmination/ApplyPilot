from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Iterator, Mapping
from decimal import Decimal, localcontext

import pytest

import applypilot.brain.parity as parity_module
from applypilot.brain.parity import (
    CanonicalizationError,
    ParityClaim,
    ParityVerificationError,
    SQLiteJSON,
    SQLiteTimestamp,
    Unresolved,
    canonical_bytes,
    compare_sorted,
    verify_parity,
)


class _AdversarialMapping(Mapping[str, str]):
    def __init__(self, count: int, value: str) -> None:
        self._count = count
        self._value = value
        self.values_read = 0

    def __getitem__(self, key: str) -> str:
        self.values_read += 1
        return self._value

    def __iter__(self) -> Iterator[str]:
        return (f"key-{index:08d}" for index in range(self._count))

    def __len__(self) -> int:
        return self._count


class _EncodeForbiddenString(str):
    def encode(self, *args, **kwargs):
        raise AssertionError("oversized string was encoded before its budget check")


class _CopyForbiddenBytearray(bytearray):
    def __bytes__(self) -> bytes:
        raise AssertionError("oversized bytearray was copied before its budget check")


def test_verifier_rejects_fabricated_equal_hashes() -> None:
    fabricated = "0" * 64
    claim = ParityClaim(
        source_count=1,
        target_count=1,
        source_hash=fabricated,
        target_hash=fabricated,
        mismatch_count=0,
        unresolved_count=0,
    )

    with pytest.raises(ParityVerificationError, match="independent recomputation"):
        verify_parity(iter([("job-1", {"title": "Engineer"})]), iter([("job-1", {"title": "Director"})]), claim)


def test_extra_and_missing_keys_are_each_counted_once() -> None:
    result = compare_sorted(
        iter([("a", {"value": 1}), ("c", {"value": 3})]),
        iter([("b", {"value": 2}), ("c", {"value": 3}), ("d", {"value": 4})]),
    )

    assert result.source_count == 2
    assert result.target_count == 3
    assert result.mismatch_count == 3
    assert result.unresolved_count == 0
    assert not result.passed


def test_same_key_with_different_content_is_a_mismatch() -> None:
    result = compare_sorted(iter([("a", {"value": 1})]), iter([("a", {"value": 2})]))

    assert result.source_count == result.target_count == 1
    assert result.source_hash != result.target_hash
    assert result.mismatch_count == 1
    assert not result.passed


def test_sqlite_and_postgres_numeric_and_timestamp_values_normalize_equally() -> None:
    eastern = timezone(timedelta(hours=-5))
    source = [
        (
            "job-1",
            {
                "integer": 1,
                "real": 10.500,
                "created_at": datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=eastern),
                "payload": [b"\x00\xff", {"score": Decimal("0.1000")}],
            },
        )
    ]
    target = [
        (
            "job-1",
            {
                "integer": Decimal("1.000"),
                "real": Decimal("10.5"),
                "created_at": datetime(2026, 1, 2, 8, 4, 5, 123456, tzinfo=timezone.utc),
                "payload": [memoryview(b"\x00\xff"), {"score": 0.1}],
            },
        )
    ]

    result = compare_sorted(iter(source), iter(target))

    assert canonical_bytes(source[0][1]) == canonical_bytes(target[0][1])
    assert result.source_hash == result.target_hash
    assert result.mismatch_count == result.unresolved_count == 0
    assert result.passed


def test_decimal_formatting_is_context_independent_and_lossless_beyond_28_digits() -> None:
    left = Decimal("1234567890123456789012345678.0000000000000000000000000001")
    right = Decimal("1234567890123456789012345678.0000000000000000000000000002")

    with localcontext() as context:
        context.prec = 3
        left_bytes = canonical_bytes(left)
        right_bytes = canonical_bytes(right)

    assert left_bytes != right_bytes
    result = compare_sorted(iter([("value", left)]), iter([("value", right)]))
    assert result.mismatch_count == 1
    assert result.source_hash != result.target_hash


def test_typed_sqlite_json_and_timestamp_match_postgres_values() -> None:
    source = [
        (
            "job-1",
            {
                "payload": SQLiteJSON('{"enabled":true,"nested":{"score":0.1000},"items":[1,null]}'),
                "created_at": SQLiteTimestamp("2026-01-02 03:04:05.123456-05:00"),
            },
        )
    ]
    target = [
        (
            "job-1",
            {
                "payload": {
                    "enabled": True,
                    "items": [Decimal("1"), None],
                    "nested": {"score": Decimal("0.1")},
                },
                "created_at": datetime(2026, 1, 2, 8, 4, 5, 123456, tzinfo=timezone.utc),
            },
        )
    ]

    assert compare_sorted(iter(source), iter(target)).passed


@pytest.mark.parametrize(
    "text,match",
    [
        ('{"key":1,"key":2}', "duplicate JSON key"),
        ('{"value":NaN}', "nonstandard JSON constant"),
        ('{"value":Infinity}', "nonstandard JSON constant"),
    ],
)
def test_typed_sqlite_json_rejects_ambiguous_or_nonstandard_input(text: str, match: str) -> None:
    with pytest.raises(CanonicalizationError, match=match):
        canonical_bytes(SQLiteJSON(text))


@pytest.mark.parametrize(
    "text,match",
    [
        ("2026-01-02T03:04:05", "timezone-aware"),
        ("not-a-timestamp", "invalid SQLite timestamp"),
        ("2026-13-02T03:04:05Z", "invalid SQLite timestamp"),
    ],
)
def test_typed_sqlite_timestamp_rejects_naive_or_invalid_input(text: str, match: str) -> None:
    with pytest.raises(CanonicalizationError, match=match):
        canonical_bytes(SQLiteTimestamp(text))


def test_framing_is_unambiguous() -> None:
    left = compare_sorted(iter([("ab", "c")]), iter([("ab", "c")]))
    right = compare_sorted(iter([("a", "bc")]), iter([("a", "bc")]))

    assert left.source_hash != right.source_hash


def test_unresolved_rows_are_reported_without_being_treated_as_equal() -> None:
    result = compare_sorted(iter([("a", Unresolved("source parse failure"))]), iter([("a", {"value": 1})]))

    assert result.mismatch_count == 1
    assert result.unresolved_count == 1
    assert not result.passed


def test_large_generators_are_merged_without_materialization() -> None:
    count = 100_000
    consumed = {"source": 0, "target": 0}

    def rows(side: str, other: str):
        for index in range(count):
            consumed[side] += 1
            if consumed[side] - consumed[other] > 2:
                raise AssertionError(f"{side} iterator was materialized or read too far ahead")
            yield (f"{index:06d}", {"value": index})

    result = compare_sorted(rows("source", "target"), rows("target", "source"))

    assert consumed == {"source": count, "target": count}
    assert result.source_count == result.target_count == count
    assert result.passed


def test_oversized_aggregate_aborts_during_recursive_budget_accounting() -> None:
    oversized = _AdversarialMapping(count=100_000, value="x" * 128)

    with pytest.raises(CanonicalizationError, match="256-byte bound"):
        compare_sorted(iter([("row", oversized)]), iter(()), max_row_bytes=256)

    assert oversized.values_read < 10


def test_oversized_string_is_rejected_before_utf8_allocation() -> None:
    oversized = _EncodeForbiddenString("x" * 100_000)

    with pytest.raises(CanonicalizationError, match="64-byte bound"):
        canonical_bytes(oversized, max_bytes=64)


def test_oversized_bytes_like_values_are_rejected_before_copy() -> None:
    guarded = _CopyForbiddenBytearray(b"x" * 100_000)

    with pytest.raises(CanonicalizationError, match="64-byte bound"):
        canonical_bytes(guarded, max_bytes=64)
    with pytest.raises(CanonicalizationError, match="64-byte bound"):
        canonical_bytes(memoryview(guarded), max_bytes=64)


def test_oversized_sqlite_json_is_rejected_before_full_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    loads_called = False

    def forbidden_loads(*args, **kwargs):
        nonlocal loads_called
        loads_called = True
        raise AssertionError("oversized SQLite JSON reached json.loads")

    monkeypatch.setattr(parity_module.json, "loads", forbidden_loads)

    with pytest.raises(CanonicalizationError, match="64-byte bound"):
        canonical_bytes(SQLiteJSON(" " * 100_000 + "{}"), max_bytes=64)

    assert not loads_called
