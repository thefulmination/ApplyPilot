from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot import database
from applypilot import inbox_auth


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
