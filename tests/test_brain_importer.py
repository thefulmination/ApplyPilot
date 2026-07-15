from __future__ import annotations

import hashlib
import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
import uuid
from copy import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

import applypilot.brain.importer as importer_module
from applypilot.brain.importer import (
    BatchRequest,
    CompositeCursor,
    ImportCheckpoint,
    IntegerCursor,
    OversizedManifestCursorError,
    OversizedSourceRowError,
    SealedSnapshotReceipt,
    SnapshotChangedError,
    SnapshotError,
    SnapshotReader,
    SourceSchemaError,
    SourceManifest,
    TextCursor,
    canonical_json,
    cursor_from_canonical_json,
    seal_sqlite_snapshot,
)


SOURCE_SCHEMAS = {
    "jobs": "url TEXT PRIMARY KEY, title TEXT",
    "applications": "id INTEGER PRIMARY KEY, job_url TEXT",
    "application_events": "id INTEGER PRIMARY KEY, job_url TEXT",
    "email_events": "message_id TEXT PRIMARY KEY, subject TEXT",
    "email_event_reviews": "id INTEGER PRIMARY KEY, message_id TEXT",
    "reviewed_outcomes": "event_id TEXT, job_url TEXT, PRIMARY KEY(event_id, job_url)",
    "research_labels": "id TEXT PRIMARY KEY, job_url TEXT",
    "research_label_confidence": "label_id TEXT PRIMARY KEY, weight REAL",
    "research_pairwise_labels": "id TEXT PRIMARY KEY, winner TEXT",
    "research_kg_artifacts": "kg_version TEXT PRIMARY KEY, compact_kg_json BLOB",
    "research_kg_runs": "kg_version TEXT PRIMARY KEY, source TEXT",
    "research_scores": "id INTEGER PRIMARY KEY, job_url TEXT",
    "decision_policy_versions": "policy_version TEXT PRIMARY KEY, lane TEXT",
    "job_decisions": "decision_id TEXT PRIMARY KEY, job_url TEXT",
}


@pytest.fixture
def source_backup(tmp_path: Path) -> Path:
    path = tmp_path / "brain backup.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 17")
        for table, columns in SOURCE_SCHEMAS.items():
            conn.execute(f'CREATE TABLE "{table}" ({columns})')
        conn.executemany(
            "INSERT INTO jobs(url, title) VALUES (?, ?)",
            [("job-a", "A"), ("job-b", "B"), ("job-c", "C")],
        )
        conn.executemany(
            "INSERT INTO applications(id, job_url) VALUES (?, ?)",
            [(1, "job-a"), (3, "job-c")],
        )
        conn.executemany(
            "INSERT INTO reviewed_outcomes(event_id, job_url) VALUES (?, ?)",
            [("mail-a", "job-a"), ("mail-a", "job-b"), ("mail-b", "job-a")],
        )
        conn.execute(
            "INSERT INTO research_kg_artifacts(kg_version, compact_kg_json) VALUES (?, ?)",
            ("kg-1", b"\x00\xff"),
        )
        conn.executemany(
            "INSERT INTO research_scores(id, job_url) VALUES (?, ?)",
            [(2, "job-a"), (5, "job-c")],
        )
        conn.commit()
    return path


def _source_binding(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


_SEALED: dict[Path, tuple[tuple[int, int], SealedSnapshotReceipt]] = {}


def _receipt(path: Path) -> SealedSnapshotReceipt:
    identity = (path.stat().st_size, path.stat().st_mtime_ns)
    cached = _SEALED.get(path)
    if cached is not None and cached[0] == identity:
        return cached[1]
    destination = path.with_name(f"{path.name}.sealed-{len(_SEALED)}.db")
    receipt = seal_sqlite_snapshot(path, destination)
    _SEALED[path] = (identity, receipt)
    return receipt


def _reader(path: Path, *, manifest_cursor_max_bytes: int = 64 * 1024) -> SnapshotReader:
    return SnapshotReader(_receipt(path), manifest_cursor_max_bytes=manifest_cursor_max_bytes)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode_manifest_object(value: dict) -> SourceManifest:
    payload = canonical_json(value)
    return SourceManifest.from_canonical_json(payload, _sha256(payload))


def _unchecked_integer_cursor(value: int) -> IntegerCursor:
    cursor = object.__new__(IntegerCursor)
    object.__setattr__(cursor, "value", value)
    return cursor


def test_manifest_fingerprints_explicit_backup_and_all_authority_domains(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    assert manifest.version == 2
    assert manifest.manifest_cursor_max_bytes == 64 * 1024
    assert Path(manifest.source.path) == reader.path
    assert manifest.source.size == reader.receipt.size
    assert len(manifest.source.sha256) == 64
    assert manifest.source.quick_check == "ok"
    assert manifest.source.page_count > 0
    assert manifest.source.page_size == 4096
    assert manifest.source.schema_version > 0
    assert manifest.source.user_version == 17
    assert [table.name for table in manifest.tables] == list(SOURCE_SCHEMAS)
    assert {table.name: table.row_count for table in manifest.tables} == {
        name: {
            "jobs": 3,
            "applications": 2,
            "reviewed_outcomes": 3,
            "research_kg_artifacts": 1,
            "research_scores": 2,
        }.get(name, 0)
        for name in SOURCE_SCHEMAS
    }
    assert manifest.table("jobs").upper_bound == TextCursor("job-c")
    assert manifest.table("applications").upper_bound == IntegerCursor(3)
    assert manifest.table("application_events").row_count == 0
    assert manifest.table("application_events").upper_bound is None
    assert manifest.table("reviewed_outcomes").upper_bound == CompositeCursor(("mail-b", "job-a"))
    assert manifest.table("research_scores").upper_bound == IntegerCursor(5)
    assert len(manifest.sha256) == 64
    assert json.loads(manifest.canonical_json)["version"] == 2


def test_reader_requires_existing_file_and_never_uses_a_default_path(tmp_path: Path):
    with pytest.raises(TypeError):
        SnapshotReader(tmp_path / "missing.db")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SnapshotReader(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sha256", "size"),
    [
        (None, 1),
        ("0" * 64, None),
        ("A" * 64, 1),
        ("0" * 63, 1),
        ("g" * 64, 1),
        ("0" * 64, -1),
        ("0" * 64, True),
    ],
)
def test_reader_rejects_absent_partial_or_invalid_source_bindings(source_backup: Path, sha256, size):
    receipt = _receipt(source_backup)
    with pytest.raises((TypeError, ValueError)):
        SnapshotReader(replace(receipt, sha256=sha256, size=size))


def test_text_keyset_batches_are_stable_and_resume_without_offset(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    request = BatchRequest(manifest.sha256, "jobs", None, 2)

    first = reader.read_batch(manifest, request)
    repeated = reader.read_batch(manifest, request)
    resumed = _reader(source_backup).read_batch(
        manifest,
        BatchRequest(manifest.sha256, "jobs", first.next_cursor, 2),
    )

    assert [row["url"] for row in first.rows] == ["job-a", "job-b"]
    assert first == repeated
    assert _reader(source_backup).read_batch(manifest, request) == first
    assert first.identity_sha256 == repeated.identity_sha256
    assert first.rows_sha256 == repeated.rows_sha256
    assert first.next_cursor == TextCursor("job-b")
    assert [row["url"] for row in resumed.rows] == ["job-c"]
    assert resumed.next_cursor == TextCursor("job-c")
    assert (
        reader.read_batch(
            manifest,
            BatchRequest(manifest.sha256, "jobs", resumed.next_cursor, 2),
        ).rows
        == ()
    )

    checkpoint = ImportCheckpoint.from_batch(first)
    assert checkpoint.cursor == TextCursor("job-b")
    assert len(checkpoint.sha256) == 64
    assert json.loads(checkpoint.canonical_json)["version"] == 1


def test_integer_and_composite_keyset_cursors(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    applications = reader.read_batch(
        manifest,
        BatchRequest(manifest.sha256, "applications", IntegerCursor(1), 10),
    )
    scores = reader.read_batch(
        manifest,
        BatchRequest(manifest.sha256, "research_scores", IntegerCursor(2), 10),
    )
    first_outcomes = reader.read_batch(
        manifest,
        BatchRequest(manifest.sha256, "reviewed_outcomes", None, 2),
    )
    resumed_outcomes = reader.read_batch(
        manifest,
        BatchRequest(manifest.sha256, "reviewed_outcomes", first_outcomes.next_cursor, 2),
    )

    assert [row["id"] for row in applications.rows] == [3]
    assert applications.next_cursor == IntegerCursor(3)
    assert [row["id"] for row in scores.rows] == [5]
    assert ImportCheckpoint.from_batch(scores).cursor == IntegerCursor(5)
    assert [(row["event_id"], row["job_url"]) for row in first_outcomes.rows] == [
        ("mail-a", "job-a"),
        ("mail-a", "job-b"),
    ]
    assert first_outcomes.next_cursor == CompositeCursor(("mail-a", "job-b"))
    assert [(row["event_id"], row["job_url"]) for row in resumed_outcomes.rows] == [("mail-b", "job-a")]


def test_cursor_integer_domain_accepts_only_javascript_safe_identity_values():
    maximum = 2**53 - 1

    assert IntegerCursor(maximum).value == maximum
    assert IntegerCursor(-maximum).value == -maximum
    assert CompositeCursor(("key", maximum)).values == ("key", maximum)
    for value in (2**53, -(2**53), 2**60):
        with pytest.raises(ValueError, match="safe integer") as raised:
            IntegerCursor(value)
        assert len(str(raised.value)) < 160
        with pytest.raises(ValueError, match="safe integer"):
            CompositeCursor(("key", value))


def test_manifest_producer_safe_integer_upper_bound_round_trips_its_decoder(source_backup: Path):
    maximum = 2**53 - 1
    with closing(sqlite3.connect(source_backup)) as connection:
        connection.execute("INSERT INTO applications(id, job_url) VALUES (?, ?)", (maximum, "job-max"))
        connection.commit()

    manifest = _reader(source_backup).capture_manifest()

    assert manifest.table("applications").upper_bound == IntegerCursor(maximum)
    assert SourceManifest.from_canonical_json(manifest.canonical_json, manifest.sha256) == manifest


@pytest.mark.parametrize("unsafe_cursor", [2**53, 2**60])
def test_manifest_rejects_unsafe_integer_key_before_upper_bound_materialization(
    source_backup: Path,
    monkeypatch,
    unsafe_cursor: int,
):
    with closing(sqlite3.connect(source_backup)) as connection:
        connection.execute("INSERT INTO applications(id, job_url) VALUES (?, ?)", (unsafe_cursor, "unsafe"))
        connection.commit()
    reader = _reader(source_backup)
    original_cursor_from_values = reader._cursor_from_values

    def must_not_materialize(table, values):
        if table.name == "applications":
            raise AssertionError("unsafe upper bound was materialized")
        return original_cursor_from_values(table, values)

    monkeypatch.setattr(reader, "_cursor_from_values", must_not_materialize)
    with pytest.raises(ValueError, match="safe integer") as raised:
        reader.capture_manifest()
    assert len(str(raised.value)) < 160
    assert str(unsafe_cursor) not in str(raised.value)


@pytest.mark.parametrize(
    ("table", "cursor"),
    [
        ("jobs", IntegerCursor(1)),
        ("applications", TextCursor("1")),
        ("reviewed_outcomes", CompositeCursor(("mail-a",))),
        ("reviewed_outcomes", CompositeCursor(("mail-a", 1))),
    ],
)
def test_resumable_cursors_are_typed_and_validated(source_backup: Path, table: str, cursor):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    with pytest.raises(ValueError, match="cursor"):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, table, cursor, 10))


def test_request_validation_binds_manifest_table_and_batch_size(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    with pytest.raises(ValueError, match="manifest"):
        reader.read_batch(manifest, BatchRequest("0" * 64, "jobs", None, 1))
    with pytest.raises(ValueError, match="source table"):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, "sqlite_master", None, 1))
    with pytest.raises(ValueError, match="batch size"):
        BatchRequest(manifest.sha256, "jobs", None, 0)
    with pytest.raises(ValueError, match="max batch bytes"):
        BatchRequest(manifest.sha256, "jobs", None, 1, max_batch_bytes=1)
    batch = reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 1))
    checkpoint = ImportCheckpoint.from_batch(batch)
    with pytest.raises(ValueError, match="cursor"):
        replace(checkpoint, cursor=IntegerCursor(1))
    with pytest.raises(ValueError, match="checkpoint version"):
        replace(checkpoint, version=2)


def test_changed_or_replaced_snapshot_is_rejected_during_run(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    with closing(sqlite3.connect(reader.path)) as conn:
        conn.execute("UPDATE jobs SET title = 'tampered' WHERE url = 'job-a'")
        conn.commit()
    os.utime(reader.path, None)

    with pytest.raises(SnapshotChangedError):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 2))


def test_replaced_snapshot_path_is_rejected_even_when_bytes_match(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    replacement = reader.path.with_suffix(".replacement")
    shutil.copyfile(reader.path, replacement)
    os.replace(replacement, reader.path)

    with pytest.raises(SnapshotChangedError):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 2))


def test_sealed_mutation_between_fresh_hash_and_open_is_rejected(source_backup: Path, monkeypatch):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    original_hash_file = importer_module._hash_file

    def hash_then_mutate_sealed(path):
        result = original_hash_file(path)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE jobs SET title='raced' WHERE url='job-a'")
            connection.commit()
        return result

    monkeypatch.setattr(importer_module, "_hash_file", hash_then_mutate_sealed)
    with pytest.raises(SnapshotChangedError):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 1))


def test_expected_snapshot_hash_and_size_are_verified(source_backup: Path):
    receipt = _receipt(source_backup)
    with pytest.raises(SnapshotError, match="SHA-256 mismatch"):
        SnapshotReader(replace(receipt, sha256="0" * 64)).capture_manifest()
    with pytest.raises(SnapshotError, match="size mismatch"):
        SnapshotReader(replace(receipt, size=receipt.size + 1)).capture_manifest()


def test_extractors_do_not_write_sqlite_or_create_sidecars(source_backup: Path):
    reader = _reader(source_backup)
    before = reader.path.read_bytes()
    manifest = reader.capture_manifest()
    batch = reader.read_batch(manifest, BatchRequest(manifest.sha256, "research_kg_artifacts", None, 10))

    assert batch.rows[0]["compact_kg_json"] == b"\x00\xff"
    assert reader.path.read_bytes() == before
    assert not Path(f"{reader.path}-journal").exists()
    assert not Path(f"{reader.path}-wal").exists()
    assert not Path(f"{reader.path}-shm").exists()


def test_canonical_json_is_ordered_compact_and_binary_safe():
    assert canonical_json({"z": b"\xff", "a": [2, 1]}) == '{"a":[2,1],"z":{"$bytes":"/w=="}}'


def test_strict_cursor_decoder_rejects_hash_shape_type_and_noncanonical_json():
    payload = canonical_json(TextCursor("job-b"))
    assert cursor_from_canonical_json(payload, _sha256(payload), "jobs") == TextCursor("job-b")

    invalid_objects = [
        {"kind": "text"},
        {"kind": "text", "value": "job-b", "extra": 1},
        {"kind": "integer", "value": True},
        {"kind": "unknown", "value": "job-b"},
    ]
    for value in invalid_objects:
        invalid = canonical_json(value)
        with pytest.raises(ValueError):
            cursor_from_canonical_json(invalid, _sha256(invalid), "jobs")
    with pytest.raises(ValueError, match="canonical"):
        cursor_from_canonical_json('{"value":"job-b", "kind":"text"}', _sha256(payload), "jobs")
    with pytest.raises(ValueError, match="SHA-256"):
        cursor_from_canonical_json(payload, "0" * 64, "jobs")


@pytest.mark.parametrize("unsafe_cursor", [2**53, 2**60])
def test_decoder_checkpoint_and_batch_inputs_reject_unsafe_integer_cursors(
    source_backup: Path,
    unsafe_cursor: int,
):
    payload = canonical_json({"kind": "integer", "value": unsafe_cursor})
    with pytest.raises(ValueError, match="safe integer"):
        cursor_from_canonical_json(payload, _sha256(payload), "applications")

    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    forged = _unchecked_integer_cursor(unsafe_cursor)
    with pytest.raises(ValueError, match="safe integer"):
        BatchRequest(manifest.sha256, "applications", forged, 1)

    request = BatchRequest(manifest.sha256, "applications", IntegerCursor(1), 1)
    object.__setattr__(request, "after", forged)
    with pytest.raises(ValueError, match="safe integer"):
        reader.read_batch(manifest, request)

    batch = reader.read_batch(
        manifest,
        BatchRequest(manifest.sha256, "applications", IntegerCursor(1), 1),
    )
    checkpoint = ImportCheckpoint.from_batch(batch)
    object.__setattr__(checkpoint, "cursor", forged)
    with pytest.raises(ValueError, match="safe integer"):
        checkpoint.__post_init__()

    checkpoint_value = json.loads(ImportCheckpoint.from_batch(batch).canonical_json)
    checkpoint_value["cursor"] = {"kind": "integer", "value": unsafe_cursor}
    checkpoint_payload = canonical_json(checkpoint_value)
    with pytest.raises(ValueError, match="safe integer"):
        ImportCheckpoint.from_canonical_json(checkpoint_payload, _sha256(checkpoint_payload))


def test_strict_manifest_decoder_round_trip_and_schema_rejections(source_backup: Path):
    manifest = _reader(source_backup).capture_manifest()
    decoded = SourceManifest.from_canonical_json(manifest.canonical_json, manifest.sha256)
    assert decoded == manifest

    with pytest.raises(ValueError, match="canonical"):
        SourceManifest.from_canonical_json(json.dumps(json.loads(manifest.canonical_json)), manifest.sha256)
    with pytest.raises(ValueError, match="SHA-256"):
        SourceManifest.from_canonical_json(manifest.canonical_json, "0" * 64)

    invalid_objects = []
    base = json.loads(manifest.canonical_json)
    for key in ("version", "source", "tables", "manifest_cursor_max_bytes"):
        value = json.loads(manifest.canonical_json)
        del value[key]
        invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["extra"] = True
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["version"] = 1
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["manifest_cursor_max_bytes"] = "large"
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["source"]["size"] = "large"
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["tables"] = value["tables"][:-1]
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["tables"][0]["key_columns"] = ["title"]
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["tables"][0]["upper_bound"] = {"kind": "integer", "value": 3}
    invalid_objects.append(value)
    value = json.loads(manifest.canonical_json)
    value["tables"][0]["row_count"] = 0
    invalid_objects.append(value)
    for value in invalid_objects:
        with pytest.raises(ValueError):
            _decode_manifest_object(value)

    for field, replacement in {
        "path": str(source_backup.with_name("other.db")),
        "sha256": "0" * 64,
        "size": base["source"]["size"] + 1,
        "quick_check": "corrupt",
        "page_count": base["source"]["page_count"] + 1,
        "page_size": base["source"]["page_size"] * 2,
        "schema_version": base["source"]["schema_version"] + 1,
        "user_version": base["source"]["user_version"] + 1,
    }.items():
        tampered = json.loads(manifest.canonical_json)
        tampered["source"][field] = replacement
        with pytest.raises(ValueError, match="SHA-256"):
            SourceManifest.from_canonical_json(canonical_json(tampered), manifest.sha256)


def test_strict_checkpoint_decoder_recomputes_every_batch_identity_field(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    batch = reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 2))
    checkpoint = ImportCheckpoint.from_batch(batch)
    decoded = ImportCheckpoint.from_canonical_json(checkpoint.canonical_json, checkpoint.sha256)
    assert decoded == checkpoint
    assert decoded.resume_request(manifest) == BatchRequest(manifest.sha256, "jobs", TextCursor("job-b"), 2)
    with pytest.raises(ValueError, match="SHA-256"):
        ImportCheckpoint.from_canonical_json(checkpoint.canonical_json, "0" * 64)

    mutations = {
        "manifest_sha256": "0" * 64,
        "source_table": "applications",
        "previous_cursor": {"kind": "text", "value": "job-a"},
        "batch_size": 3,
        "max_batch_bytes": checkpoint.max_batch_bytes + 1,
        "upper_bound": {"kind": "text", "value": "job-z"},
        "cursor": {"kind": "text", "value": "job-a"},
        "row_count": 3,
        "canonical_byte_count": checkpoint.canonical_byte_count + 1,
        "rows_sha256": "1" * 64,
        "batch_identity_sha256": "2" * 64,
    }
    for field, replacement in mutations.items():
        tampered = json.loads(checkpoint.canonical_json)
        tampered[field] = replacement
        payload = canonical_json(tampered)
        with pytest.raises(ValueError):
            ImportCheckpoint.from_canonical_json(payload, _sha256(payload))

    invalid = json.loads(checkpoint.canonical_json)
    invalid["extra"] = True
    with pytest.raises(ValueError):
        payload = canonical_json(invalid)
        ImportCheckpoint.from_canonical_json(payload, _sha256(payload))
    invalid = json.loads(checkpoint.canonical_json)
    del invalid["cursor"]
    with pytest.raises(ValueError):
        payload = canonical_json(invalid)
        ImportCheckpoint.from_canonical_json(payload, _sha256(payload))
    with pytest.raises(ValueError, match="canonical"):
        ImportCheckpoint.from_canonical_json(
            json.dumps(json.loads(checkpoint.canonical_json)),
            checkpoint.sha256,
        )


def test_checkpoint_rejects_rehashed_out_of_bounds_cursor_and_resume_rechecks_integrity(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    batch = reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 2))
    checkpoint = ImportCheckpoint.from_batch(batch)
    out_of_bounds = TextCursor("job-z")
    identity_payload = importer_module._batch_identity_payload(
        BatchRequest(manifest.sha256, "jobs", None, 2),
        checkpoint.upper_bound,
        out_of_bounds,
        checkpoint.row_count,
        checkpoint.canonical_byte_count,
        checkpoint.rows_sha256,
    )
    identity_sha256 = _sha256(canonical_json(identity_payload))
    with pytest.raises(ValueError, match="upper bound"):
        replace(
            checkpoint,
            cursor=out_of_bounds,
            batch_identity_sha256=identity_sha256,
        )

    for field, replacement in {
        "manifest_sha256": "0" * 64,
        "source_table": "applications",
        "cursor": IntegerCursor(3),
        "batch_identity_sha256": "1" * 64,
    }.items():
        tampered = copy(checkpoint)
        object.__setattr__(tampered, field, replacement)
        with pytest.raises(ValueError):
            tampered.resume_request(manifest)


def test_true_serialized_restart_decodes_and_resumes_on_fresh_reader(source_backup: Path):
    original_reader = _reader(source_backup)
    original_manifest = original_reader.capture_manifest()
    first = original_reader.read_batch(
        original_manifest,
        BatchRequest(original_manifest.sha256, "jobs", None, 2),
    )
    original_checkpoint = ImportCheckpoint.from_batch(first)
    manifest_json, manifest_sha256 = original_manifest.canonical_json, original_manifest.sha256
    checkpoint_json, checkpoint_sha256 = original_checkpoint.canonical_json, original_checkpoint.sha256
    del original_reader, original_manifest, original_checkpoint, first

    manifest = SourceManifest.from_canonical_json(manifest_json, manifest_sha256)
    checkpoint = ImportCheckpoint.from_canonical_json(checkpoint_json, checkpoint_sha256)
    fresh_reader = _reader(source_backup)
    resumed = fresh_reader.read_batch(manifest, checkpoint.resume_request(manifest))

    assert [row["url"] for row in resumed.rows] == ["job-c"]
    assert resumed.next_cursor == TextCursor("job-c")


def test_reader_rehashes_sealed_file_before_every_batch(source_backup: Path, monkeypatch):
    calls = 0
    original_hash_file = importer_module._hash_file

    def counted_hash_file(path):
        nonlocal calls
        calls += 1
        return original_hash_file(path)

    monkeypatch.setattr(importer_module, "_hash_file", counted_hash_file)
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    first = reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 1))
    reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", first.next_cursor, 1))
    assert calls == 4

    _reader(source_backup).read_batch(manifest, BatchRequest(manifest.sha256, "jobs", first.next_cursor, 1))
    assert calls == 5


def test_committed_wal_only_row_is_included_in_sealed_snapshot(source_backup: Path, tmp_path: Path):
    writer = sqlite3.connect(source_backup)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO jobs(url, title) VALUES ('job-wal-only', 'committed in WAL')")
        writer.commit()
        assert writer.execute("SELECT title FROM jobs WHERE url='job-wal-only'").fetchone()[0] == "committed in WAL"
        assert Path(f"{source_backup}-wal").exists()

        receipt = seal_sqlite_snapshot(source_backup, tmp_path / "sealed-wal.db")
        reader = SnapshotReader(receipt)
        manifest = reader.capture_manifest()
        batch = reader.read_batch(
            manifest,
            BatchRequest(manifest.sha256, "jobs", TextCursor("job-c"), 10),
        )
        assert [row["url"] for row in batch.rows] == ["job-wal-only"]
    finally:
        writer.close()


def test_source_mutation_during_backup_is_transactionally_consistent(source_backup: Path, tmp_path: Path, monkeypatch):
    with closing(sqlite3.connect(source_backup)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE backup_padding(value BLOB)")
        connection.execute("INSERT INTO backup_padding VALUES (zeroblob(8000000))")
        connection.commit()
    original_backup = importer_module._backup_database
    mutation_started = False

    def backup_with_concurrent_commit(source, destination):
        nonlocal mutation_started

        def progress(_status, remaining, _total):
            nonlocal mutation_started
            if mutation_started or remaining == 0:
                return
            mutation_started = True

            def mutate():
                with closing(sqlite3.connect(source_backup)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute("UPDATE jobs SET title='after-a' WHERE url='job-a'")
                    writer.execute("UPDATE jobs SET title='after-b' WHERE url='job-b'")
                    writer.commit()

            worker = threading.Thread(target=mutate)
            worker.start()
            worker.join()

        source.backup(destination, pages=1, progress=progress, sleep=0.001)

    monkeypatch.setattr(importer_module, "_backup_database", backup_with_concurrent_commit)
    receipt = seal_sqlite_snapshot(source_backup, tmp_path / "sealed-consistent.db")
    monkeypatch.setattr(importer_module, "_backup_database", original_backup)

    with closing(sqlite3.connect(receipt.path)) as sealed:
        titles = tuple(
            row[0] for row in sealed.execute("SELECT title FROM jobs WHERE url IN ('job-a','job-b') ORDER BY url")
        )
    assert mutation_started
    assert receipt.source_changed_during_backup is True
    assert titles in {("A", "B"), ("after-a", "after-b")}


def test_subsequent_source_mutation_and_sidecars_do_not_affect_sealed_reader(source_backup: Path):
    receipt = _receipt(source_backup)
    with closing(sqlite3.connect(source_backup)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("INSERT INTO jobs(url, title) VALUES ('job-later', 'later')")
        writer.commit()
        reader = SnapshotReader(receipt)
        manifest = reader.capture_manifest()
        assert manifest.table("jobs").row_count == 3


def test_sealed_destination_rejects_overwrite_and_symlink(source_backup: Path, tmp_path: Path):
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"occupied")
    with pytest.raises(FileExistsError):
        seal_sqlite_snapshot(source_backup, existing)

    symlink = tmp_path / "linked.db"
    try:
        symlink.symlink_to(existing)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises((FileExistsError, SnapshotError)):
        seal_sqlite_snapshot(source_backup, symlink)


def test_sealed_destination_rejects_junction_escape(source_backup: Path, tmp_path: Path):
    requested_root = tmp_path / "requested"
    outside = tmp_path / "outside"
    requested_root.mkdir()
    outside.mkdir()
    junction = requested_root / "escape"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip(f"junction creation unavailable: {created.stderr}")
    else:
        junction.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SnapshotError, match="reparse|symlink|junction"):
        seal_sqlite_snapshot(source_backup, junction / "escaped.db")
    assert not (outside / "escaped.db").exists()


def test_parent_component_replacement_is_blocked_or_detected(source_backup: Path, tmp_path: Path, monkeypatch):
    container = tmp_path / "container"
    seal_parent = container / "seals"
    seal_parent.mkdir(parents=True)
    destination = seal_parent / "sealed.db"
    moved = tmp_path / "moved-container"
    replacement_succeeded = False

    def replace_component():
        nonlocal replacement_succeeded
        try:
            os.rename(container, moved)
            (container / "seals").mkdir(parents=True)
            replacement_succeeded = True
        except OSError:
            replacement_succeeded = False

    monkeypatch.setattr(importer_module, "_before_publish", replace_component)
    if os.name == "nt":
        receipt = seal_sqlite_snapshot(source_backup, destination)
        assert replacement_succeeded is False
        assert Path(receipt.path) == destination
    else:
        with pytest.raises(SnapshotChangedError, match="parent"):
            seal_sqlite_snapshot(source_backup, destination)
        assert replacement_succeeded is True
        assert not destination.exists()


def test_online_backup_audits_db_wal_and_ephemeral_shm_without_source_mutation(source_backup: Path, tmp_path: Path):
    writer = sqlite3.connect(source_backup)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO jobs(url, title) VALUES ('job-wal-audit', 'audit')")
        writer.commit()
        wal = Path(f"{source_backup}-wal")
        assert wal.exists()
        db_before = _source_binding(source_backup)
        wal_before = _source_binding(wal)

        receipt = seal_sqlite_snapshot(source_backup, tmp_path / "online-audited.db")

        assert receipt.source_mode == "online_backup"
        assert receipt.source_changed_during_backup is False
        assert receipt.source_db_audit.before_sha256 == db_before[0]
        assert receipt.source_db_audit.after_sha256 == db_before[0]
        assert receipt.source_db_audit.changed is False
        assert receipt.source_wal_audit.before_sha256 == wal_before[0]
        assert receipt.source_wal_audit.after_sha256 == wal_before[0]
        assert receipt.source_wal_audit.changed is False
        assert receipt.source_shm_audit.ephemeral is True
        assert receipt.source_shm_audit.path == f"{source_backup.resolve()}-shm"
    finally:
        writer.close()


def test_online_backup_succeeds_under_sustained_wal_writers_with_consistent_snapshot(
    source_backup: Path, tmp_path: Path
):
    with closing(sqlite3.connect(source_backup)) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        setup.execute("PRAGMA wal_autocheckpoint=0")
        setup.execute("CREATE TABLE backup_padding(value BLOB)")
        setup.execute("INSERT INTO backup_padding VALUES (zeroblob(12000000))")
        setup.commit()
    stop = threading.Event()
    writes = 0

    def sustain_writes():
        nonlocal writes
        with closing(sqlite3.connect(source_backup, timeout=30)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            generation = 0
            while not stop.is_set():
                generation += 1
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE jobs SET title=? WHERE url='job-a'", (f"generation-{generation}",))
                writer.execute("UPDATE jobs SET title=? WHERE url='job-b'", (f"generation-{generation}",))
                writer.commit()
                writes += 1
                time.sleep(0.001)

    worker = threading.Thread(target=sustain_writes)
    worker.start()
    while writes < 5:
        time.sleep(0.001)
    try:
        receipt = seal_sqlite_snapshot(source_backup, tmp_path / "sustained.db")
    finally:
        stop.set()
        worker.join(timeout=30)

    with closing(sqlite3.connect(receipt.path)) as sealed:
        titles = tuple(
            row[0] for row in sealed.execute("SELECT title FROM jobs WHERE url IN ('job-a','job-b') ORDER BY url")
        )
        assert sealed.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert writes >= 5
    assert titles[0] == titles[1]
    assert receipt.source_changed_during_backup is True


def test_immutable_no_filesystem_write_mode_requires_checkpointed_sidecar_free_source(
    source_backup: Path, tmp_path: Path
):
    before = _source_binding(source_backup)
    receipt = seal_sqlite_snapshot(
        source_backup,
        tmp_path / "immutable.db",
        source_mode="immutable_no_filesystem_write",
    )
    assert receipt.source_mode == "immutable_no_filesystem_write"
    assert receipt.source_db_audit.before_sha256 == receipt.source_db_audit.after_sha256 == before[0]
    assert receipt.source_wal_audit.before_exists is False
    assert receipt.source_shm_audit.before_exists is False
    assert _source_binding(source_backup) == before

    Path(f"{source_backup}-wal").write_bytes(b"")
    with pytest.raises(SnapshotError, match="sidecar"):
        seal_sqlite_snapshot(
            source_backup,
            tmp_path / "immutable-rejected.db",
            source_mode="immutable_no_filesystem_write",
        )


def _crash_after_publish(source_backup: Path, destination: Path) -> subprocess.CompletedProcess:
    script = (
        "import os,sys\n"
        "import applypilot.brain.importer as m\n"
        "m._after_publish=lambda: os._exit(74)\n"
        "m.seal_sqlite_snapshot(sys.argv[1],sys.argv[2])\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(source_backup), str(destination)],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=False,
    )


def test_retry_after_post_publish_crash_reconstructs_same_receipt(source_backup: Path, tmp_path: Path):
    destination = tmp_path / "recovered.db"
    crashed = _crash_after_publish(source_backup, destination)
    assert crashed.returncode == 74
    assert destination.exists()
    markers = list(tmp_path.glob(".applypilot-seal-*.receipt.json"))
    assert len(markers) == 1
    source_backup.unlink()

    first_retry = seal_sqlite_snapshot(source_backup, destination)
    second_retry = seal_sqlite_snapshot(source_backup, destination)

    assert first_retry == second_retry
    assert Path(first_retry.path) == destination
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == first_retry.sha256
    with closing(sqlite3.connect(destination)) as sealed:
        assert sealed.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_post_publish_retry_never_overwrites_mismatched_destination(source_backup: Path, tmp_path: Path):
    destination = tmp_path / "mismatched.db"
    assert _crash_after_publish(source_backup, destination).returncode == 74
    with destination.open("r+b") as corrupted:
        corrupted.seek(0)
        corrupted.write(b"BROKEN!!")
        corrupted.flush()
        os.fsync(corrupted.fileno())
    corrupted_bytes = destination.read_bytes()

    with pytest.raises(SnapshotChangedError, match="published sealed destination"):
        seal_sqlite_snapshot(source_backup, destination)

    assert destination.read_bytes() == corrupted_bytes


def test_forged_marker_never_deletes_preexisting_matching_file(source_backup: Path, tmp_path: Path):
    destination = tmp_path / "forgery-target.db"
    operation_id = str(uuid.uuid4())
    temporary = tmp_path / f".applypilot-seal-{operation_id}.sealing"
    marker = tmp_path / f".applypilot-seal-{operation_id}.receipt.json"
    preexisting = tmp_path / "preexisting.bin"
    preexisting.write_bytes(b"do not delete")
    os.link(preexisting, temporary)
    marker.write_text(
        canonical_json(
            {
                "version": 1,
                "operation_id": operation_id,
                "destination_name": destination.name,
                "temporary_name": temporary.name,
                "temporary_sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                "temporary_size": temporary.stat().st_size,
                "temporary_identity": [
                    str(temporary.stat().st_dev),
                    str(temporary.stat().st_ino),
                    str(temporary.stat().st_size),
                    str(temporary.stat().st_mtime_ns),
                    str(temporary.stat().st_ctime_ns),
                ],
                "receipt": {},
            }
        ),
        encoding="utf-8",
    )

    receipt = seal_sqlite_snapshot(source_backup, destination)

    assert Path(receipt.path) == destination
    assert preexisting.read_bytes() == b"do not delete"
    assert temporary.read_bytes() == b"do not delete"
    assert marker.exists()


def test_batch_is_bounded_by_canonical_bytes_and_hashes_streamed_array(source_backup: Path):
    with closing(sqlite3.connect(source_backup)) as conn:
        conn.executemany(
            "INSERT INTO jobs(url, title) VALUES (?, ?)",
            [(f"job-large-{index:02d}", "x" * 200_000) for index in range(8)],
        )
        conn.commit()
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    request = BatchRequest(
        manifest.sha256,
        "jobs",
        TextCursor("job-c"),
        100,
        max_batch_bytes=450_000,
    )

    batch = reader.read_batch(manifest, request)

    assert len(batch.rows) == 2
    assert batch.canonical_byte_count <= request.max_batch_bytes
    assert (
        batch.rows_sha256
        == hashlib.sha256(canonical_json([dict(row) for row in batch.rows]).encode("utf-8")).hexdigest()
    )
    assert batch.next_cursor == TextCursor("job-large-01")
    checkpoint = ImportCheckpoint.from_batch(batch)
    assert checkpoint.max_batch_bytes == 450_000
    assert checkpoint.canonical_byte_count == batch.canonical_byte_count


def test_single_oversized_row_returns_quarantine_contract_without_materializing_batch(source_backup: Path):
    with closing(sqlite3.connect(source_backup)) as conn:
        conn.execute("INSERT INTO jobs(url, title) VALUES (?, ?)", ("job-z", "x" * 50_000))
        conn.commit()
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    with pytest.raises(OversizedSourceRowError) as raised:
        reader.read_batch(
            manifest,
            BatchRequest(manifest.sha256, "jobs", TextCursor("job-c"), 10, max_batch_bytes=1024),
        )

    error = raised.value
    assert error.source_table == "jobs"
    assert error.canonical_row_bytes > 50_000
    assert error.max_batch_bytes == 1024
    assert error.quarantine_contract == {
        "reason_code": "source_row_exceeds_batch_byte_limit",
        "source_table": "jobs",
        "source_locator": {
            "key_types": ("text",),
            "key_byte_lengths": (5,),
        },
        "canonical_row_bytes": error.canonical_row_bytes,
        "max_batch_bytes": 1024,
        "size_measurement": "canonical_upper_bound",
    }
    with pytest.raises(TypeError):
        error.quarantine_contract["source_locator"]["key_types"] = ()  # type: ignore[index]


def test_oversized_row_preflight_keeps_python_memory_bounded(source_backup: Path):
    huge_value = "x" * 12_000_000
    with closing(sqlite3.connect(source_backup)) as conn:
        conn.execute("INSERT INTO jobs(url, title) VALUES (?, ?)", ("job-z", huge_value))
        conn.commit()
    del huge_value
    gc.collect()
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    tracemalloc.start()
    try:
        with pytest.raises(OversizedSourceRowError) as raised:
            reader.read_batch(
                manifest,
                BatchRequest(manifest.sha256, "jobs", TextCursor("job-c"), 10, max_batch_bytes=4096),
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert raised.value.source_locator["key_byte_lengths"] == (5,)
    assert raised.value.size_measurement == "canonical_upper_bound"
    assert peak < 1_000_000


@pytest.mark.parametrize("oversized_field", ["payload", "key"])
def test_quote_heavy_payload_and_oversized_text_key_are_rejected_before_materialization(
    source_backup: Path,
    oversized_field: str,
):
    if oversized_field == "payload":
        key, title, limit = "job-z", '"' * 6_000_000, 6_500_000
    else:
        key, title, limit = '"' * 6_000_000, "small", 1_000_000
    key_length = len(key.encode())
    with closing(sqlite3.connect(source_backup)) as connection:
        connection.execute("INSERT INTO jobs(url, title) VALUES (?, ?)", (key, title))
        connection.commit()
    del key, title
    gc.collect()
    tracemalloc.start()
    try:
        reader = _reader(source_backup, manifest_cursor_max_bytes=1_000_000)
        if oversized_field == "key":
            with pytest.raises(OversizedManifestCursorError) as raised_key:
                reader.capture_manifest()
        else:
            manifest = reader.capture_manifest()
            request = BatchRequest(manifest.sha256, "jobs", TextCursor("job-c"), 1, max_batch_bytes=limit)
            with pytest.raises(OversizedSourceRowError) as raised_row:
                reader.read_batch(manifest, request)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    if oversized_field == "key":
        assert raised_key.value.source_table == "jobs"
        assert raised_key.value.key_upper_bound_bytes > 1_000_000
        assert "source_key" not in raised_key.value.quarantine_contract
    else:
        assert raised_row.value.source_locator["key_byte_lengths"] == (key_length,)
        assert "source_key" not in raised_row.value.quarantine_contract
    assert peak < 1_000_000


def test_manifest_key_bound_sql_uses_only_typeof_and_blob_length_metadata(source_backup: Path):
    table = importer_module._TABLE_BY_NAME["jobs"]
    bound_sql = SnapshotReader._manifest_key_bound_sql(table)
    normalized = bound_sql.lower()

    assert "json_quote" not in normalized
    assert "typeof" in normalized
    assert "length(cast(" in normalized
    assert " as blob))" in normalized
    with closing(sqlite3.connect(source_backup)) as connection:
        plan = connection.execute(f'EXPLAIN SELECT MAX({bound_sql}) FROM "jobs"').fetchall()
    plan_text = " ".join(str(value) for row in plan for value in row if value is not None).lower()
    assert "json_quote" not in plan_text


@pytest.mark.parametrize("table", ["jobs", "reviewed_outcomes"])
def test_keyset_batches_support_simple_and_composite_without_rowid(source_backup: Path, table: str):
    with closing(sqlite3.connect(source_backup)) as connection:
        if table == "jobs":
            connection.execute("ALTER TABLE jobs RENAME TO jobs_old")
            connection.execute("CREATE TABLE jobs(url TEXT PRIMARY KEY, title TEXT) WITHOUT ROWID")
            connection.execute("INSERT INTO jobs SELECT * FROM jobs_old")
            connection.execute("DROP TABLE jobs_old")
        else:
            connection.execute("ALTER TABLE reviewed_outcomes RENAME TO reviewed_outcomes_old")
            connection.execute(
                "CREATE TABLE reviewed_outcomes("
                "event_id TEXT, job_url TEXT, PRIMARY KEY(event_id, job_url)) WITHOUT ROWID"
            )
            connection.execute("INSERT INTO reviewed_outcomes SELECT * FROM reviewed_outcomes_old")
            connection.execute("DROP TABLE reviewed_outcomes_old")
        connection.commit()

    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    first = reader.read_batch(manifest, BatchRequest(manifest.sha256, table, None, 2))
    second = reader.read_batch(manifest, BatchRequest(manifest.sha256, table, first.next_cursor, 2))

    assert len(first.rows) == 2
    assert len(second.rows) == 1
    assert first.next_cursor != second.next_cursor


def test_oversized_without_rowid_composite_row_uses_bounded_key_metadata(source_backup: Path):
    with closing(sqlite3.connect(source_backup)) as connection:
        connection.execute("ALTER TABLE reviewed_outcomes RENAME TO reviewed_outcomes_old")
        connection.execute(
            "CREATE TABLE reviewed_outcomes("
            "event_id TEXT, job_url TEXT, payload TEXT, PRIMARY KEY(event_id, job_url)) WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO reviewed_outcomes(event_id, job_url, payload) "
            "SELECT event_id, job_url, '' FROM reviewed_outcomes_old"
        )
        connection.execute("DROP TABLE reviewed_outcomes_old")
        connection.execute(
            "INSERT INTO reviewed_outcomes VALUES ('mail-z', 'job-z', ?)",
            ("x" * 2_000_000,),
        )
        connection.commit()
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    with pytest.raises(OversizedSourceRowError) as raised:
        reader.read_batch(
            manifest,
            BatchRequest(
                manifest.sha256,
                "reviewed_outcomes",
                CompositeCursor(("mail-b", "job-a")),
                1,
                max_batch_bytes=1024,
            ),
        )

    assert raised.value.source_locator == {
        "key_types": ("text", "text"),
        "key_byte_lengths": (6, 5),
    }


@pytest.mark.parametrize(
    ("table", "after"),
    [
        ("jobs", TextCursor("job-z")),
        ("applications", IntegerCursor(4)),
        ("reviewed_outcomes", CompositeCursor(("mail-z", "job-z"))),
    ],
)
def test_read_batch_rejects_after_cursor_beyond_manifest_upper_bound(source_backup: Path, table: str, after):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()

    with pytest.raises(ValueError, match="upper bound"):
        reader.read_batch(manifest, BatchRequest(manifest.sha256, table, after, 1))


def test_batch_rows_are_deeply_immutable_and_checkpoint_hash_cannot_drift(source_backup: Path):
    reader = _reader(source_backup)
    manifest = reader.capture_manifest()
    batch = reader.read_batch(manifest, BatchRequest(manifest.sha256, "jobs", None, 2))
    checkpoint = ImportCheckpoint.from_batch(batch)

    with pytest.raises(TypeError):
        batch.rows[0]["title"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        batch.rows[0]["nested"] = {"mutable": True}  # type: ignore[index]
    assert ImportCheckpoint.from_batch(batch) == checkpoint


@pytest.mark.parametrize("table", ["jobs", "reviewed_outcomes"])
def test_nonbinary_primary_key_collation_is_rejected(source_backup: Path, table: str):
    with closing(sqlite3.connect(source_backup)) as conn:
        if table == "jobs":
            conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            conn.execute("CREATE TABLE jobs(url TEXT PRIMARY KEY COLLATE NOCASE, title TEXT)")
            conn.execute("INSERT INTO jobs SELECT * FROM jobs_old")
            conn.execute("DROP TABLE jobs_old")
        else:
            conn.execute("ALTER TABLE reviewed_outcomes RENAME TO reviewed_outcomes_old")
            conn.execute(
                "CREATE TABLE reviewed_outcomes("
                "event_id TEXT COLLATE NOCASE, job_url TEXT, PRIMARY KEY(event_id, job_url))"
            )
            conn.execute("INSERT INTO reviewed_outcomes SELECT * FROM reviewed_outcomes_old")
            conn.execute("DROP TABLE reviewed_outcomes_old")
        conn.commit()

    with pytest.raises(SourceSchemaError, match="BINARY"):
        _reader(source_backup).capture_manifest()


def test_unregistered_custom_primary_key_collation_is_rejected_as_source_schema(source_backup: Path):
    with closing(sqlite3.connect(source_backup)) as conn:
        conn.create_collation("CUSTOM_ORDER", lambda left, right: (left > right) - (left < right))
        conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
        conn.execute("CREATE TABLE jobs(url TEXT PRIMARY KEY COLLATE CUSTOM_ORDER, title TEXT)")
        conn.execute("INSERT INTO jobs SELECT * FROM jobs_old")
        conn.execute("DROP TABLE jobs_old")
        conn.commit()

    with pytest.raises(SourceSchemaError, match="BINARY"):
        _reader(source_backup).capture_manifest()
