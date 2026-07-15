"""RFC 8785 canonical JSON and SHA-256 for canonical-brain contracts."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any

import rfc8785


_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _validate_unicode(value: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{path} contains a lone Unicode surrogate")
    return value


def _snapshot(value: Any, path: str, ancestors: set[int]) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _validate_unicode(value, path)
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE_INTEGER:
            return value
        try:
            converted = float(value)
        except OverflowError as exc:
            raise ValueError(f"{path} is outside the IEEE-754 double domain") from exc
        if not math.isfinite(converted):
            raise ValueError(f"{path} is outside the IEEE-754 double domain")
        if int(converted) != value:
            raise ValueError(f"{path} is not exactly representable as an IEEE-754 double")
        return converted
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a nonfinite number")
        if value == 0 and math.copysign(1.0, value) < 0:
            raise ValueError(f"{path} contains negative zero")
        return value

    identity = id(value)
    if identity in ancestors:
        raise ValueError(f"{path} contains a cycle")
    if isinstance(value, Mapping):
        ancestors.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} contains a non-string object key")
                _validate_unicode(key, f"{path} key")
                result[key] = _snapshot(item, f"{path}.{key}", ancestors)
            return result
        finally:
            ancestors.remove(identity)
    if isinstance(value, (list, tuple)):
        ancestors.add(identity)
        try:
            return [_snapshot(item, f"{path}[{index}]", ancestors) for index, item in enumerate(value)]
        finally:
            ancestors.remove(identity)
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def canonicalize_jcs_bytes(value: Any) -> bytes:
    """Return RFC 8785 UTF-8 bytes for the TS-compatible plain-data domain."""

    snapshot = _snapshot(value, "$", set())
    try:
        return rfc8785.dumps(snapshot)
    except rfc8785.CanonicalizationError as exc:
        raise ValueError(f"value cannot be canonicalized as RFC 8785 JCS: {exc}") from exc


def canonicalize_jcs(value: Any) -> str:
    """Return RFC 8785 canonical JSON text."""

    return canonicalize_jcs_bytes(value).decode("utf-8")


def jcs_sha256(value: Any) -> str:
    """Return lowercase SHA-256 over RFC 8785 canonical UTF-8 bytes."""

    return hashlib.sha256(canonicalize_jcs_bytes(value)).hexdigest()
