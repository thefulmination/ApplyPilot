"""Off-machine DB backup (mirror_db_offsite).

The keep-alive run keeps the authoritative DB in %LOCALAPPDATA% and writes its rolling
backup next to it -- both on the SAME local disk. mirror_db_offsite copies the DB into
the OneDrive-synced APP_DIR so a local-disk loss is still recoverable. It must: write a
valid copy when the paths differ, no-op when the DB already lives in the synced dir,
respect the backups-off switch, and NEVER overwrite a good offsite copy with a corrupt
source.
"""
from __future__ import annotations

import sqlite3

from applypilot import config, database
from applypilot.apply import launcher as L


def test_mirror_writes_offsite_copy(tmp_path, monkeypatch):
    local = tmp_path / "local" / "applypilot.db"
    local.parent.mkdir(parents=True)
    onedrive = tmp_path / "onedrive"
    database.init_db(local)
    monkeypatch.setattr(config, "DB_PATH", local)
    monkeypatch.setattr(config, "APP_DIR", onedrive)
    monkeypatch.delenv("APPLYPILOT_BACKUP_INTERVAL", raising=False)

    assert L.mirror_db_offsite() is True
    dest = onedrive / "applypilot.db"
    assert dest.exists()
    c = sqlite3.connect(str(dest))
    try:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # valid DB, schema present
    finally:
        c.close()


def test_mirror_noop_when_db_already_synced(tmp_path, monkeypatch):
    # No APPLYPILOT_DB_PATH override -> the DB already lives in APP_DIR -> nothing to mirror.
    appdir = tmp_path / "synced"
    appdir.mkdir()
    db = appdir / "applypilot.db"
    database.init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "APP_DIR", appdir)
    monkeypatch.delenv("APPLYPILOT_BACKUP_INTERVAL", raising=False)

    assert L.mirror_db_offsite() is False


def test_mirror_disabled_when_interval_zero(tmp_path, monkeypatch):
    local = tmp_path / "local" / "applypilot.db"
    local.parent.mkdir(parents=True)
    onedrive = tmp_path / "onedrive"
    database.init_db(local)
    monkeypatch.setattr(config, "DB_PATH", local)
    monkeypatch.setattr(config, "APP_DIR", onedrive)
    monkeypatch.setenv("APPLYPILOT_BACKUP_INTERVAL", "0")

    assert L.mirror_db_offsite() is False
    assert not (onedrive / "applypilot.db").exists()


def test_mirror_does_not_clobber_good_copy_with_corrupt_source(tmp_path, monkeypatch):
    onedrive = tmp_path / "onedrive"
    onedrive.mkdir()
    good = onedrive / "applypilot.db"
    good.write_bytes(b"GOOD-OFFSITE-COPY")  # sentinel: a pre-existing valid offsite copy
    corrupt = tmp_path / "local" / "applypilot.db"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a sqlite database at all")
    monkeypatch.setattr(config, "DB_PATH", corrupt)
    monkeypatch.setattr(config, "APP_DIR", onedrive)
    monkeypatch.delenv("APPLYPILOT_BACKUP_INTERVAL", raising=False)

    assert L.mirror_db_offsite() is False
    assert good.read_bytes() == b"GOOD-OFFSITE-COPY"  # untouched
