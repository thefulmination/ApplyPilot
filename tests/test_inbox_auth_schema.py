from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from applypilot import database


def test_init_db_creates_inbox_auth_tables(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"inbox_events", "auth_challenges"} <= tables


def test_inbox_events_message_id_is_unique(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    conn.execute(
        """
        INSERT INTO inbox_events (message_id, event_type, confidence, created_at)
        VALUES ('msg-1', 'auth_challenge', 'high', '2026-06-24T00:00:00+00:00')
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO inbox_events (message_id, event_type, confidence, created_at)
            VALUES ('msg-1', 'auth_challenge', 'high', '2026-06-24T00:01:00+00:00')
            """
        )


def test_auth_challenges_indexes_include_status_and_job_url(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(auth_challenges)").fetchall()}
    assert {"idx_auth_challenges_status", "idx_auth_challenges_job_url"} <= indexes
