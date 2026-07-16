"""Capture an auditable, read-only receipt for the SQLite brain source.

The cloud importer must never treat a path or a set of row counts as an
identity.  This module records the file fingerprint, SQLite health metadata,
sidecar state, and the row counts for the fourteen source tables used by the
brain migration.  It does not write to the source database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from applypilot.brain.importer import SOURCE_TABLES


class SourceReceiptError(RuntimeError):
    """The source changed or failed the SQLite source contract."""


@dataclass(frozen=True, slots=True)
class SQLiteSourceReceipt:
    path: str
    sha256: str
    byte_length: int
    quick_check: str
    page_count: int
    page_size: int
    schema_version: int
    user_version: int
    table_counts: dict[str, int]
    source_stat_before: tuple[int, int, int]
    source_stat_after: tuple[int, int, int]
    wal_size: int
    shm_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "quick_check": self.quick_check,
            "page_count": self.page_count,
            "page_size": self.page_size,
            "schema_version": self.schema_version,
            "user_version": self.user_version,
            "table_counts": dict(self.table_counts),
            "source_stat_before": list(self.source_stat_before),
            "source_stat_after": list(self.source_stat_after),
            "wal_size": self.wal_size,
            "shm_size": self.shm_size,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _stat_identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ino)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture_sqlite_source_receipt(path: str | os.PathLike[str]) -> SQLiteSourceReceipt:
    """Capture a source receipt without mutating the SQLite database.

    The source must be checkpointed enough for a read-only connection to see a
    consistent database.  A non-empty WAL is rejected because this receipt is
    intended to be the immutable input to a cloud migration; callers should
    first create a sealed SQLite snapshot when a WAL is active.
    """

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    wal = Path(f"{source}-wal")
    shm = Path(f"{source}-shm")
    wal_size = wal.stat().st_size if wal.exists() else 0
    shm_size = shm.stat().st_size if shm.exists() else 0
    if wal_size:
        raise SourceReceiptError(f"SQLite WAL is non-empty; seal a snapshot first: {wal}")

    before = _stat_identity(source)
    encoded = source.as_posix().replace("'", "%27")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        quick_check = "\n".join(str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall())
        if quick_check != "ok":
            raise SourceReceiptError(f"SQLite quick_check failed: {quick_check}")
        table_names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        missing = [table.name for table in SOURCE_TABLES if table.name not in table_names]
        if missing:
            raise SourceReceiptError(f"source is missing required tables: {', '.join(missing)}")
        counts = {
            table.name: int(connection.execute(
                f'SELECT COUNT(*) FROM "{table.name.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0])
            for table in SOURCE_TABLES
        }
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()

    after = _stat_identity(source)
    if before != after:
        raise SourceReceiptError("SQLite source changed while the receipt was being captured")
    digest = _sha256(source)
    if before != _stat_identity(source):
        raise SourceReceiptError("SQLite source changed while its SHA-256 was computed")
    return SQLiteSourceReceipt(
        path=str(source),
        sha256=digest,
        byte_length=after[0],
        quick_check=quick_check,
        page_count=page_count,
        page_size=page_size,
        schema_version=schema_version,
        user_version=user_version,
        table_counts=counts,
        source_stat_before=before,
        source_stat_after=after,
        wal_size=wal_size,
        shm_size=shm_size,
    )
