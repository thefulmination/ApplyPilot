from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from applypilot import database
from applypilot import inbox_auth


def _seed_job_and_challenge(conn, *, job_url: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, site, discovered_at) VALUES (?, 'T', 'S', ?)",
        (job_url, now),
    )
    conn.commit()
    return inbox_auth.create_auth_challenge(
        job_url=job_url,
        application_url=job_url,
        provider="greenhouse.io",
        challenge_type="email_code",
    )


def test_record_inbox_event_is_idempotent(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)

    first = inbox_auth.record_inbox_event(
        message_id="msg-1",
        thread_id="thread-1",
        sender="no-reply@greenhouse.io",
        subject="Verification code",
        event_type="auth_code",
        confidence="high",
        snippet="Use code 839214",
    )
    second = inbox_auth.record_inbox_event(
        message_id="msg-1",
        thread_id="thread-1",
        sender="no-reply@greenhouse.io",
        subject="Verification code",
        event_type="auth_code",
        confidence="high",
        snippet="Use code 839214",
    )

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0] == 1


def test_expire_stale_challenges(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, discovered_at)
        VALUES ('https://jobs.example/1', 'Role', 'Example', ?)
        """,
        (now.isoformat(),),
    )
    challenge_id = inbox_auth.create_auth_challenge(
        job_url="https://jobs.example/1",
        application_url="https://jobs.example/1/apply",
        provider="greenhouse",
        challenge_type="email_code",
        ttl_seconds=1,
    )
    conn.execute(
        "UPDATE auth_challenges SET status='watching', expires_at=? WHERE id=?",
        ((now - timedelta(seconds=5)).isoformat(), challenge_id),
    )
    conn.commit()

    expired = inbox_auth.expire_stale_challenges()

    assert expired == 1
    status = conn.execute(
        "SELECT status FROM auth_challenges WHERE id=?",
        (challenge_id,)
    ).fetchone()[0]
    assert status == "expired"


def test_resolve_challenge_requires_pending_or_watching(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO jobs (url, title, site, discovered_at) VALUES ('u', 'T', 'S', ?)", (now,))
    challenge_id = inbox_auth.create_auth_challenge(
        job_url="u",
        application_url="u",
        provider="greenhouse",
        challenge_type="email_code",
    )
    event_id = inbox_auth.record_inbox_event(
        message_id="msg-2",
        sender="no-reply@greenhouse.io",
        subject="Code",
        event_type="auth_code",
        confidence="high",
    )

    assert inbox_auth.resolve_auth_challenge(challenge_id, event_id) is True
    assert inbox_auth.resolve_auth_challenge(challenge_id, event_id) is False


def test_claim_auth_match_prevents_sequential_message_reuse(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    first = _seed_job_and_challenge(conn, job_url="first")
    second = _seed_job_and_challenge(conn, job_url="second")

    assert inbox_auth.claim_auth_match(first, message_id="shared", event_type="auth_code")
    assert inbox_auth.claim_auth_match(first, message_id="shared", event_type="auth_code")
    assert not inbox_auth.claim_auth_match(second, message_id="shared", event_type="auth_code")
    assert inbox_auth.claimed_auth_message_ids() == {"shared"}


def test_claim_auth_match_is_atomic_across_concurrent_connections(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "applypilot.db"
    setup = database.init_db(db_path)
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: setup)
    challenge_ids = [
        _seed_job_and_challenge(setup, job_url="race-1"),
        _seed_job_and_challenge(setup, job_url="race-2"),
    ]

    def claim(challenge_id: int) -> bool:
        conn = database.get_connection(db_path)
        return inbox_auth.claim_auth_match(
            challenge_id,
            message_id="race-message",
            event_type="auth_code",
            connection=conn,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, challenge_ids))

    assert sorted(results) == [False, True]
    rows = setup.execute(
        "SELECT id, inbox_event_id FROM auth_challenges WHERE id IN (?, ?)",
        challenge_ids,
    ).fetchall()
    assert sum(row["inbox_event_id"] is not None for row in rows) == 1


def test_claim_auth_match_rolls_back_when_resolution_persistence_fails(
    tmp_path, monkeypatch
) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    challenge_id = _seed_job_and_challenge(conn, job_url="failure")
    conn.execute(
        """
        CREATE TRIGGER fail_auth_resolution
        BEFORE UPDATE OF inbox_event_id ON auth_challenges
        BEGIN SELECT RAISE(ABORT, 'resolution failed'); END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="resolution failed"):
        inbox_auth.claim_auth_match(
            challenge_id, message_id="must-not-survive", event_type="auth_code"
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM inbox_events WHERE message_id='must-not-survive'"
    ).fetchone()[0] == 0
