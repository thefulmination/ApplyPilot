"""Read deterministic, resumable batches from an immutable SQLite backup."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import quote

from applypilot.brain.canonical_hash import canonicalize_jcs, canonicalize_jcs_bytes, jcs_sha256


MANIFEST_VERSION = 2
CHECKPOINT_VERSION = 1
BATCH_VERSION = 1
_MAX_BATCH_SIZE = 100_000
_MAX_CURSOR_SAFE_INTEGER = 2**53 - 1
DEFAULT_MAX_BATCH_BYTES = 16 * 1024 * 1024
SEALED_SNAPSHOT_VERSION = 1
DEFAULT_MANIFEST_CURSOR_MAX_BYTES = 64 * 1024
ONLINE_BACKUP_MODE = "online_backup"
IMMUTABLE_NO_FILESYSTEM_WRITE_MODE = "immutable_no_filesystem_write"
_SOURCE_MODES = {ONLINE_BACKUP_MODE, IMMUTABLE_NO_FILESYSTEM_WRITE_MODE}


class SnapshotError(RuntimeError):
    """Base error for an unusable migration snapshot."""


class SnapshotChangedError(SnapshotError):
    """The bound source path no longer identifies the captured snapshot."""


class SourceSchemaError(SnapshotError):
    """The backup does not satisfy the known source schema contract."""


class OversizedManifestCursorError(SnapshotError):
    """A source key cannot be safely materialized into a manifest cursor."""

    def __init__(self, source_table: str, key_upper_bound_bytes: int, cursor_max_bytes: int) -> None:
        self.source_table = source_table
        self.key_upper_bound_bytes = key_upper_bound_bytes
        self.cursor_max_bytes = cursor_max_bytes
        self.quarantine_contract = _freeze_value(
            {
                "reason_code": "source_key_exceeds_manifest_cursor_cap",
                "source_table": source_table,
                "key_upper_bound_bytes": key_upper_bound_bytes,
                "manifest_cursor_max_bytes": cursor_max_bytes,
            }
        )
        super().__init__(
            f"source key in {source_table} is bounded at {key_upper_bound_bytes} bytes; "
            f"manifest cursor cap is {cursor_max_bytes}"
        )


class OversizedSourceRowError(SnapshotError):
    """A source row cannot fit in an otherwise empty byte-bounded batch."""

    def __init__(
        self,
        source_table: str,
        source_locator: Mapping[str, Any],
        canonical_row_bytes: int,
        max_batch_bytes: int,
        *,
        size_measurement: str = "exact",
    ) -> None:
        self.source_table = source_table
        self.source_locator = _freeze_value(source_locator)
        self.canonical_row_bytes = canonical_row_bytes
        self.max_batch_bytes = max_batch_bytes
        self.size_measurement = size_measurement
        self.quarantine_contract = _freeze_value(
            {
                "reason_code": "source_row_exceeds_batch_byte_limit",
                "source_table": source_table,
                "source_locator": self.source_locator,
                "canonical_row_bytes": canonical_row_bytes,
                "max_batch_bytes": max_batch_bytes,
                "size_measurement": size_measurement,
            }
        )
        super().__init__(
            f"source row in {source_table} at {dict(source_locator)!r} is bounded at "
            f"{canonical_row_bytes} canonical bytes; "
            f"batch limit is {max_batch_bytes}"
        )


@dataclass(frozen=True, slots=True)
class TextCursor:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("text cursor value must be a string")


@dataclass(frozen=True, slots=True)
class IntegerCursor:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("integer cursor value must be an integer")
        _validate_cursor_integer(self.value)


@dataclass(frozen=True, slots=True)
class CompositeCursor:
    values: tuple[str | int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.values, tuple) or not self.values:
            raise TypeError("composite cursor values must be a non-empty tuple")
        if any(isinstance(value, bool) or not isinstance(value, (str, int)) for value in self.values):
            raise TypeError("composite cursor values must contain only strings or integers")
        for value in self.values:
            if isinstance(value, int):
                _validate_cursor_integer(value)


Cursor = TextCursor | IntegerCursor | CompositeCursor


@dataclass(frozen=True, slots=True)
class SourceTableSpec:
    name: str
    key_columns: tuple[str, ...]
    key_types: tuple[type[str] | type[int], ...]


# All 14 tables in the immutable source checkpoint are captured, including the
# advisory research_scores table, so source accounting remains exact.
SOURCE_TABLES: tuple[SourceTableSpec, ...] = (
    SourceTableSpec("jobs", ("url",), (str,)),
    SourceTableSpec("applications", ("id",), (int,)),
    SourceTableSpec("application_events", ("id",), (int,)),
    SourceTableSpec("email_events", ("message_id",), (str,)),
    SourceTableSpec("email_event_reviews", ("id",), (int,)),
    SourceTableSpec("reviewed_outcomes", ("event_id", "job_url"), (str, str)),
    SourceTableSpec("research_labels", ("id",), (str,)),
    SourceTableSpec("research_label_confidence", ("label_id",), (str,)),
    SourceTableSpec("research_pairwise_labels", ("id",), (str,)),
    SourceTableSpec("research_kg_artifacts", ("kg_version",), (str,)),
    SourceTableSpec("research_kg_runs", ("kg_version",), (str,)),
    SourceTableSpec("research_scores", ("id",), (int,)),
    SourceTableSpec("decision_policy_versions", ("policy_version",), (str,)),
    SourceTableSpec("job_decisions", ("decision_id",), (str,)),
)
_TABLE_BY_NAME = {table.name: table for table in SOURCE_TABLES}


def _validate_cursor_integer(value: int) -> None:
    if abs(value) > _MAX_CURSOR_SAFE_INTEGER:
        raise ValueError("cursor integer must be within the JavaScript safe integer range")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _validate_cursor_value(table: SourceTableSpec, cursor: Cursor | None) -> None:
    if cursor is None:
        return
    if len(table.key_columns) == 1:
        expected = TextCursor if table.key_types == (str,) else IntegerCursor
        if not isinstance(cursor, expected):
            raise ValueError(f"cursor for {table.name} must be {expected.__name__}")
        if isinstance(cursor, IntegerCursor):
            _validate_cursor_integer(cursor.value)
        return
    if not isinstance(cursor, CompositeCursor) or len(cursor.values) != len(table.key_columns):
        raise ValueError(f"cursor for {table.name} must have {len(table.key_columns)} composite values")
    for value, expected_type in zip(cursor.values, table.key_types, strict=True):
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise ValueError(f"cursor for {table.name} has an invalid composite value type")
        if isinstance(value, int):
            _validate_cursor_integer(value)


def _cursor_key(cursor: Cursor) -> tuple[str | int, ...]:
    if isinstance(cursor, CompositeCursor):
        return cursor.values
    return (cursor.value,)


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, TextCursor):
        return {"kind": "text", "value": value.value}
    if isinstance(value, IntegerCursor):
        return {"kind": "integer", "value": value.value}
    if isinstance(value, CompositeCursor):
        return {"kind": "composite", "values": list(value.values)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not canonical-JSON serializable")


def canonical_json(value: Any) -> str:
    """Serialize importer contract values using RFC 8785 JCS."""

    return canonicalize_jcs(_json_value(value))


def _canonical_sha256(value: Any) -> str:
    return jcs_sha256(_json_value(value))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _prevalidate_encoded_cursors(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _prevalidate_encoded_cursors(item)
        return
    if not isinstance(value, dict):
        return
    kind = value.get("kind")
    if kind == "integer":
        cursor_value = value.get("value")
        if isinstance(cursor_value, int) and not isinstance(cursor_value, bool):
            _validate_cursor_integer(cursor_value)
    elif kind == "composite":
        values = value.get("values")
        if isinstance(values, list):
            for item in values:
                if isinstance(item, int) and not isinstance(item, bool):
                    _validate_cursor_integer(item)
    for item in value.values():
        _prevalidate_encoded_cursors(item)


def _strict_json(payload: str, expected_sha256: str, *, cursor_contract: bool = False) -> Any:
    if not isinstance(payload, str):
        raise TypeError("canonical JSON payload must be a string")
    if not _is_sha256(expected_sha256):
        raise ValueError("expected canonical JSON SHA-256 is invalid")
    try:
        value = json.loads(payload, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid canonical JSON") from exc
    if cursor_contract:
        _prevalidate_encoded_cursors(value)
    if canonical_json(value) != payload:
        raise ValueError("JSON payload is not in canonical encoding")
    actual_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f"canonical JSON SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    return value


def _require_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise ValueError(f"{label} fields mismatch; missing={missing}, unknown={unknown}")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _decode_cursor_object(value: Any) -> Cursor | None:
    if value is None:
        return None
    if not isinstance(value, dict) or "kind" not in value:
        raise ValueError("cursor must be null or an object with a kind")
    kind = value["kind"]
    if kind == "text":
        fields = _require_fields(value, {"kind", "value"}, "text cursor")
        if not isinstance(fields["value"], str):
            raise ValueError("text cursor value must be a string")
        return TextCursor(fields["value"])
    if kind == "integer":
        fields = _require_fields(value, {"kind", "value"}, "integer cursor")
        if isinstance(fields["value"], bool) or not isinstance(fields["value"], int):
            raise ValueError("integer cursor value must be an integer")
        return IntegerCursor(fields["value"])
    if kind == "composite":
        fields = _require_fields(value, {"kind", "values"}, "composite cursor")
        values = fields["values"]
        if not isinstance(values, list) or not values:
            raise ValueError("composite cursor values must be a non-empty array")
        if any(isinstance(item, bool) or not isinstance(item, (str, int)) for item in values):
            raise ValueError("composite cursor values must contain only strings or integers")
        return CompositeCursor(tuple(values))
    raise ValueError(f"unknown cursor kind: {kind!r}")


def cursor_from_canonical_json(payload: str, expected_sha256: str, source_table: str) -> Cursor:
    """Strictly decode and table-validate one canonical cursor payload."""

    table = _TABLE_BY_NAME.get(source_table)
    if table is None:
        raise ValueError(f"unknown source table: {source_table}")
    cursor = _decode_cursor_object(_strict_json(payload, expected_sha256, cursor_contract=True))
    if cursor is None:
        raise ValueError("cursor payload cannot be null")
    _validate_cursor_value(table, cursor)
    return cursor


@dataclass(frozen=True, slots=True)
class SourceFileAudit:
    path: str
    before_exists: bool
    after_exists: bool
    before_sha256: str | None
    after_sha256: str | None
    before_size: int | None
    after_size: int | None
    before_mtime_ns: int | None
    after_mtime_ns: int | None
    before_stat_identity: tuple[int, int, int, int, int] | None
    after_stat_identity: tuple[int, int, int, int, int] | None
    changed: bool
    observation_complete: bool
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class SealedSnapshotReceipt:
    """Proof that a private SQLite backup was completed and durably published."""

    version: int
    path: str
    source_path: str
    sha256: str
    size: int
    quick_check: str
    source_mode: str
    source_db_audit: SourceFileAudit
    source_wal_audit: SourceFileAudit
    source_shm_audit: SourceFileAudit
    source_changed_during_backup: bool

    def __post_init__(self) -> None:
        if self.version != SEALED_SNAPSHOT_VERSION:
            raise ValueError(f"unsupported sealed snapshot receipt version: {self.version}")
        for value, label in ((self.path, "sealed path"), (self.source_path, "source path")):
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise ValueError(f"{label} must be a non-empty absolute path")
        if Path(self.path) == Path(self.source_path):
            raise ValueError("sealed destination must differ from the live source")
        if not _is_sha256(self.sha256):
            raise ValueError("sealed snapshot SHA-256 is invalid")
        _require_int(self.size, "sealed snapshot size", minimum=1)
        if self.quick_check != "ok":
            raise ValueError("sealed snapshot quick_check must be 'ok'")
        if self.source_mode not in _SOURCE_MODES:
            raise ValueError(f"unsupported source mode: {self.source_mode!r}")
        for audit, label in (
            (self.source_db_audit, "source DB audit"),
            (self.source_wal_audit, "source WAL audit"),
            (self.source_shm_audit, "source SHM audit"),
        ):
            if not isinstance(audit, SourceFileAudit):
                raise TypeError(f"{label} must be a SourceFileAudit")
        if not isinstance(self.source_changed_during_backup, bool):
            raise TypeError("source change observation must be boolean")


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    path: str
    sha256: str
    size: int
    quick_check: str
    page_count: int
    page_size: int
    schema_version: int
    user_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or not Path(self.path).is_absolute():
            raise ValueError("source path must be a non-empty absolute path")
        if not _is_sha256(self.sha256):
            raise ValueError("source SHA-256 is invalid")
        _require_int(self.size, "source size", minimum=1)
        if self.quick_check != "ok":
            raise ValueError("source quick_check must be 'ok'")
        _require_int(self.page_count, "source page_count", minimum=1)
        _require_int(self.page_size, "source page_size", minimum=1)
        _require_int(self.schema_version, "source schema_version")
        _require_int(self.user_version, "source user_version")

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "quick_check": self.quick_check,
            "page_count": self.page_count,
            "page_size": self.page_size,
            "schema_version": self.schema_version,
            "user_version": self.user_version,
        }


@dataclass(frozen=True, slots=True)
class TableManifest:
    name: str
    key_columns: tuple[str, ...]
    row_count: int
    upper_bound: Cursor | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ValueError("source table name must be a string")
        table = _TABLE_BY_NAME.get(self.name)
        if table is None:
            raise ValueError(f"unknown source table: {self.name}")
        if self.key_columns != table.key_columns:
            raise ValueError(f"key columns for {self.name} do not match the source contract")
        _require_int(self.row_count, f"row count for {self.name}")
        _validate_cursor_value(table, self.upper_bound)
        if (self.row_count == 0) != (self.upper_bound is None):
            raise ValueError(f"row count and upper bound for {self.name} are inconsistent")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "key_columns": self.key_columns,
            "row_count": self.row_count,
            "upper_bound": self.upper_bound,
        }


@dataclass(frozen=True, slots=True)
class SourceManifest:
    version: int
    source: SourceFingerprint
    tables: tuple[TableManifest, ...]
    manifest_cursor_max_bytes: int
    _canonical_json_cache: str = field(init=False, repr=False, compare=False)
    _sha256_cache: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version: {self.version}")
        if not isinstance(self.source, SourceFingerprint):
            raise TypeError("manifest source must be a SourceFingerprint")
        if not isinstance(self.tables, tuple):
            raise TypeError("manifest tables must be a tuple")
        _require_int(self.manifest_cursor_max_bytes, "manifest cursor max bytes", minimum=1)
        expected_names = tuple(table.name for table in SOURCE_TABLES)
        actual_names = tuple(table.name for table in self.tables)
        if actual_names != expected_names:
            raise ValueError(f"manifest tables must exactly match {expected_names!r}")
        encoded = canonical_json(self.as_dict())
        object.__setattr__(self, "_canonical_json_cache", encoded)
        object.__setattr__(self, "_sha256_cache", hashlib.sha256(encoded.encode("utf-8")).hexdigest())

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source.as_dict(),
            "tables": tuple(table.as_dict() for table in self.tables),
            "manifest_cursor_max_bytes": self.manifest_cursor_max_bytes,
        }

    @property
    def canonical_json(self) -> str:
        return self._canonical_json_cache

    @property
    def sha256(self) -> str:
        return self._sha256_cache

    def table(self, name: str) -> TableManifest:
        for table in self.tables:
            if table.name == name:
                return table
        raise ValueError(f"unknown source table: {name}")

    @classmethod
    def from_canonical_json(cls, payload: str, expected_sha256: str) -> SourceManifest:
        root = _require_fields(
            _strict_json(payload, expected_sha256, cursor_contract=True),
            {"version", "source", "tables", "manifest_cursor_max_bytes"},
            "source manifest",
        )
        version = _require_int(root["version"], "manifest version", minimum=1)
        source_data = _require_fields(
            root["source"],
            {
                "path",
                "sha256",
                "size",
                "quick_check",
                "page_count",
                "page_size",
                "schema_version",
                "user_version",
            },
            "source fingerprint",
        )
        source = SourceFingerprint(
            path=source_data["path"],
            sha256=source_data["sha256"],
            size=source_data["size"],
            quick_check=source_data["quick_check"],
            page_count=source_data["page_count"],
            page_size=source_data["page_size"],
            schema_version=source_data["schema_version"],
            user_version=source_data["user_version"],
        )
        if not isinstance(root["tables"], list):
            raise ValueError("manifest tables must be an array")
        tables: list[TableManifest] = []
        for item in root["tables"]:
            table_data = _require_fields(
                item,
                {"name", "key_columns", "row_count", "upper_bound"},
                "table manifest",
            )
            if not isinstance(table_data["key_columns"], list) or not all(
                isinstance(column, str) for column in table_data["key_columns"]
            ):
                raise ValueError("table key_columns must be an array of strings")
            tables.append(
                TableManifest(
                    name=table_data["name"],
                    key_columns=tuple(table_data["key_columns"]),
                    row_count=table_data["row_count"],
                    upper_bound=_decode_cursor_object(table_data["upper_bound"]),
                )
            )
        return cls(
            version=version,
            source=source,
            tables=tuple(tables),
            manifest_cursor_max_bytes=_require_int(
                root["manifest_cursor_max_bytes"], "manifest cursor max bytes", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class BatchRequest:
    manifest_sha256: str
    source_table: str
    after: Cursor | None
    batch_size: int
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES

    def __post_init__(self) -> None:
        if not _is_sha256(self.manifest_sha256):
            raise ValueError("manifest SHA-256 must be 64 lowercase hexadecimal characters")
        if not isinstance(self.source_table, str) or not self.source_table:
            raise ValueError("source table must be a non-empty string")
        table = _TABLE_BY_NAME.get(self.source_table)
        if table is not None:
            _validate_cursor_value(table, self.after)
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int):
            raise ValueError("batch size must be an integer")
        if self.batch_size < 1 or self.batch_size > _MAX_BATCH_SIZE:
            raise ValueError(f"batch size must be between 1 and {_MAX_BATCH_SIZE}")
        if isinstance(self.max_batch_bytes, bool) or not isinstance(self.max_batch_bytes, int):
            raise ValueError("max batch bytes must be an integer")
        if self.max_batch_bytes < 2:
            raise ValueError("max batch bytes must be at least 2 for an empty JSON array")

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_sha256": self.manifest_sha256,
            "source_table": self.source_table,
            "after": self.after,
            "batch_size": self.batch_size,
            "max_batch_bytes": self.max_batch_bytes,
        }


@dataclass(frozen=True, slots=True)
class SourceBatch:
    version: int
    request: BatchRequest
    upper_bound: Cursor | None
    rows: tuple[Mapping[str, Any], ...]
    next_cursor: Cursor | None
    canonical_byte_count: int
    rows_sha256: str
    identity_sha256: str


def _batch_identity_payload(
    request: BatchRequest,
    upper_bound: Cursor | None,
    next_cursor: Cursor | None,
    row_count: int,
    canonical_byte_count: int,
    rows_sha256: str,
) -> dict[str, Any]:
    return {
        "version": BATCH_VERSION,
        "request": request.as_dict(),
        "upper_bound": upper_bound,
        "next_cursor": next_cursor,
        "row_count": row_count,
        "canonical_byte_count": canonical_byte_count,
        "rows_sha256": rows_sha256,
    }


@dataclass(frozen=True, slots=True)
class ImportCheckpoint:
    version: int
    manifest_sha256: str
    source_table: str
    previous_cursor: Cursor | None
    cursor: Cursor | None
    batch_size: int
    max_batch_bytes: int
    upper_bound: Cursor | None
    row_count: int
    canonical_byte_count: int
    batch_identity_sha256: str
    rows_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version: {self.version}")
        if not _is_sha256(self.manifest_sha256):
            raise ValueError("checkpoint manifest SHA-256 is invalid")
        if not _is_sha256(self.batch_identity_sha256):
            raise ValueError("checkpoint batch identity SHA-256 is invalid")
        if not _is_sha256(self.rows_sha256):
            raise ValueError("checkpoint rows SHA-256 is invalid")
        if not isinstance(self.source_table, str):
            raise ValueError("checkpoint source table must be a string")
        table = _TABLE_BY_NAME.get(self.source_table)
        if table is None:
            raise ValueError(f"unknown source table: {self.source_table}")
        _validate_cursor_value(table, self.previous_cursor)
        _validate_cursor_value(table, self.cursor)
        _validate_cursor_value(table, self.upper_bound)
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int):
            raise ValueError("checkpoint batch size must be an integer")
        if self.batch_size < 1 or self.batch_size > _MAX_BATCH_SIZE:
            raise ValueError(f"checkpoint batch size must be between 1 and {_MAX_BATCH_SIZE}")
        if isinstance(self.max_batch_bytes, bool) or not isinstance(self.max_batch_bytes, int):
            raise ValueError("checkpoint max batch bytes must be an integer")
        if self.max_batch_bytes < 2:
            raise ValueError("checkpoint max batch bytes must be at least 2")
        _require_int(self.row_count, "checkpoint row_count")
        _require_int(self.canonical_byte_count, "checkpoint canonical_byte_count", minimum=2)
        if self.canonical_byte_count > self.max_batch_bytes:
            raise ValueError("checkpoint canonical byte count exceeds its byte limit")
        if self.row_count == 0 and self.canonical_byte_count != 2:
            raise ValueError("empty checkpoint canonical byte count must describe []")
        if self.row_count > self.batch_size:
            raise ValueError("checkpoint row_count cannot exceed its batch size")
        if self.row_count > 0 and self.cursor is None:
            raise ValueError("non-empty checkpoint must have a cursor")
        if self.upper_bound is None and self.row_count > 0:
            raise ValueError("non-empty checkpoint must have an upper bound")
        if self.upper_bound is None:
            if self.previous_cursor is not None or self.cursor is not None:
                raise ValueError("checkpoint cursors require a table upper bound")
        else:
            upper_key = _cursor_key(self.upper_bound)
            if self.previous_cursor is not None and _cursor_key(self.previous_cursor) > upper_key:
                raise ValueError("checkpoint previous cursor exceeds the table upper bound")
            if self.cursor is not None and _cursor_key(self.cursor) > upper_key:
                raise ValueError("checkpoint cursor exceeds the table upper bound")
        if self.previous_cursor is not None and self.cursor is not None:
            previous_key = _cursor_key(self.previous_cursor)
            cursor_key = _cursor_key(self.cursor)
            if cursor_key < previous_key:
                raise ValueError("checkpoint cursor cannot move backwards")
            if self.row_count > 0 and cursor_key == previous_key:
                raise ValueError("non-empty checkpoint cursor must advance")
        if self.row_count == 0 and self.cursor != self.previous_cursor:
            raise ValueError("empty checkpoint cursor must equal its previous cursor")
        request = BatchRequest(
            self.manifest_sha256,
            self.source_table,
            self.previous_cursor,
            self.batch_size,
            self.max_batch_bytes,
        )
        expected_identity = _canonical_sha256(
            _batch_identity_payload(
                request,
                self.upper_bound,
                self.cursor,
                self.row_count,
                self.canonical_byte_count,
                self.rows_sha256,
            )
        )
        if self.batch_identity_sha256 != expected_identity:
            raise ValueError("checkpoint batch identity SHA-256 does not match its batch receipt")

    @classmethod
    def from_batch(cls, batch: SourceBatch) -> ImportCheckpoint:
        return cls(
            version=CHECKPOINT_VERSION,
            manifest_sha256=batch.request.manifest_sha256,
            source_table=batch.request.source_table,
            previous_cursor=batch.request.after,
            cursor=batch.next_cursor,
            batch_size=batch.request.batch_size,
            max_batch_bytes=batch.request.max_batch_bytes,
            upper_bound=batch.upper_bound,
            row_count=len(batch.rows),
            canonical_byte_count=batch.canonical_byte_count,
            batch_identity_sha256=batch.identity_sha256,
            rows_sha256=batch.rows_sha256,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "source_table": self.source_table,
            "previous_cursor": self.previous_cursor,
            "cursor": self.cursor,
            "batch_size": self.batch_size,
            "max_batch_bytes": self.max_batch_bytes,
            "upper_bound": self.upper_bound,
            "row_count": self.row_count,
            "canonical_byte_count": self.canonical_byte_count,
            "batch_identity_sha256": self.batch_identity_sha256,
            "rows_sha256": self.rows_sha256,
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, payload: str, expected_sha256: str) -> ImportCheckpoint:
        root = _require_fields(
            _strict_json(payload, expected_sha256, cursor_contract=True),
            {
                "version",
                "manifest_sha256",
                "source_table",
                "previous_cursor",
                "cursor",
                "batch_size",
                "max_batch_bytes",
                "upper_bound",
                "row_count",
                "canonical_byte_count",
                "batch_identity_sha256",
                "rows_sha256",
            },
            "import checkpoint",
        )
        return cls(
            version=_require_int(root["version"], "checkpoint version", minimum=1),
            manifest_sha256=root["manifest_sha256"],
            source_table=root["source_table"],
            previous_cursor=_decode_cursor_object(root["previous_cursor"]),
            cursor=_decode_cursor_object(root["cursor"]),
            batch_size=root["batch_size"],
            max_batch_bytes=root["max_batch_bytes"],
            upper_bound=_decode_cursor_object(root["upper_bound"]),
            row_count=root["row_count"],
            canonical_byte_count=root["canonical_byte_count"],
            batch_identity_sha256=root["batch_identity_sha256"],
            rows_sha256=root["rows_sha256"],
        )

    def resume_request(self, manifest: SourceManifest) -> BatchRequest:
        """Verify this receipt against a manifest and request the following batch."""

        if not isinstance(manifest, SourceManifest):
            raise TypeError("resume manifest must be a SourceManifest")
        if self.manifest_sha256 != manifest.sha256:
            raise ValueError("checkpoint manifest SHA-256 does not match the supplied manifest")
        table = manifest.table(self.source_table)
        if self.upper_bound != table.upper_bound:
            raise ValueError("checkpoint upper bound does not match the supplied manifest")
        # __post_init__ recomputes the predecessor identity. Reconstructing here
        # also catches any future mutation if this dataclass stops being frozen.
        self.__post_init__()
        return BatchRequest(
            self.manifest_sha256,
            self.source_table,
            self.cursor,
            self.batch_size,
            self.max_batch_bytes,
        )


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def read(cls, path: Path) -> _FileIdentity:
        stat = path.stat()
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    values: tuple[int, ...]

    @classmethod
    def read(cls, path: Path) -> _DirectoryIdentity:
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                info = os.fstat(descriptor)
                return cls((info.st_dev, info.st_ino))
            finally:
                os.close(descriptor)

        import ctypes
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x0080,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "cannot open destination parent without traversal", str(path))
        try:
            information = ByHandleFileInformation()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
                raise OSError(ctypes.get_last_error(), "cannot identify destination parent", str(path))
            if information.file_attributes & 0x00000400:
                raise SnapshotError(f"destination parent is a Windows reparse point: {path}")
            return cls(
                (
                    information.volume_serial_number,
                    information.file_index_high,
                    information.file_index_low,
                )
            )
        finally:
            kernel.CloseHandle(handle)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _snapshot_sidecars(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


def _assert_no_snapshot_sidecars(path: Path) -> None:
    existing = [sidecar for sidecar in _snapshot_sidecars(path) if sidecar.exists()]
    if existing:
        names = ", ".join(str(sidecar) for sidecar in existing)
        raise SnapshotError(f"immutable SQLite snapshot has WAL/SHM sidecar state: {names}")


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    raise TypeError(f"unsupported mutable source value: {type(value).__name__}")


def _backup_database(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    source.backup(destination, pages=256, sleep=0.001)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as sealed:
        os.fsync(sealed.fileno())


def _has_link_or_reparse(path: Path) -> bool:
    information = os.lstat(path)
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _validate_lexical_parent(destination_path: str | os.PathLike[str]) -> Path:
    requested = Path(destination_path).expanduser()
    destination = Path(os.path.abspath(requested))
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(parent)
    current = Path(parent.anchor)
    for component in parent.parts[1:]:
        current /= component
        if _has_link_or_reparse(current):
            raise SnapshotError(f"destination path contains a symlink, junction, or reparse point: {current}")
        if not current.is_dir():
            raise NotADirectoryError(current)
    return destination


class _DirectoryHandle:
    """Retained no-traversal capability for one validated destination directory."""

    def __init__(self, path: Path, raw_handle: int, identity: _DirectoryIdentity) -> None:
        self.path = path
        self.raw_handle = raw_handle
        self.identity = identity

    @classmethod
    def open(cls, path: Path) -> _DirectoryHandle:
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            information = os.fstat(descriptor)
            return cls(path, descriptor, _DirectoryIdentity((information.st_dev, information.st_ino)))

        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x0080,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot retain destination directory", str(path))
        try:
            identity = _DirectoryIdentity.read(path)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        return cls(path, int(handle), identity)

    def __enter__(self) -> _DirectoryHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.raw_handle < 0:
            return
        if os.name == "nt":
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.raw_handle)
        else:
            os.close(self.raw_handle)
        self.raw_handle = -1

    def revalidate(self) -> None:
        current = Path(self.path.anchor)
        for component in self.path.parts[1:]:
            current /= component
            if _has_link_or_reparse(current):
                raise SnapshotChangedError(f"destination parent gained a reparse component: {current}")
        if _DirectoryIdentity.read(self.path) != self.identity:
            raise SnapshotChangedError("destination parent directory changed during sealing")
        if os.name != "nt":
            information = os.fstat(self.raw_handle)
            if _DirectoryIdentity((information.st_dev, information.st_ino)) != self.identity:
                raise SnapshotChangedError("retained destination directory handle changed identity")

    def create_exclusive(self, name: str, mode: int = 0o600) -> int:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            return os.open(self.path / name, flags, mode)
        return os.open(name, flags, mode, dir_fd=self.raw_handle)

    def write_exclusive(self, name: str, payload: bytes) -> None:
        descriptor = self.create_exclusive(name)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def unlink_created(self, name: str) -> None:
        try:
            if os.name == "nt":
                os.unlink(self.path / name)
            else:
                os.unlink(name, dir_fd=self.raw_handle)
        except FileNotFoundError:
            pass

    def publish(self, temporary_name: str, destination_name: str) -> None:
        self.revalidate()
        destination = self.path / destination_name
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            move_file.restype = wintypes.BOOL
            if not move_file(str(self.path / temporary_name), str(destination), 0x00000008):
                error = ctypes.get_last_error()
                if os.path.lexists(destination):
                    raise FileExistsError(destination)
                raise OSError(error, "failed to durably publish sealed SQLite snapshot", str(destination))
            return

        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        rename_at_2 = getattr(libc, "renameat2", None)
        if rename_at_2 is not None:
            result = rename_at_2(
                self.raw_handle,
                os.fsencode(temporary_name),
                self.raw_handle,
                os.fsencode(destination_name),
                1,
            )
            if result == 0:
                os.fsync(self.raw_handle)
                return
            error = ctypes.get_errno()
            if error != errno.ENOSYS:
                raise OSError(error, os.strerror(error), str(destination))
        os.link(
            temporary_name,
            destination_name,
            src_dir_fd=self.raw_handle,
            dst_dir_fd=self.raw_handle,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=self.raw_handle)
        os.fsync(self.raw_handle)


@dataclass(frozen=True, slots=True)
class _SourceFileState:
    exists: bool
    sha256: str | None
    size: int | None
    mtime_ns: int | None
    stat_identity: tuple[int, int, int, int, int] | None
    observation_complete: bool


def _capture_source_file(path: Path, *, hash_contents: bool) -> _SourceFileState:
    try:
        before = _FileIdentity.read(path)
    except FileNotFoundError:
        return _SourceFileState(False, None, None, None, None, True)
    digest: str | None = None
    observation_complete = True
    if hash_contents:
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for block in iter(lambda: source.read(64 * 1024), b""):
                    hasher.update(block)
            after = _FileIdentity.read(path)
        except (FileNotFoundError, OSError):
            return _SourceFileState(False, None, None, None, None, False)
        if after == before:
            digest = hasher.hexdigest()
        else:
            before = after
            observation_complete = False
    return _SourceFileState(
        True,
        digest,
        before.size,
        before.mtime_ns,
        (before.device, before.inode, before.size, before.mtime_ns, before.ctime_ns),
        observation_complete,
    )


def _source_audit(path: Path, before: _SourceFileState, after: _SourceFileState, *, ephemeral: bool) -> SourceFileAudit:
    return SourceFileAudit(
        path=str(path),
        before_exists=before.exists,
        after_exists=after.exists,
        before_sha256=before.sha256,
        after_sha256=after.sha256,
        before_size=before.size,
        after_size=after.size,
        before_mtime_ns=before.mtime_ns,
        after_mtime_ns=after.mtime_ns,
        before_stat_identity=before.stat_identity,
        after_stat_identity=after.stat_identity,
        changed=before != after,
        observation_complete=before.observation_complete and after.observation_complete,
        ephemeral=ephemeral,
    )


def _identity_values(identity: _FileIdentity) -> tuple[str, ...]:
    return tuple(
        str(value) for value in (identity.device, identity.inode, identity.size, identity.mtime_ns, identity.ctime_ns)
    )


def _audit_as_dict(audit: SourceFileAudit) -> dict[str, Any]:
    def identity(value: tuple[int, int, int, int, int] | None) -> list[str] | None:
        return None if value is None else [str(item) for item in value]

    return {
        "path": audit.path,
        "before_exists": audit.before_exists,
        "after_exists": audit.after_exists,
        "before_sha256": audit.before_sha256,
        "after_sha256": audit.after_sha256,
        "before_size": audit.before_size,
        "after_size": audit.after_size,
        "before_mtime_ns": None if audit.before_mtime_ns is None else str(audit.before_mtime_ns),
        "after_mtime_ns": None if audit.after_mtime_ns is None else str(audit.after_mtime_ns),
        "before_stat_identity": identity(audit.before_stat_identity),
        "after_stat_identity": identity(audit.after_stat_identity),
        "changed": audit.changed,
        "observation_complete": audit.observation_complete,
        "ephemeral": audit.ephemeral,
    }


def _receipt_as_dict(receipt: SealedSnapshotReceipt) -> dict[str, Any]:
    return {
        "version": receipt.version,
        "path": receipt.path,
        "source_path": receipt.source_path,
        "sha256": receipt.sha256,
        "size": receipt.size,
        "quick_check": receipt.quick_check,
        "source_mode": receipt.source_mode,
        "source_db_audit": _audit_as_dict(receipt.source_db_audit),
        "source_wal_audit": _audit_as_dict(receipt.source_wal_audit),
        "source_shm_audit": _audit_as_dict(receipt.source_shm_audit),
        "source_changed_during_backup": receipt.source_changed_during_backup,
    }


def _audit_from_dict(value: Any) -> SourceFileAudit:
    fields = _require_fields(
        value,
        {
            "path",
            "before_exists",
            "after_exists",
            "before_sha256",
            "after_sha256",
            "before_size",
            "after_size",
            "before_mtime_ns",
            "after_mtime_ns",
            "before_stat_identity",
            "after_stat_identity",
            "changed",
            "observation_complete",
            "ephemeral",
        },
        "source file audit",
    )

    def optional_int(item: Any) -> int | None:
        return None if item is None else int(item)

    def optional_identity(item: Any) -> tuple[int, int, int, int, int] | None:
        if item is None:
            return None
        if not isinstance(item, list) or len(item) != 5 or not all(isinstance(part, str) for part in item):
            raise ValueError("source file stat identity must be five decimal strings or null")
        return tuple(int(part) for part in item)  # type: ignore[return-value]

    return SourceFileAudit(
        path=fields["path"],
        before_exists=fields["before_exists"],
        after_exists=fields["after_exists"],
        before_sha256=fields["before_sha256"],
        after_sha256=fields["after_sha256"],
        before_size=fields["before_size"],
        after_size=fields["after_size"],
        before_mtime_ns=optional_int(fields["before_mtime_ns"]),
        after_mtime_ns=optional_int(fields["after_mtime_ns"]),
        before_stat_identity=optional_identity(fields["before_stat_identity"]),
        after_stat_identity=optional_identity(fields["after_stat_identity"]),
        changed=fields["changed"],
        observation_complete=fields["observation_complete"],
        ephemeral=fields["ephemeral"],
    )


def _receipt_from_dict(value: Any) -> SealedSnapshotReceipt:
    fields = _require_fields(
        value,
        {
            "version",
            "path",
            "source_path",
            "sha256",
            "size",
            "quick_check",
            "source_mode",
            "source_db_audit",
            "source_wal_audit",
            "source_shm_audit",
            "source_changed_during_backup",
        },
        "sealed snapshot receipt",
    )
    return SealedSnapshotReceipt(
        version=fields["version"],
        path=fields["path"],
        source_path=fields["source_path"],
        sha256=fields["sha256"],
        size=fields["size"],
        quick_check=fields["quick_check"],
        source_mode=fields["source_mode"],
        source_db_audit=_audit_from_dict(fields["source_db_audit"]),
        source_wal_audit=_audit_from_dict(fields["source_wal_audit"]),
        source_shm_audit=_audit_from_dict(fields["source_shm_audit"]),
        source_changed_during_backup=fields["source_changed_during_backup"],
    )


def _seal_names(operation_id: str) -> tuple[str, str]:
    prefix = f".applypilot-seal-{operation_id}"
    return f"{prefix}.sealing", f"{prefix}.receipt.json"


def _is_single_regular_file(path: Path) -> bool:
    if not path.exists() or _has_link_or_reparse(path):
        return False
    information = os.lstat(path)
    return stat.S_ISREG(information.st_mode) and information.st_nlink == 1


def _quick_check_path(path: Path) -> str:
    encoded = quote(path.as_posix(), safe="/:")
    try:
        with closing(sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)) as connection:
            return "\n".join(str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall())
    except sqlite3.DatabaseError as exc:
        raise SnapshotChangedError(f"published sealed destination is not a valid SQLite database: {path}") from exc


def _ready_marker_fields(
    operation_id: str,
    destination: Path,
    temporary_name: str,
    temporary_identity: _FileIdentity,
    receipt: SealedSnapshotReceipt,
) -> dict[str, Any]:
    return {
        "version": 1,
        "operation_id": operation_id,
        "destination_name": destination.name,
        "temporary_name": temporary_name,
        "temporary_sha256": receipt.sha256,
        "temporary_size": receipt.size,
        "temporary_identity": list(_identity_values(temporary_identity)),
        "receipt": _receipt_as_dict(receipt),
    }


def _read_ready_marker(marker: Path, destination: Path) -> tuple[str, _FileIdentity, SealedSnapshotReceipt] | None:
    match = re.fullmatch(
        r"\.applypilot-seal-([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.receipt\.json",
        marker.name,
    )
    if match is None or not _is_single_regular_file(marker) or marker.stat().st_size > 64 * 1024:
        return None
    try:
        payload = marker.read_text(encoding="utf-8")
        value = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    try:
        canonical_payload = canonical_json(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if canonical_payload != payload:
        return None
    try:
        fields = _require_fields(
            value,
            {
                "version",
                "operation_id",
                "destination_name",
                "temporary_name",
                "temporary_sha256",
                "temporary_size",
                "temporary_identity",
                "receipt",
            },
            "seal recovery marker",
        )
    except (TypeError, ValueError):
        return None
    operation_id = match.group(1)
    temporary_name, expected_marker_name = _seal_names(operation_id)
    if (
        fields["version"] != 1
        or fields["operation_id"] != operation_id
        or fields["destination_name"] != destination.name
        or fields["temporary_name"] != temporary_name
        or marker.name != expected_marker_name
        or not isinstance(fields["temporary_identity"], list)
        or len(fields["temporary_identity"]) != 5
        or not all(isinstance(item, str) for item in fields["temporary_identity"])
    ):
        return None
    try:
        receipt = _receipt_from_dict(fields["receipt"])
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        Path(receipt.path) != destination
        or receipt.sha256 != fields["temporary_sha256"]
        or receipt.size != fields["temporary_size"]
    ):
        return None
    identity = _FileIdentity(*(int(item) for item in fields["temporary_identity"]))
    return temporary_name, identity, receipt


def _validate_recovery_file(path: Path, identity: _FileIdentity, receipt: SealedSnapshotReceipt) -> None:
    if not _is_single_regular_file(path):
        raise SnapshotChangedError(f"published sealed destination is not an owned regular file: {path}")
    if _FileIdentity.read(path) != identity:
        raise SnapshotChangedError(f"published sealed destination identity mismatch: {path}")
    sha256, current_identity = _hash_file(path)
    if sha256 != receipt.sha256 or current_identity.size != receipt.size:
        raise SnapshotChangedError(f"published sealed destination fingerprint mismatch: {path}")
    if _quick_check_path(path) != receipt.quick_check:
        raise SnapshotChangedError(f"published sealed destination quick_check mismatch: {path}")


def _reconcile_seal(
    destination: Path,
    directory: _DirectoryHandle,
    expected_source: Path,
    source_mode: str,
) -> SealedSnapshotReceipt | None:
    for marker in sorted(destination.parent.glob(".applypilot-seal-*.receipt.json")):
        ready = _read_ready_marker(marker, destination)
        if ready is None:
            continue
        temporary_name, identity, receipt = ready
        if Path(receipt.source_path) != expected_source or receipt.source_mode != source_mode:
            continue
        temporary = destination.parent / temporary_name
        if os.path.lexists(destination):
            if os.path.lexists(temporary):
                continue
            _validate_recovery_file(destination, identity, receipt)
            return receipt
        if not os.path.lexists(temporary):
            continue
        _validate_recovery_file(temporary, identity, receipt)
        directory.publish(temporary_name, destination.name)
        _after_publish()
        return receipt
    return None


def _before_publish() -> None:
    pass


def _after_publish() -> None:
    pass


def seal_sqlite_snapshot(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    source_mode: str = ONLINE_BACKUP_MODE,
) -> SealedSnapshotReceipt:
    """Create and durably publish a transactionally consistent private backup.

    Online mode includes committed WAL state and records source observations as
    non-authoritative provenance. Immutable mode is limited to checkpointed,
    sidecar-free input and uses SQLite's immutable read contract.
    """

    if not isinstance(source_path, (str, os.PathLike)) or not isinstance(destination_path, (str, os.PathLike)):
        raise TypeError("explicit source and sealed destination paths are required")
    if source_mode not in _SOURCE_MODES:
        raise ValueError(f"unsupported source mode: {source_mode!r}")
    requested_source = Path(os.path.abspath(Path(source_path).expanduser()))
    source = requested_source.resolve(strict=True) if requested_source.exists() else requested_source
    destination = _validate_lexical_parent(destination_path)
    if source == destination:
        raise ValueError("sealed destination must differ from the live source")
    with _DirectoryHandle.open(destination.parent) as directory:
        directory.revalidate()
        recovered = _reconcile_seal(destination, directory, source, source_mode)
        if recovered is not None:
            return recovered
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        if not source.is_file():
            raise FileNotFoundError(source)

        wal, shm = _snapshot_sidecars(source)
        db_before = _capture_source_file(source, hash_contents=True)
        wal_before = _capture_source_file(wal, hash_contents=True)
        shm_before = _capture_source_file(shm, hash_contents=False)
        if source_mode == IMMUTABLE_NO_FILESYSTEM_WRITE_MODE and (wal_before.exists or shm_before.exists):
            raise SnapshotError("immutable no-filesystem-write source must be fully checkpointed and sidecar-free")

        operation_id = str(uuid.uuid4())
        temporary_name, marker_name = _seal_names(operation_id)
        temporary = destination.parent / temporary_name
        descriptor = directory.create_exclusive(temporary_name)
        os.close(descriptor)
        marker_written = False
        published = False
        try:
            encoded_source = quote(source.as_posix(), safe="/:")
            immutable_query = "&immutable=1" if source_mode == IMMUTABLE_NO_FILESYSTEM_WRITE_MODE else ""
            with closing(
                sqlite3.connect(f"file:{encoded_source}?mode=ro{immutable_query}", uri=True)
            ) as source_connection:
                source_connection.execute("PRAGMA query_only = ON")
                data_version_before = int(source_connection.execute("PRAGMA data_version").fetchone()[0])
                source_connection.execute("BEGIN")
                source_connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
                with closing(sqlite3.connect(temporary)) as destination_connection:
                    _backup_database(source_connection, destination_connection)
                    destination_connection.commit()
                    try:
                        quick_rows = destination_connection.execute("PRAGMA quick_check").fetchall()
                    except sqlite3.OperationalError as exc:
                        if "collation" in str(exc).lower():
                            raise SourceSchemaError(
                                "source key collations must be registered BINARY collations"
                            ) from exc
                        raise
                    quick_check = "\n".join(str(row[0]) for row in quick_rows)
                    if quick_check != "ok":
                        raise SnapshotError(f"sealed SQLite quick_check failed: {quick_check}")
                data_version_after = int(source_connection.execute("PRAGMA data_version").fetchone()[0])

            db_after = _capture_source_file(source, hash_contents=True)
            wal_after = _capture_source_file(wal, hash_contents=True)
            shm_after = _capture_source_file(shm, hash_contents=False)
            db_audit = _source_audit(source, db_before, db_after, ephemeral=False)
            wal_audit = _source_audit(wal, wal_before, wal_after, ephemeral=False)
            shm_audit = _source_audit(shm, shm_before, shm_after, ephemeral=True)
            source_changed = (
                db_audit.changed
                or wal_audit.changed
                or not db_audit.observation_complete
                or not wal_audit.observation_complete
                or data_version_before != data_version_after
            )
            if source_mode == IMMUTABLE_NO_FILESYSTEM_WRITE_MODE and (
                source_changed or wal_after.exists or shm_after.exists
            ):
                raise SnapshotChangedError("immutable source or sidecar state changed during sealing")

            for sidecar in _snapshot_sidecars(temporary):
                if sidecar.exists():
                    raise SnapshotError(f"sealed SQLite destination retained sidecar state: {sidecar}")
            _fsync_file(temporary)
            sha256, identity = _hash_file(temporary)
            receipt = SealedSnapshotReceipt(
                version=SEALED_SNAPSHOT_VERSION,
                path=str(destination),
                source_path=str(source),
                sha256=sha256,
                size=identity.size,
                quick_check=quick_check,
                source_mode=source_mode,
                source_db_audit=db_audit,
                source_wal_audit=wal_audit,
                source_shm_audit=shm_audit,
                source_changed_during_backup=source_changed,
            )
            marker_fields = _ready_marker_fields(operation_id, destination, temporary_name, identity, receipt)
            directory.write_exclusive(marker_name, canonicalize_jcs_bytes(marker_fields))
            marker_written = True
            directory.revalidate()
            _before_publish()
            directory.publish(temporary_name, destination.name)
            published = True
            _after_publish()
            return receipt
        except BaseException:
            if not marker_written and not published:
                directory.unlink_created(temporary_name)
            raise


def _hash_file(path: Path) -> tuple[str, _FileIdentity]:
    _assert_no_snapshot_sidecars(path)
    before = _FileIdentity.read(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(64 * 1024), b""):
            digest.update(block)
    after = _FileIdentity.read(path)
    _assert_no_snapshot_sidecars(path)
    if after != before:
        raise SnapshotChangedError("SQLite snapshot changed while its SHA-256 was being computed")
    return digest.hexdigest(), after


class SnapshotReader:
    """Read only a private backup proven by a sealed snapshot receipt."""

    def __init__(
        self,
        receipt: SealedSnapshotReceipt,
        *,
        manifest_cursor_max_bytes: int = DEFAULT_MANIFEST_CURSOR_MAX_BYTES,
    ) -> None:
        if not isinstance(receipt, SealedSnapshotReceipt):
            raise TypeError("SnapshotReader requires a SealedSnapshotReceipt")
        raw_path = Path(receipt.path)
        if not raw_path.is_file():
            raise FileNotFoundError(raw_path)
        self.path = raw_path.resolve(strict=True)
        if self.path != raw_path:
            raise SnapshotError("sealed snapshot receipt path must not be a symlink")
        _assert_no_snapshot_sidecars(self.path)
        self.receipt = receipt
        self.expected_sha256 = receipt.sha256
        self.expected_size = receipt.size
        self.manifest_cursor_max_bytes = _require_int(manifest_cursor_max_bytes, "manifest cursor max bytes", minimum=1)
        self._identity: _FileIdentity | None = None
        self._bound_manifest_sha256: str | None = None

    def _connect(self) -> sqlite3.Connection:
        _assert_no_snapshot_sidecars(self.path)
        encoded_path = quote(self.path.as_posix(), safe="/:")
        connection = sqlite3.connect(f"file:{encoded_path}?mode=ro&immutable=1", uri=True)
        try:
            _assert_no_snapshot_sidecars(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    def _assert_identity(self) -> None:
        _assert_no_snapshot_sidecars(self.path)
        if self._identity is None:
            raise SnapshotError("snapshot reader is not bound to a manifest")
        try:
            current = _FileIdentity.read(self.path)
        except FileNotFoundError as exc:
            raise SnapshotChangedError("SQLite snapshot path disappeared during the run") from exc
        if current != self._identity:
            raise SnapshotChangedError("SQLite snapshot changed or was replaced during the run")

    def _inspect_source(self, connection: sqlite3.Connection) -> tuple[str, int, int, int, int]:
        try:
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.OperationalError as exc:
            if "collation" in str(exc).lower():
                raise SourceSchemaError("source key collations must be registered BINARY collations") from exc
            raise
        quick_check = "\n".join(str(row[0]) for row in quick_rows)
        if quick_check != "ok":
            raise SnapshotError(f"SQLite quick_check failed: {quick_check}")
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return quick_check, page_count, page_size, schema_version, user_version

    def _columns(self, connection: sqlite3.Connection, table: SourceTableSpec) -> tuple[str, ...]:
        rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table.name)})").fetchall()
        columns = tuple(str(row[1]) for row in rows)
        if not columns:
            raise SourceSchemaError(f"missing required source table: {table.name}")
        missing = [column for column in table.key_columns if column not in columns]
        if missing:
            raise SourceSchemaError(f"source table {table.name} is missing key columns: {', '.join(missing)}")
        primary_key = tuple(str(row[1]) for row in sorted(rows, key=lambda item: int(item[5])) if int(row[5]) > 0)
        if primary_key != table.key_columns:
            raise SourceSchemaError(
                f"source table {table.name} primary key {primary_key!r} does not match {table.key_columns!r}"
            )
        if str in table.key_types:
            self._validate_binary_primary_key(connection, table)
        return columns

    def _validate_binary_primary_key(self, connection: sqlite3.Connection, table: SourceTableSpec) -> None:
        indexes = connection.execute(f"PRAGMA index_list({_quote_identifier(table.name)})").fetchall()
        for index in indexes:
            if str(index[3]) != "pk":
                continue
            details = connection.execute(f"PRAGMA index_xinfo({_quote_identifier(str(index[1]))})").fetchall()
            key_rows = sorted(
                (row for row in details if int(row[5]) == 1 and int(row[1]) >= 0), key=lambda row: int(row[0])
            )
            columns = tuple(str(row[2]) for row in key_rows)
            if columns != table.key_columns:
                continue
            collations = tuple(str(row[4]).upper() for row in key_rows)
            if collations != tuple("BINARY" for _ in table.key_columns):
                raise SourceSchemaError(
                    f"source table {table.name} key collations must all be BINARY, got {collations!r}"
                )
            return
        raise SourceSchemaError(f"source table {table.name} has no BINARY primary-key index")

    @staticmethod
    def _key_sql(column: str, value_type: type[str] | type[int]) -> str:
        quoted = _quote_identifier(column)
        return f"{quoted} COLLATE BINARY" if value_type is str else quoted

    @staticmethod
    def _row_size_bound_sql(columns: tuple[str, ...]) -> tuple[str, str]:
        minimum_parts: list[str] = []
        maximum_parts: list[str] = []
        for column in columns:
            quoted = _quote_identifier(column)
            key_overhead = len(canonicalize_jcs_bytes(column)) + 1
            minimum_parts.append(
                f"{key_overhead} + CASE typeof({quoted}) "
                "WHEN 'null' THEN 4 WHEN 'integer' THEN 1 WHEN 'real' THEN 1 "
                f"WHEN 'text' THEN 2 + length(CAST({quoted} AS BLOB)) "
                f"WHEN 'blob' THEN 13 + 4 * ((length({quoted}) + 2) / 3) "
                "ELSE 1 END"
            )
            maximum_parts.append(
                f"{key_overhead} + CASE typeof({quoted}) "
                "WHEN 'null' THEN 4 WHEN 'integer' THEN 20 WHEN 'real' THEN 24 "
                f"WHEN 'text' THEN length(CAST(json_quote({quoted}) AS BLOB)) "
                f"WHEN 'blob' THEN 13 + 4 * ((length({quoted}) + 2) / 3) "
                "ELSE 24 END"
            )
        structural = 2 + max(0, len(columns) - 1)
        return (
            f"{structural} + " + " + ".join(minimum_parts),
            f"{structural} + " + " + ".join(maximum_parts),
        )

    def _cursor_from_values(self, table: SourceTableSpec, values: tuple[Any, ...]) -> Cursor:
        if len(values) == 1 and table.key_types == (str,):
            return TextCursor(values[0])
        if len(values) == 1 and table.key_types == (int,):
            return IntegerCursor(values[0])
        return CompositeCursor(values)

    @staticmethod
    def _manifest_key_bound_sql(table: SourceTableSpec) -> str:
        parts: list[str] = []
        for column in table.key_columns:
            quoted = _quote_identifier(column)
            parts.append(
                f"CASE typeof({quoted}) "
                "WHEN 'null' THEN 4 WHEN 'integer' THEN 20 WHEN 'real' THEN 24 "
                f"WHEN 'text' THEN 2 + 6 * length(CAST({quoted} AS BLOB)) "
                f"WHEN 'blob' THEN 13 + 4 * ((length(CAST({quoted} AS BLOB)) + 2) / 3) "
                "ELSE 24 END"
            )
        return f"128 + {' + '.join(parts)}"

    def _table_manifest(self, connection: sqlite3.Connection, table: SourceTableSpec) -> TableManifest:
        self._columns(connection, table)
        quoted_table = _quote_identifier(table.name)
        count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0])
        if count == 0:
            upper_bound = None
        else:
            for column, value_type in zip(table.key_columns, table.key_types, strict=True):
                if value_type is not int:
                    continue
                quoted_column = _quote_identifier(column)
                unsafe = connection.execute(
                    f"SELECT 1 FROM {quoted_table} "
                    f"WHERE typeof({quoted_column}) = 'integer' "
                    f"AND ({quoted_column} < ? OR {quoted_column} > ?) LIMIT 1",
                    (-_MAX_CURSOR_SAFE_INTEGER, _MAX_CURSOR_SAFE_INTEGER),
                ).fetchone()
                if unsafe is not None:
                    raise ValueError(
                        f"integer cursor component for {table.name}.{column} is outside "
                        "the JavaScript safe integer range"
                    )
            key_bound_sql = self._manifest_key_bound_sql(table)
            maximum_key_bytes = int(
                connection.execute(f"SELECT MAX({key_bound_sql}) FROM {quoted_table}").fetchone()[0]
            )
            if maximum_key_bytes > self.manifest_cursor_max_bytes:
                raise OversizedManifestCursorError(
                    table.name,
                    maximum_key_bytes,
                    self.manifest_cursor_max_bytes,
                )
            key_parts = tuple(
                self._key_sql(column, value_type)
                for column, value_type in zip(table.key_columns, table.key_types, strict=True)
            )
            keys = ", ".join(_quote_identifier(column) for column in table.key_columns)
            order = ", ".join(f"{part} DESC" for part in key_parts)
            row = connection.execute(f"SELECT {keys} FROM {quoted_table} ORDER BY {order} LIMIT 1").fetchone()
            values = tuple(row[index] for index in range(len(table.key_columns)))
            upper_bound = self._cursor_from_values(table, values)
            exact_cursor_bytes = len(canonicalize_jcs_bytes(_json_value(upper_bound)))
            if exact_cursor_bytes > self.manifest_cursor_max_bytes:
                raise OversizedManifestCursorError(
                    table.name,
                    exact_cursor_bytes,
                    self.manifest_cursor_max_bytes,
                )
        return TableManifest(table.name, table.key_columns, count, upper_bound)

    def capture_manifest(self) -> SourceManifest:
        _assert_no_snapshot_sidecars(self.path)
        initial_identity = _FileIdentity.read(self.path)
        if initial_identity.size != self.expected_size:
            raise SnapshotError(
                f"SQLite snapshot size mismatch: expected {self.expected_size}, got {initial_identity.size}"
            )
        sha256, identity = _hash_file(self.path)
        if sha256 != self.expected_sha256:
            raise SnapshotError(f"SQLite snapshot SHA-256 mismatch: expected {self.expected_sha256}, got {sha256}")
        _assert_no_snapshot_sidecars(self.path)
        if _FileIdentity.read(self.path) != identity:
            raise SnapshotChangedError("SQLite snapshot changed between fingerprint and immutable open")
        with closing(self._connect()) as connection:
            quick_check, page_count, page_size, schema_version, user_version = self._inspect_source(connection)
            tables = tuple(self._table_manifest(connection, table) for table in SOURCE_TABLES)
        _assert_no_snapshot_sidecars(self.path)
        if _FileIdentity.read(self.path) != identity:
            raise SnapshotChangedError("SQLite snapshot changed while its manifest was being captured")
        source = SourceFingerprint(
            path=str(self.path),
            sha256=sha256,
            size=identity.size,
            quick_check=quick_check,
            page_count=page_count,
            page_size=page_size,
            schema_version=schema_version,
            user_version=user_version,
        )
        manifest = SourceManifest(
            MANIFEST_VERSION,
            source,
            tables,
            self.manifest_cursor_max_bytes,
        )
        self._identity = identity
        self._bound_manifest_sha256 = manifest.sha256
        return manifest

    def _bind_manifest(self, manifest: SourceManifest) -> None:
        if manifest.version != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version: {manifest.version}")
        if manifest.manifest_cursor_max_bytes != self.manifest_cursor_max_bytes:
            raise ValueError("manifest cursor cap does not match this reader")
        if Path(manifest.source.path) != self.path:
            raise SnapshotChangedError("manifest source path does not match this reader")
        if manifest.source.sha256 != self.expected_sha256 or manifest.source.size != self.expected_size:
            raise SnapshotChangedError("manifest source binding does not match this reader's required fingerprint")
        if self._bound_manifest_sha256 is not None:
            if self._bound_manifest_sha256 != manifest.sha256:
                raise ValueError("reader is already bound to a different manifest")
        sha256, identity = _hash_file(self.path)
        if sha256 != manifest.source.sha256 or identity.size != manifest.source.size:
            raise SnapshotChangedError("SQLite snapshot fingerprint does not match the resume manifest")
        if self._identity is not None and identity != self._identity:
            raise SnapshotChangedError("sealed SQLite snapshot was replaced during the run")
        self._identity = identity
        self._bound_manifest_sha256 = manifest.sha256

    def _validate_cursor(self, table: SourceTableSpec, cursor: Cursor | None) -> None:
        _validate_cursor_value(table, cursor)

    @staticmethod
    def _cursor_values(cursor: Cursor) -> tuple[str | int, ...]:
        if isinstance(cursor, CompositeCursor):
            return cursor.values
        return (cursor.value,)

    def read_batch(self, manifest: SourceManifest, request: BatchRequest) -> SourceBatch:
        if request.manifest_sha256 != manifest.sha256:
            raise ValueError("batch request manifest SHA-256 does not match the supplied manifest")
        table = _TABLE_BY_NAME.get(request.source_table)
        if table is None:
            raise ValueError(f"unknown source table: {request.source_table}")
        table_manifest = manifest.table(table.name)
        self._validate_cursor(table, request.after)
        self._validate_cursor(table, table_manifest.upper_bound)
        if request.after is not None:
            if table_manifest.upper_bound is None or _cursor_key(request.after) > _cursor_key(
                table_manifest.upper_bound
            ):
                raise ValueError(f"after cursor for {table.name} exceeds the manifest upper bound")
        self._bind_manifest(manifest)
        self._assert_identity()

        retained_rows: list[Mapping[str, Any]] = []
        rows_hasher = hashlib.sha256()
        rows_hasher.update(b"[")
        canonical_byte_count = 1
        if table_manifest.upper_bound is None:
            pass
        else:
            quoted_keys = tuple(
                self._key_sql(column, value_type)
                for column, value_type in zip(table.key_columns, table.key_types, strict=True)
            )
            key_expression = quoted_keys[0] if len(quoted_keys) == 1 else f"({', '.join(quoted_keys)})"
            upper_values = self._cursor_values(table_manifest.upper_bound)
            upper_placeholders = "?" if len(upper_values) == 1 else f"({', '.join('?' for _ in upper_values)})"
            order = ", ".join(quoted_keys)
            with closing(self._connect()) as connection:
                columns = self._columns(connection, table)
                selected = ", ".join(_quote_identifier(column) for column in columns)
                minimum_size_sql, maximum_size_sql = self._row_size_bound_sql(columns)
                key_metadata = ", ".join(
                    expression
                    for column in table.key_columns
                    for expression in (
                        f"typeof({_quote_identifier(column)}) AS {_quote_identifier(f'__key_type_{column}')}",
                        f"length(CAST({_quote_identifier(column)} AS BLOB)) AS "
                        f"{_quote_identifier(f'__key_bytes_{column}')}",
                    )
                )
                scan_after = request.after
                while len(retained_rows) < request.batch_size:
                    conditions = [f"{key_expression} <= {upper_placeholders}"]
                    parameters: list[Any] = list(upper_values)
                    if scan_after is not None:
                        after_values = self._cursor_values(scan_after)
                        after_placeholders = (
                            "?" if len(after_values) == 1 else f"({', '.join('?' for _ in after_values)})"
                        )
                        conditions.insert(0, f"{key_expression} > {after_placeholders}")
                        parameters = list(after_values) + parameters
                    where = " AND ".join(conditions)
                    metadata = connection.execute(
                        f"SELECT {key_metadata}, {minimum_size_sql} AS __minimum_canonical_bytes, "
                        f"{maximum_size_sql} AS __maximum_canonical_bytes "
                        f"FROM {_quote_identifier(table.name)} WHERE {where} ORDER BY {order} LIMIT 1",
                        parameters,
                    ).fetchone()
                    if metadata is None:
                        break
                    locator = {
                        "key_types": tuple(metadata[f"__key_type_{column}"] for column in table.key_columns),
                        "key_byte_lengths": tuple(
                            int(metadata[f"__key_bytes_{column}"]) for column in table.key_columns
                        ),
                    }
                    separator_size = 1 if retained_rows else 0
                    maximum_row_bytes = int(metadata["__maximum_canonical_bytes"])
                    maximum_projected_size = canonical_byte_count + separator_size + maximum_row_bytes + 1
                    if maximum_projected_size > request.max_batch_bytes:
                        if retained_rows:
                            break
                        self._assert_identity()
                        raise OversizedSourceRowError(
                            table.name,
                            locator,
                            maximum_row_bytes,
                            request.max_batch_bytes,
                            size_measurement="canonical_upper_bound",
                        )
                    full_row = connection.execute(
                        f"SELECT {selected} FROM {_quote_identifier(table.name)} "
                        f"WHERE {where} ORDER BY {order} LIMIT 1",
                        parameters,
                    ).fetchone()
                    if full_row is None:
                        raise SnapshotChangedError(f"sealed row disappeared during read: {table.name} {locator!r}")
                    row = {column: full_row[column] for column in columns}
                    encoded_row = canonicalize_jcs_bytes(_json_value(row))
                    if len(encoded_row) > maximum_row_bytes:
                        raise SourceSchemaError(
                            f"canonical size bound failed for {table.name} {locator!r}: "
                            f"exact={len(encoded_row)}, upper={maximum_row_bytes}"
                        )
                    projected_size = canonical_byte_count + separator_size + len(encoded_row) + 1
                    if projected_size > request.max_batch_bytes:
                        if retained_rows:
                            break
                        self._assert_identity()
                        raise OversizedSourceRowError(
                            table.name,
                            locator,
                            len(encoded_row),
                            request.max_batch_bytes,
                        )
                    if retained_rows:
                        rows_hasher.update(b",")
                        canonical_byte_count += 1
                    rows_hasher.update(encoded_row)
                    canonical_byte_count += len(encoded_row)
                    retained_rows.append(_freeze_value(row))
                    scan_after = self._cursor_from_values(
                        table,
                        tuple(row[column] for column in table.key_columns),
                    )
        self._assert_identity()

        rows_hasher.update(b"]")
        canonical_byte_count += 1
        rows = tuple(retained_rows)
        next_cursor = request.after
        if rows:
            next_cursor = self._cursor_from_values(table, tuple(rows[-1][column] for column in table.key_columns))
        rows_sha256 = rows_hasher.hexdigest()
        identity_payload = _batch_identity_payload(
            request,
            table_manifest.upper_bound,
            next_cursor,
            len(rows),
            canonical_byte_count,
            rows_sha256,
        )
        return SourceBatch(
            version=BATCH_VERSION,
            request=request,
            upper_bound=table_manifest.upper_bound,
            rows=rows,
            next_cursor=next_cursor,
            canonical_byte_count=canonical_byte_count,
            rows_sha256=rows_sha256,
            identity_sha256=_canonical_sha256(identity_payload),
        )
