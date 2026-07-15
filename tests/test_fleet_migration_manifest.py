from __future__ import annotations

import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from applypilot.fleet import migrator


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "src" / "applypilot" / "fleet" / "migrations" / "manifest-v1.json"


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _write_manifest(root: Path, migrations: list[dict[str, object]]) -> Path:
    predecessor_files = []
    for relative in (
        "src/applypilot/fleet/schema_v3.sql",
        "src/applypilot/apply/fleet_schema.sql",
        "src/applypilot/fleet/schema.py",
    ):
        data = (REPO_ROOT / relative).read_bytes()
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        predecessor_files.append({"path": relative, "git_blob": _git_blob_id(data.replace(b"\r\n", b"\n"))})
    payload = {
        "schema_version": 1,
        "manifest_id": "applypilot-fleet-migrations-v1",
        "migration_role": "applypilot_fleet_migrator",
        "predecessor": {
            "runtime_commit": "2b3a7c83118df840dda60c9b728f29e3dc0c1b9d",
            "files": predecessor_files,
        },
        "migrations": migrations,
    }
    path = root / "manifest-v1.json"
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _migration(root: Path, migration_id: str, predecessor_id: str | None, predecessor_sha256: str | None) -> dict[str, object]:
    relative = f"src/applypilot/fleet/migrations/{migration_id}.sql"
    sql = f"CREATE TABLE migration_probe_{migration_id.split('_')[1]} (id integer PRIMARY KEY);\n"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql, encoding="utf-8", newline="\n")
    return {
        "id": migration_id,
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "predecessor_id": predecessor_id,
        "predecessor_sha256": predecessor_sha256,
        "transaction_mode": "transactional",
        "minimum_schema_contract": "fleet-v3",
        "maximum_schema_contract": "fleet-v3",
        "forward_recovery_command": f"applypilot-fleet-migrate --through {migration_id}",
    }


@pytest.fixture
def migration_pg(fleet_pg):
    with psycopg.connect(fleet_pg, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS applypilot_fleet_schema_migrations CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS applypilot_reject_migration_ledger_mutation() CASCADE")
        if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='applypilot_fleet_migrator')").fetchone()[0]:
            conn.execute("DROP OWNED BY applypilot_fleet_migrator CASCADE")
            conn.execute("DROP ROLE applypilot_fleet_migrator")
        conn.execute("CREATE ROLE applypilot_fleet_migrator NOLOGIN")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO applypilot_fleet_migrator")
    try:
        yield fleet_pg
    finally:
        with psycopg.connect(fleet_pg, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS applypilot_fleet_schema_migrations CASCADE")
            conn.execute("DROP FUNCTION IF EXISTS applypilot_reject_migration_ledger_mutation() CASCADE")
            if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='applypilot_fleet_migrator')").fetchone()[0]:
                conn.execute("DROP OWNED BY applypilot_fleet_migrator CASCADE")
                conn.execute("DROP ROLE applypilot_fleet_migrator")


def test_repository_manifest_pins_the_exact_legacy_predecessor():
    manifest = migrator.load_manifest(MANIFEST_PATH)
    assert manifest.predecessor.runtime_commit == "2b3a7c83118df840dda60c9b728f29e3dc0c1b9d"
    assert {item.path: item.git_blob for item in manifest.predecessor.files} == {
        "src/applypilot/fleet/schema_v3.sql": "0741a6e675d2ea42a3bb0d785fd4c0c444e96b3d",
        "src/applypilot/apply/fleet_schema.sql": "6eb4a84dcc05568233ee32551b28c75f13b0a17f",
        "src/applypilot/fleet/schema.py": "5637001b6457f9fc8f0c22f4220fe2e1249ff9c0",
    }
    assert tuple(item.id for item in manifest.migrations) == (
        "20260715_001_application_authority",
    )
    migrator.verify_predecessor(manifest, REPO_ROOT)


def test_manifest_rejects_duplicate_keys_unknown_fields_and_unsafe_paths(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8", newline="\n")
    with pytest.raises(migrator.ManifestError, match="duplicate"):
        migrator.load_manifest(duplicate)

    valid = json.loads(_write_manifest(tmp_path, []).read_text(encoding="utf-8"))
    valid["unexpected"] = True
    (tmp_path / "unknown.json").write_text(json.dumps(valid) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(migrator.ManifestError, match="fields"):
        migrator.load_manifest(tmp_path / "unknown.json")

    migration = _migration(tmp_path, "20260715_001_probe", None, None)
    migration["path"] = "../escape.sql"
    with pytest.raises(migrator.ManifestError, match="path"):
        migrator.load_manifest(_write_manifest(tmp_path, [migration]))


def test_manifest_rejects_reordered_duplicate_and_broken_chains(tmp_path):
    first = _migration(tmp_path, "20260715_001_probe", None, None)
    second = _migration(tmp_path, "20260715_002_probe", first["id"], first["sha256"])
    migrator.load_manifest(_write_manifest(tmp_path, [first, second]))

    with pytest.raises(migrator.ManifestError, match="strictly increasing"):
        migrator.load_manifest(_write_manifest(tmp_path, [second, first]))
    with pytest.raises(migrator.ManifestError, match="duplicate"):
        migrator.load_manifest(_write_manifest(tmp_path, [first, first]))
    broken = dict(second)
    broken["predecessor_sha256"] = "0" * 64
    with pytest.raises(migrator.ManifestError, match="predecessor"):
        migrator.load_manifest(_write_manifest(tmp_path, [first, broken]))


def test_predecessor_and_sql_checksums_are_verified_from_bytes(tmp_path):
    manifest_path = _write_manifest(tmp_path, [])
    manifest = migrator.load_manifest(manifest_path)
    (tmp_path / "src/applypilot/fleet/schema.py").write_bytes(b"tampered\n")
    with pytest.raises(migrator.ManifestError, match="predecessor blob"):
        migrator.verify_predecessor(manifest, tmp_path)

    first = _migration(tmp_path, "20260715_001_probe", None, None)
    manifest = migrator.load_manifest(_write_manifest(tmp_path, [first]))
    (tmp_path / str(first["path"])).write_text("SELECT 2;\n", encoding="utf-8", newline="\n")
    with pytest.raises(migrator.ManifestError, match="checksum"):
        migrator.verify_migration_files(manifest, tmp_path)


def test_apply_requires_exact_role_and_creates_immutable_ledger(migration_pg, tmp_path):
    manifest = migrator.load_manifest(_write_manifest(tmp_path, []))
    with psycopg.connect(migration_pg, row_factory=dict_row) as conn:
        with pytest.raises(migrator.MigrationError, match="migration role"):
            migrator.apply_manifest(conn, manifest, tmp_path)
        conn.rollback()
        conn.execute("SET ROLE applypilot_fleet_migrator")
        result = migrator.apply_manifest(conn, manifest, tmp_path)
        assert result.applied == ()
        assert result.already_applied == ()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("TRUNCATE applypilot_fleet_schema_migrations")


def test_apply_is_atomic_resumable_and_rejects_ledger_drift(migration_pg, tmp_path):
    first = _migration(tmp_path, "20260715_001_probe", None, None)
    second = _migration(tmp_path, "20260715_002_probe", first["id"], first["sha256"])
    manifest = migrator.load_manifest(_write_manifest(tmp_path, [first, second]))
    with psycopg.connect(migration_pg, row_factory=dict_row) as conn:
        conn.execute("SET ROLE applypilot_fleet_migrator")
        result = migrator.apply_manifest(conn, manifest, tmp_path)
        assert result.applied == (first["id"], second["id"])
        result = migrator.apply_manifest(conn, manifest, tmp_path)
        assert result.applied == ()
        assert result.already_applied == (first["id"], second["id"])
        conn.execute("RESET ROLE")
        conn.execute("ALTER TABLE applypilot_fleet_schema_migrations DISABLE TRIGGER USER")
        conn.execute("UPDATE applypilot_fleet_schema_migrations SET migration_sha256=%s WHERE migration_id=%s", ("0" * 64, first["id"]))
        conn.execute("ALTER TABLE applypilot_fleet_schema_migrations ENABLE TRIGGER USER")
        conn.commit()
        conn.execute("SET ROLE applypilot_fleet_migrator")
        with pytest.raises(migrator.MigrationError, match="ledger mismatch"):
            migrator.apply_manifest(conn, manifest, tmp_path)


def test_existing_ledger_contract_is_verified_not_repaired(migration_pg, tmp_path):
    manifest = migrator.load_manifest(_write_manifest(tmp_path, []))
    with psycopg.connect(migration_pg, row_factory=dict_row) as conn:
        conn.execute("SET ROLE applypilot_fleet_migrator")
        migrator.apply_manifest(conn, manifest, tmp_path)
        conn.execute("RESET ROLE")
        conn.execute("ALTER TABLE applypilot_fleet_schema_migrations ADD COLUMN injected text")
        conn.commit()
        conn.execute("SET ROLE applypilot_fleet_migrator")
        with pytest.raises(migrator.MigrationError, match="ledger contract"):
            migrator.apply_manifest(conn, manifest, tmp_path)


def test_failed_migration_rolls_back_sql_and_ledger(migration_pg, tmp_path):
    first = _migration(tmp_path, "20260715_001_probe", None, None)
    path = tmp_path / str(first["path"])
    path.write_text("CREATE TABLE must_rollback (id integer);\nSELECT 1 / 0;\n", encoding="utf-8", newline="\n")
    first["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = migrator.load_manifest(_write_manifest(tmp_path, [first]))
    with psycopg.connect(migration_pg, row_factory=dict_row) as conn:
        conn.execute("SET ROLE applypilot_fleet_migrator")
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrator.apply_manifest(conn, manifest, tmp_path)
        conn.rollback()
        assert conn.execute("SELECT to_regclass('public.must_rollback') AS oid").fetchone()["oid"] is None
        assert conn.execute("SELECT count(*) AS n FROM applypilot_fleet_schema_migrations").fetchone()["n"] == 0


def test_application_authority_is_single_owner_and_crash_conservative(migration_pg):
    manifest = migrator.load_manifest(MANIFEST_PATH)
    with psycopg.connect(migration_pg, row_factory=dict_row) as conn:
        conn.execute("SET ROLE applypilot_fleet_migrator")
        migrator.apply_manifest(conn, manifest, REPO_ROOT)
        op = "00000000-0000-0000-0000-000000000001"
        first = conn.execute(
            "SELECT * FROM fleet_worker_authorize_lease(%s,%s,%s,%s,%s::uuid,%s,%s)",
            ("app-1", "https://jobs.example/a", "worker-a", "ats", op, "hash-a", 60),
        ).fetchone()
        assert first["authority_epoch"] == 1
        assert conn.execute(
            "SELECT fleet_worker_mark_browser_interaction(%s,%s,%s)",
            ("app-1", "worker-a", 1),
        ).fetchone()["fleet_worker_mark_browser_interaction"]
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="not claimable"):
            conn.execute(
                "SELECT * FROM fleet_worker_authorize_lease(%s,%s,%s,%s,%s::uuid,%s,%s)",
                ("app-1", "https://jobs.example/a?alias=1", "worker-b", "linkedin", op, "hash-a", 60),
            )
        conn.rollback()
        assert not conn.execute(
            "SELECT fleet_worker_requeue(%s,%s,%s)", ("app-1", "worker-a", 1)
        ).fetchone()["fleet_worker_requeue"]
        assert conn.execute(
            "SELECT fleet_worker_terminalize(%s,%s,%s,%s,%s::jsonb)",
            ("app-1", "worker-a", 1, "applied", '{"receipt":"r1"}'),
        ).fetchone()["fleet_worker_terminalize"]
        row = conn.execute(
            "SELECT state, terminal_status, terminal_evidence->>'receipt' AS receipt "
            "FROM fleet_application_authority WHERE canonical_application_id='app-1'"
        ).fetchone()
        assert (row["state"], row["terminal_status"], row["receipt"]) == ("terminal", "applied", "r1")
