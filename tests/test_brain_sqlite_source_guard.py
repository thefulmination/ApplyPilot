from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import applypilot.brain.sqlite_source_guard as guard_module
from applypilot.brain.sqlite_source_guard import SQLiteSourceGuard, SQLiteSourceGuardError


def _database(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts (value) VALUES ('sealed')")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guard_opens_exact_source_read_only_and_rechecks(tmp_path: Path) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)

    with SQLiteSourceGuard(source, expected_sha256) as guard:
        assert guard.connection.execute("SELECT value FROM facts").fetchone()[0] == "sealed"
        assert guard.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        guard.recheck()
        guard.verify_final()
        with pytest.raises(sqlite3.OperationalError):
            guard.connection.execute("INSERT INTO facts (value) VALUES ('forbidden')")

    with pytest.raises(RuntimeError, match="closed"):
        guard.connection


def test_guard_rejects_wrong_expected_sha256(tmp_path: Path) -> None:
    source = tmp_path / "sealed.db"
    _database(source)

    with pytest.raises(SQLiteSourceGuardError, match="SHA-256 mismatch"):
        SQLiteSourceGuard(source, "0" * 64)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_guard_rejects_any_sqlite_sidecar(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)
    Path(f"{source}{suffix}").touch()

    with pytest.raises(SQLiteSourceGuardError, match="cannot be verified"):
        SQLiteSourceGuard(source, expected_sha256)


def test_guard_rejects_mutation_between_fingerprint_and_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)
    real_connect = guard_module.sqlite3.connect
    calls = 0

    def mutate_then_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        with source.open("r+b") as database:
            database.seek(0)
            database.write(b"changed!")
            database.flush()
            os.fsync(database.fileno())
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(guard_module.sqlite3, "connect", mutate_then_connect)

    with pytest.raises(SQLiteSourceGuardError, match="while being opened"):
        SQLiteSourceGuard(source, expected_sha256)
    assert calls == 1


def test_recheck_does_not_hash_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)
    original_hash_file = guard_module._hash_file
    calls = 0

    def counting_hash(path: Path):
        nonlocal calls
        calls += 1
        return original_hash_file(path)

    monkeypatch.setattr(guard_module, "_hash_file", counting_hash)
    with SQLiteSourceGuard(source, expected_sha256) as guard:
        assert calls == 1
        guard.recheck()
        guard.recheck()
        assert calls == 1


def test_verify_final_hashes_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)
    original_hash_file = guard_module._hash_file
    calls = 0

    def counting_hash(path: Path):
        nonlocal calls
        calls += 1
        return original_hash_file(path)

    monkeypatch.setattr(guard_module, "_hash_file", counting_hash)
    with SQLiteSourceGuard(source, expected_sha256) as guard:
        assert calls == 1
        guard.verify_final()
        assert calls == 2


def test_recheck_rejects_replacement_even_when_bytes_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)

    with SQLiteSourceGuard(source, expected_sha256) as guard:
        original_read = guard_module._FileIdentity.read

        def replacement_identity(path: Path):
            identity = original_read(path)
            return replace(identity, inode=identity.inode + 1)

        monkeypatch.setattr(guard_module._FileIdentity, "read", replacement_identity)

        with pytest.raises(SQLiteSourceGuardError, match="replaced"):
            guard.recheck()


def test_recheck_rejects_same_size_mutation(tmp_path: Path) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)

    with SQLiteSourceGuard(source, expected_sha256) as guard:
        contents = source.read_bytes()
        offset = contents.index(b"sealed")
        with source.open("r+b") as database:
            database.seek(offset)
            database.write(b"change")
            database.flush()
            os.fsync(database.fileno())

        with pytest.raises(SQLiteSourceGuardError, match="changed or was replaced"):
            guard.recheck()


def test_verify_final_rejects_mutation_even_if_identity_appears_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)

    with SQLiteSourceGuard(source, expected_sha256) as guard:
        sealed_identity = guard_module._FileIdentity.read(source)
        contents = source.read_bytes()
        offset = contents.index(b"sealed")
        with source.open("r+b") as database:
            database.seek(offset)
            database.write(b"change")
            database.flush()
            os.fsync(database.fileno())
        monkeypatch.setattr(guard_module._FileIdentity, "read", lambda path: sealed_identity)

        guard.recheck()
        with pytest.raises(SQLiteSourceGuardError, match="SHA-256 mismatch"):
            guard.verify_final()


def test_recheck_rejects_wal_created_after_open(tmp_path: Path) -> None:
    source = tmp_path / "sealed.db"
    expected_sha256 = _database(source)

    with SQLiteSourceGuard(source, expected_sha256) as guard:
        Path(f"{source}-wal").touch()

        with pytest.raises(SQLiteSourceGuardError, match="cannot be verified"):
            guard.recheck()
