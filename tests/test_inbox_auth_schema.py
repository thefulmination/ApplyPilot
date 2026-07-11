from __future__ import annotations

import sqlite3
import hashlib
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


def test_inbox_event_scrub_migration_hashes_merges_and_preserves_fk_links(
    tmp_path: Path,
) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    raw_message_id = "legacy-884422-https://evil.example/verify?token=legacy-secret"
    digest = "sha256:" + hashlib.sha256(raw_message_id.encode("utf-8")).hexdigest()
    now = "2026-07-10T12:00:00+00:00"
    conn.executemany(
        "INSERT INTO jobs (url, title, site, discovered_at) VALUES (?, 'T', 'S', ?)",
        [("legacy-scrub-1", now), ("legacy-scrub-2", now)],
    )
    raw_event = conn.execute(
        """
        INSERT INTO inbox_events (
            message_id, thread_id, sender, sender_domain, subject, received_at,
            event_type, confidence, matched_job_url, matched_company,
            matched_method, snippet, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            raw_message_id,
            "thread-884422",
            "Code 884422 <alerts@884422.greenhouse.io>",
            "884422.greenhouse.io",
            "Subject 884422",
            "Fri, 10 Jul 2026 08:30:00 -0400",
            "event-884422",
            "confidence-884422",
            "company-884422",
            "method-884422",
            "https://evil.example/verify?token=legacy-secret",
            now,
        ),
    ).lastrowid
    digest_event = conn.execute(
        """
        INSERT INTO inbox_events (message_id, event_type, confidence, created_at)
        VALUES (?, 'auth_event', 'high', ?)
        """,
        (digest, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO auth_challenges (
            job_url, challenge_type, status, requested_at, expires_at,
            resolved_at, inbox_event_id, created_at, updated_at
        ) VALUES (?, 'email_code', 'resolved', ?, ?, ?, ?, ?, ?)
        """,
        [
            ("legacy-scrub-1", now, now, now, raw_event, now, now),
            ("legacy-scrub-2", now, now, now, digest_event, now, now),
        ],
    )
    conn.commit()

    database.ensure_inbox_auth_tables(conn)
    database.ensure_inbox_auth_tables(conn)

    events = conn.execute("SELECT * FROM inbox_events").fetchall()
    assert len(events) == 1
    event = events[0]
    assert event["id"] == digest_event
    assert event["message_id"] == digest
    assert event["thread_id"] is None
    assert event["sender"] is None
    assert event["sender_domain"] == "greenhouse.io"
    assert event["subject"] is None
    assert event["received_at"] == "2026-07-10T12:30:00+00:00"
    assert event["event_type"] == "auth_event"
    assert event["confidence"] in {"low", "medium", "high"}
    assert event["matched_job_url"] is None
    assert event["matched_company"] is None
    assert event["matched_method"] is None
    assert event["snippet"] is None
    serialized = "|".join("" if value is None else str(value) for value in event)
    assert "884422" not in serialized
    assert "evil.example" not in serialized
    challenges = conn.execute(
        "SELECT status, resolved_at, inbox_event_id, last_error FROM auth_challenges ORDER BY id"
    ).fetchall()
    linked = [row for row in challenges if row["inbox_event_id"] is not None]
    conflicted = [row for row in challenges if row["inbox_event_id"] is None]
    assert len(linked) == 1 and linked[0]["inbox_event_id"] == digest_event
    assert len(conflicted) == 1
    assert tuple(conflicted[0]) == ("failed", None, None, "message_claim_conflict")
