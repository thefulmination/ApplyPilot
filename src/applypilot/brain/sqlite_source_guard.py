"""Bind immutable SQLite reads to an exact sealed source file."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import quote

from applypilot.brain.importer import (
    SnapshotChangedError,
    SnapshotError,
    _assert_no_snapshot_sidecars,
    _FileIdentity,
    _hash_file,
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class SQLiteSourceGuardError(SnapshotChangedError):
    """The sealed SQLite source no longer matches its required binding."""


class SQLiteSourceGuard:
    """Own a read-only SQLite connection bound to a path, identity, and SHA-256."""

    def __init__(self, path: str | Path, expected_sha256: str) -> None:
        if not isinstance(expected_sha256, str) or _SHA256_PATTERN.fullmatch(expected_sha256) is None:
            raise ValueError("expected_sha256 must be a lowercase 64-character SHA-256")

        supplied_path = Path(path).expanduser()
        resolved_path = supplied_path.resolve(strict=True)
        if not resolved_path.is_file():
            raise FileNotFoundError(resolved_path)
        if supplied_path.absolute() != resolved_path:
            raise SQLiteSourceGuardError("sealed SQLite source path must not be a symlink or alias")

        self.path = resolved_path
        self.expected_sha256 = expected_sha256
        self._identity: _FileIdentity | None = None
        self._connection: sqlite3.Connection | None = None
        self._open()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the guarded connection while the guard is open."""

        if self._connection is None:
            raise RuntimeError("SQLite source guard is closed")
        return self._connection

    def _fingerprint(self) -> _FileIdentity:
        try:
            digest, identity = _hash_file(self.path)
        except (OSError, SnapshotError) as exc:
            raise SQLiteSourceGuardError(f"sealed SQLite source cannot be verified: {self.path}") from exc
        if digest != self.expected_sha256:
            raise SQLiteSourceGuardError(
                f"sealed SQLite source SHA-256 mismatch: expected {self.expected_sha256}, got {digest}"
            )
        return identity

    def _assert_identity(self, expected: _FileIdentity, *, context: str) -> None:
        try:
            _assert_no_snapshot_sidecars(self.path)
            current = _FileIdentity.read(self.path)
            _assert_no_snapshot_sidecars(self.path)
        except (OSError, SnapshotError) as exc:
            raise SQLiteSourceGuardError(f"sealed SQLite source cannot be verified: {self.path}") from exc
        if current != expected:
            raise SQLiteSourceGuardError(f"sealed SQLite source changed or was replaced {context}")

    def _open(self) -> None:
        identity = self._fingerprint()
        encoded_path = quote(self.path.as_posix(), safe="/:")
        connection: sqlite3.Connection | None = None
        try:
            _assert_no_snapshot_sidecars(self.path)
            connection = sqlite3.connect(f"file:{encoded_path}?mode=ro&immutable=1", uri=True)
            connection.execute("PRAGMA query_only = ON")
            self._assert_identity(identity, context="while being opened")
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        self._identity = identity
        self._connection = connection

    def recheck(self) -> None:
        """Cheaply revalidate sidecar absence and the sealed file identity."""

        if self._connection is None or self._identity is None:
            raise RuntimeError("SQLite source guard is closed")
        self._assert_identity(self._identity, context="during guarded use")

    def verify_final(self) -> None:
        """Revalidate the full SHA-256 and identity after a long import."""

        if self._connection is None or self._identity is None:
            raise RuntimeError("SQLite source guard is closed")
        current = self._fingerprint()
        if current != self._identity:
            raise SQLiteSourceGuardError("sealed SQLite source changed or was replaced during guarded use")

    def close(self) -> None:
        """Close the guarded SQLite connection."""

        connection, self._connection = self._connection, None
        self._identity = None
        if connection is not None:
            connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
