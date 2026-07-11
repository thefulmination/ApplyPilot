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


def test_auth_challenge_message_claim_is_unique(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    indexes = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA index_list(auth_challenges)").fetchall()
    }

    assert indexes["idx_auth_challenges_inbox_event_unique"] == 1


def test_auth_claim_index_migration_repairs_legacy_duplicate_links(tmp_path: Path) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    conn.execute("DROP INDEX idx_auth_challenges_inbox_event_unique")
    now = "2026-07-10T12:00:00+00:00"
    conn.executemany(
        "INSERT INTO jobs (url, title, site, discovered_at) VALUES (?, 'T', 'S', ?)",
        [("legacy-1", now), ("legacy-2", now)],
    )
    event_id = conn.execute(
        """
        INSERT INTO inbox_events (message_id, event_type, confidence, created_at)
        VALUES ('legacy-message', 'auth_code', 'high', ?)
        """,
        (now,),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO auth_challenges (
            job_url, challenge_type, status, requested_at, expires_at,
            resolved_at, inbox_event_id, created_at, updated_at
        ) VALUES (?, 'email_code', 'resolved', ?, ?, ?, ?, ?, ?)
        """,
        [
            ("legacy-1", now, now, now, event_id, now, now),
            ("legacy-2", now, now, now, event_id, now, now),
        ],
    )
    conn.commit()

    database.ensure_inbox_auth_tables(conn)
    database.ensure_inbox_auth_tables(conn)

    links = conn.execute(
        "SELECT inbox_event_id FROM auth_challenges ORDER BY id"
    ).fetchall()
    assert [row[0] for row in links] == [event_id, None]
    repaired = conn.execute(
        "SELECT status, resolved_at, last_error FROM auth_challenges ORDER BY id"
    ).fetchall()[1]
    assert tuple(repaired) == ("failed", None, "message_claim_conflict")
    assert {
        row[1] for row in conn.execute("PRAGMA index_list(auth_challenges)")
    } >= {"idx_auth_challenges_inbox_event_unique"}
