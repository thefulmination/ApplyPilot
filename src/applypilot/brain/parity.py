"""Deterministic, streaming parity primitives for canonical brain data."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


_HASH_DOMAIN = b"ApplyPilot canonical parity rows v1\x00"
_DEFAULT_MAX_ROW_BYTES = 16 * 1024 * 1024
_FRAME_BYTES = 9
_AWARE_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_NAIVE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$")


class CanonicalizationError(ValueError):
    """Raised when a value is outside the supported canonical domain."""


class ParityOrderError(ValueError):
    """Raised when an input is not strictly sorted by its canonical key."""


class ParityVerificationError(ValueError):
    """Raised when independently recomputed parity contradicts a supplied claim."""


@dataclass(frozen=True)
class Unresolved:
    """Explicitly mark a projected value that could not be resolved."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("an unresolved value requires a reason")


@dataclass(frozen=True)
class SQLiteJSON:
    """JSON text explicitly projected from a SQLite text column."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("SQLite JSON text must be a string")


@dataclass(frozen=True)
class SQLiteTimestamp:
    """Timezone-aware timestamp text explicitly projected from SQLite."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("SQLite timestamp text must be a string")


@dataclass(frozen=True)
class ParityClaim:
    """Persisted or externally supplied parity fields requiring verification."""

    source_count: int
    target_count: int
    source_hash: str
    target_hash: str
    mismatch_count: int
    unresolved_count: int


@dataclass(frozen=True)
class ParityResult:
    """Independently computed parity fields."""

    source_count: int
    target_count: int
    source_hash: str
    target_hash: str
    mismatch_count: int
    unresolved_count: int

    @property
    def passed(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.source_hash == self.target_hash
            and self.mismatch_count == 0
            and self.unresolved_count == 0
        )


@dataclass(frozen=True)
class _CanonicalItem:
    key: bytes
    value: bytes
    unresolved: bool


def _frame(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 1:
        raise AssertionError("canonical frame tags must be one byte")
    return tag + struct.pack(">Q", len(payload)) + payload


def _bounded_frame(tag: bytes, payload: bytes, max_bytes: int, bound: int) -> bytes:
    if _FRAME_BYTES + len(payload) > max_bytes:
        raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")
    return _frame(tag, payload)


def _utf8_length(value: str, max_bytes: int, bound: int) -> int:
    length = 0
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            width = 1
        elif codepoint <= 0x7FF:
            width = 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise CanonicalizationError("strings may not contain lone Unicode surrogates")
        elif codepoint <= 0xFFFF:
            width = 3
        else:
            width = 4
        length += width
        if length > max_bytes:
            raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")
    return length


def _decimal_text(number: Decimal, max_payload_bytes: int, bound: int) -> str:
    sign, raw_digits, exponent = number.as_tuple()
    digits = list(raw_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not any(digits):
        return "0"

    digit_count = len(digits)
    point = digit_count + exponent
    if exponent >= 0:
        text_length = digit_count + exponent
    elif point > 0:
        text_length = digit_count + 1
    else:
        text_length = 2 - point + digit_count
    text_length += sign
    if text_length > max_payload_bytes:
        raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")

    digit_text = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text = digit_text + ("0" * exponent)
    elif point > 0:
        text = digit_text[:point] + "." + digit_text[point:]
    else:
        text = "0." + ("0" * -point) + digit_text
    return ("-" if sign else "") + text


def _canonical_number(value: int | float | Decimal, max_bytes: int, bound: int) -> bytes:
    try:
        if isinstance(value, float):
            if not math.isfinite(value):
                raise CanonicalizationError("non-finite floats are not canonical")
            number = Decimal(str(value))
        else:
            number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CanonicalizationError(f"invalid numeric value: {value!r}") from exc
    if not number.is_finite():
        raise CanonicalizationError("non-finite decimals are not canonical")
    if max_bytes < _FRAME_BYTES:
        raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")
    text = _decimal_text(number, max_bytes - _FRAME_BYTES, bound)
    return _bounded_frame(b"n", text.encode("ascii"), max_bytes, bound)


def _reject_json_constant(value: str) -> None:
    raise CanonicalizationError(f"nonstandard JSON constant is forbidden: {value}")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError(f"duplicate JSON key is forbidden: {key!r}")
        result[key] = value
    return result


def _parse_sqlite_json(value: SQLiteJSON, max_bytes: int, bound: int) -> Any:
    _utf8_length(value.text, max_bytes, bound)
    try:
        return json.loads(
            value.text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_json_object,
        )
    except CanonicalizationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CanonicalizationError("invalid SQLite JSON text") from exc


def _parse_sqlite_timestamp(value: SQLiteTimestamp) -> datetime:
    if _NAIVE_TIMESTAMP.fullmatch(value.text):
        raise CanonicalizationError("SQLite timestamps must be timezone-aware")
    if _AWARE_TIMESTAMP.fullmatch(value.text) is None:
        raise CanonicalizationError("invalid SQLite timestamp text")
    normalized = value.text[:-1] + "+00:00" if value.text.endswith("Z") else value.text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CanonicalizationError("invalid SQLite timestamp text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalizationError("SQLite timestamps must be timezone-aware")
    return parsed


def _canonicalize(value: Any, ancestors: set[int], max_bytes: int, bound: int) -> tuple[bytes, bool]:
    if max_bytes < _FRAME_BYTES:
        raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")
    if isinstance(value, SQLiteJSON):
        return _canonicalize(_parse_sqlite_json(value, max_bytes, bound), ancestors, max_bytes, bound)
    if isinstance(value, SQLiteTimestamp):
        return _canonicalize(_parse_sqlite_timestamp(value), ancestors, max_bytes, bound)
    if value is None:
        return _bounded_frame(b"0", b"", max_bytes, bound), False
    if isinstance(value, bool):
        return _bounded_frame(b"b", b"1" if value else b"0", max_bytes, bound), False
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value, max_bytes, bound), False
    if isinstance(value, str):
        _utf8_length(value, max_bytes - _FRAME_BYTES, bound)
        try:
            payload = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("strings may not contain lone Unicode surrogates") from exc
        return _bounded_frame(b"s", payload, max_bytes, bound), False
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload_length = value.nbytes if isinstance(value, memoryview) else len(value)
        if _FRAME_BYTES + payload_length > max_bytes:
            raise CanonicalizationError(f"canonical value exceeds the {bound}-byte bound")
        payload = bytes(value)
        return _bounded_frame(b"y", payload, max_bytes, bound), False
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalizationError("datetimes must be timezone-aware")
        text = value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return _bounded_frame(b"t", text.encode("ascii"), max_bytes, bound), False
    if isinstance(value, Unresolved):
        reason, _ = _canonicalize(value.reason, ancestors, max_bytes - _FRAME_BYTES, bound)
        return _bounded_frame(b"u", reason, max_bytes, bound), True

    identity = id(value)
    if identity in ancestors:
        raise CanonicalizationError("canonical values may not contain cycles")
    if isinstance(value, Mapping):
        ancestors.add(identity)
        try:
            entries: list[tuple[bytes, bytes, bool]] = []
            used = _FRAME_BYTES
            for key, item in value.items():
                if not isinstance(key, str):
                    raise CanonicalizationError("mapping keys must be strings")
                key_limit = max_bytes - used - (3 * _FRAME_BYTES)
                encoded_key, _ = _canonicalize(key, ancestors, key_limit, bound)
                value_limit = max_bytes - used - (2 * _FRAME_BYTES) - len(encoded_key)
                encoded_value, unresolved = _canonicalize(item, ancestors, value_limit, bound)
                used += (2 * _FRAME_BYTES) + len(encoded_key) + len(encoded_value)
                entries.append((encoded_key, encoded_value, unresolved))
            entries.sort(key=lambda entry: entry[0])
            for first, second in zip(entries, entries[1:]):
                if first[0] == second[0]:
                    raise CanonicalizationError("mapping contains duplicate canonical keys")
            payload = bytearray()
            for encoded_key, encoded_value, _ in entries:
                payload.extend(_frame(b"k", encoded_key))
                payload.extend(_frame(b"v", encoded_value))
            return _bounded_frame(b"m", bytes(payload), max_bytes, bound), any(entry[2] for entry in entries)
        finally:
            ancestors.remove(identity)
    if isinstance(value, (list, tuple)):
        ancestors.add(identity)
        try:
            payload = bytearray()
            unresolved = False
            used = _FRAME_BYTES
            for item in value:
                encoded_item, item_unresolved = _canonicalize(
                    item,
                    ancestors,
                    max_bytes - used - _FRAME_BYTES,
                    bound,
                )
                payload.extend(_frame(b"i", encoded_item))
                used += _FRAME_BYTES + len(encoded_item)
                unresolved = unresolved or item_unresolved
            return _bounded_frame(b"l", bytes(payload), max_bytes, bound), unresolved
        finally:
            ancestors.remove(identity)
    raise CanonicalizationError(f"unsupported canonical type: {type(value).__name__}")


def canonical_bytes(value: Any, *, max_bytes: int = _DEFAULT_MAX_ROW_BYTES) -> bytes:
    """Normalize a supported value into deterministic, self-framing bytes."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    encoded, _ = _canonicalize(value, set(), max_bytes, max_bytes)
    return encoded


def canonical_sort_key(value: Any, *, max_bytes: int = _DEFAULT_MAX_ROW_BYTES) -> bytes:
    """Return the byte ordering that canonical key-sorted iterators must use."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    encoded, unresolved = _canonicalize(value, set(), max_bytes, max_bytes)
    if unresolved:
        raise CanonicalizationError("canonical keys may not be unresolved")
    return encoded


class _Stream:
    def __init__(self, rows: Iterable[tuple[Any, Any]], *, max_row_bytes: int) -> None:
        self._rows: Iterator[tuple[Any, Any]] = iter(rows)
        self._previous_key: bytes | None = None
        self._max_row_bytes = max_row_bytes
        self.count = 0
        self.digest = hashlib.sha256(_HASH_DOMAIN)

    def next(self) -> _CanonicalItem | None:
        try:
            row = next(self._rows)
        except StopIteration:
            return None
        if not isinstance(row, tuple) or len(row) != 2:
            raise TypeError("parity iterators must yield (key, value) tuples")
        key, value = row
        key_bytes, key_unresolved = _canonicalize(
            key,
            set(),
            self._max_row_bytes - (4 * _FRAME_BYTES),
            self._max_row_bytes,
        )
        if key_unresolved:
            raise CanonicalizationError("canonical keys may not be unresolved")
        if self._previous_key is not None and key_bytes <= self._previous_key:
            raise ParityOrderError("parity keys must be strictly increasing in canonical byte order")
        value_limit = self._max_row_bytes - (3 * _FRAME_BYTES) - len(key_bytes)
        value_bytes, unresolved = _canonicalize(value, set(), value_limit, self._max_row_bytes)
        row_payload_bytes = (2 * _FRAME_BYTES) + len(key_bytes) + len(value_bytes)
        self.digest.update(b"r" + struct.pack(">Q", row_payload_bytes))
        self.digest.update(b"k" + struct.pack(">Q", len(key_bytes)))
        self.digest.update(key_bytes)
        self.digest.update(b"v" + struct.pack(">Q", len(value_bytes)))
        self.digest.update(value_bytes)
        self._previous_key = key_bytes
        self.count += 1
        return _CanonicalItem(key_bytes, value_bytes, unresolved)


def compare_sorted(
    source_rows: Iterable[tuple[Any, Any]],
    target_rows: Iterable[tuple[Any, Any]],
    *,
    max_row_bytes: int = _DEFAULT_MAX_ROW_BYTES,
) -> ParityResult:
    """Merge and compare two canonical key-sorted streams using bounded memory."""

    if max_row_bytes <= 0:
        raise ValueError("max_row_bytes must be positive")
    source = _Stream(source_rows, max_row_bytes=max_row_bytes)
    target = _Stream(target_rows, max_row_bytes=max_row_bytes)
    source_item = source.next()
    target_item = target.next()
    mismatches = 0
    unresolved = 0

    while source_item is not None or target_item is not None:
        if target_item is None or (source_item is not None and source_item.key < target_item.key):
            mismatches += 1
            unresolved += int(source_item.unresolved)
            source_item = source.next()
        elif source_item is None or target_item.key < source_item.key:
            mismatches += 1
            unresolved += int(target_item.unresolved)
            target_item = target.next()
        else:
            row_unresolved = source_item.unresolved or target_item.unresolved
            unresolved += int(row_unresolved)
            mismatches += int(row_unresolved or source_item.value != target_item.value)
            source_item = source.next()
            target_item = target.next()

    return ParityResult(
        source_count=source.count,
        target_count=target.count,
        source_hash=source.digest.hexdigest(),
        target_hash=target.digest.hexdigest(),
        mismatch_count=mismatches,
        unresolved_count=unresolved,
    )


def verify_parity(
    source_rows: Iterable[tuple[Any, Any]],
    target_rows: Iterable[tuple[Any, Any]],
    claim: ParityClaim,
    *,
    max_row_bytes: int = _DEFAULT_MAX_ROW_BYTES,
) -> ParityResult:
    """Independently recompute parity and reject any contradictory supplied field."""

    actual = compare_sorted(source_rows, target_rows, max_row_bytes=max_row_bytes)
    differences = [
        field.name
        for field in fields(ParityClaim)
        if getattr(claim, field.name) != getattr(actual, field.name)
    ]
    if differences:
        raise ParityVerificationError(
            "supplied parity claim failed independent recomputation: " + ", ".join(differences)
        )
    return actual
