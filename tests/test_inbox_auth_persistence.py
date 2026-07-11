from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
import sqlite3

import pytest

from applypilot import database
from applypilot import inbox_auth


def _process_claim(db_path: str, challenge_id: int, message_id: str, queue) -> None:
    conn = database.get_connection(db_path)
    try:
        queue.put(inbox_auth.claim_auth_match(
            challenge_id, message_id=message_id, event_type="auth_code", connection=conn
        ))
    finally:
        conn.close()


def _process_die_with_write_lock(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("BEGIN IMMEDIATE")
    os._exit(17)


def _match(message_id: str, received_at: datetime, *, provider="greenhouse.io", value="123456"):
    from email.utils import format_datetime

    candidate = inbox_auth.VerificationCandidate(
        kind="code", value=value, confidence="high", reasons=("test",)
    )
    return inbox_auth.AuthEmailMatch(
        message_id=message_id,
        thread_id=message_id,
        sender=f"no-reply@{provider}",
        subject="Verify",
        received_at=format_datetime(received_at),
        snippet="verification",
        candidate=candidate,
        reasons=candidate.reasons,
    )


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


def test_record_inbox_event_minimizes_sensitive_auth_metadata(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    code = "839214"
    magic_link = "https://boards.greenhouse.io/verify?token=raw-secret"

    event_id = inbox_auth.record_inbox_event(
        message_id="privacy-record",
        thread_id="thread-safe",
        sender=f"Verification {code} <no-reply@greenhouse.io>",
        subject=f"Your verification code is {code}",
        event_type="auth_code",
        confidence="high",
        snippet=f"Enter {code} or open {magic_link}",
    )

    row = conn.execute(
        "SELECT sender, sender_domain, subject, snippet FROM inbox_events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row["sender"] is None
    assert row["sender_domain"] == "greenhouse.io"
    assert row["subject"] is None
    assert row["snippet"] is None
    serialized = "|".join("" if value is None else str(value) for value in row)
    assert code not in serialized
    assert magic_link not in serialized
    assert "Your verification code" not in serialized


@pytest.mark.parametrize(
    "malformed_sender",
    [
        "Verification code 839214",
        "https://boards.greenhouse.io/verify?token=sender-secret",
        "839214 | no-reply @ greenhouse.io",
        "code=839214; magic=https://example.com/verify?token=x",
        "not-an-address",
    ],
)
def test_record_inbox_event_rejects_unsafe_persistence_sender_domain(
    tmp_path, monkeypatch, malformed_sender
) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)

    event_id = inbox_auth.record_inbox_event(
        message_id=f"malformed-{abs(hash(malformed_sender))}",
        sender=malformed_sender,
        event_type="auth_code",
        confidence="high",
    )

    row = conn.execute(
        "SELECT sender, sender_domain, subject, snippet FROM inbox_events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None, None)
    serialized = "|".join("" if value is None else str(value) for value in row)
    assert "839214" not in serialized
    assert "http" not in serialized


def test_record_inbox_event_preserves_valid_bare_sender_domain(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)

    event_id = inbox_auth.record_inbox_event(
        message_id="valid-bare-domain",
        sender="greenhouse.io",
        event_type="auth_code",
        confidence="high",
    )

    assert conn.execute(
        "SELECT sender_domain FROM inbox_events WHERE id=?", (event_id,)
    ).fetchone()[0] == "greenhouse.io"


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
    assert inbox_auth.claimed_auth_message_ids({"shared", "unused"}) == {"shared"}


def test_claim_auth_match_minimizes_sensitive_auth_metadata(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    challenge_id = _seed_job_and_challenge(conn, job_url="privacy-claim")
    code = "445566"
    magic_link = "https://boards.greenhouse.io/verify?token=claim-secret"

    assert inbox_auth.claim_auth_match(
        challenge_id,
        message_id="privacy-claim-message",
        thread_id="thread-safe",
        sender=f"Code {code} <no-reply@greenhouse.io>",
        subject=f"Use code {code}",
        event_type="auth_code",
        confidence="high",
        snippet=f"Use {code} or {magic_link}",
    )

    row = conn.execute(
        """
        SELECT event.sender, event.sender_domain, event.subject, event.snippet
          FROM auth_challenges AS challenge
          JOIN inbox_events AS event ON event.id=challenge.inbox_event_id
         WHERE challenge.id=?
        """,
        (challenge_id,),
    ).fetchone()
    assert row["sender"] is None
    assert row["sender_domain"] == "greenhouse.io"
    assert row["subject"] is None
    assert row["snippet"] is None
    serialized = "|".join("" if value is None else str(value) for value in row)
    assert code not in serialized
    assert magic_link not in serialized
    assert "Use code" not in serialized


def test_claim_auth_match_rejects_unsafe_persistence_sender_domain(
    tmp_path, monkeypatch
) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    challenge_id = _seed_job_and_challenge(conn, job_url="unsafe-sender-claim")

    assert inbox_auth.claim_auth_match(
        challenge_id,
        message_id="unsafe-sender-message",
        sender="code 445566 | https://example.com/verify?token=claim-secret",
        event_type="auth_code",
    )

    row = conn.execute(
        """
        SELECT event.sender, event.sender_domain, event.subject, event.snippet
          FROM auth_challenges AS challenge
          JOIN inbox_events AS event ON event.id=challenge.inbox_event_id
         WHERE challenge.id=?
        """,
        (challenge_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None, None)


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


def test_claim_auth_match_cannot_resolve_expired_challenge(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    challenge_id = _seed_job_and_challenge(conn, job_url="expired")
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "UPDATE auth_challenges SET requested_at=?, expires_at=? WHERE id=?",
        ((now - timedelta(minutes=10)).isoformat(), (now - timedelta(seconds=1)).isoformat(), challenge_id),
    )
    conn.commit()

    assert not inbox_auth.claim_auth_match(
        challenge_id, message_id="too-late", event_type="auth_code", now=now
    )
    row = conn.execute(
        "SELECT status, inbox_event_id FROM auth_challenges WHERE id=?", (challenge_id,)
    ).fetchone()
    assert tuple(row) == ("expired", None)


def _set_challenge_window(conn, challenge_id, requested_at, expires_at):
    conn.execute(
        "UPDATE auth_challenges SET requested_at=?, expires_at=? WHERE id=?",
        (requested_at.isoformat(), expires_at.isoformat(), challenge_id),
    )
    conn.commit()


def test_local_assignment_two_requests_one_message_fails_closed(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    first = _seed_job_and_challenge(conn, job_url="ambiguous-1")
    second = _seed_job_and_challenge(conn, job_url="ambiguous-2")
    now = datetime.now(timezone.utc)
    matches = [_match("only", now + timedelta(seconds=1))]

    assert inbox_auth.claim_unique_auth_match(first, matches, now=now) is None
    assert inbox_auth.claim_unique_auth_match(second, matches, now=now) is None


def test_local_assignment_two_equally_eligible_messages_fails_closed(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    first = _seed_job_and_challenge(conn, job_url="equal-1")
    second = _seed_job_and_challenge(conn, job_url="equal-2")
    now = datetime.now(timezone.utc)
    matches = [
        _match("equal-a", now + timedelta(seconds=1), value="111111"),
        _match("equal-b", now + timedelta(seconds=2), value="222222"),
    ]

    assert inbox_auth.claim_unique_auth_match(first, matches, now=now) is None
    assert inbox_auth.claim_unique_auth_match(second, matches, now=now) is None


def test_local_assignment_finds_globally_unique_temporal_matching(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    base = datetime.now(timezone.utc) - timedelta(minutes=4)
    first = _seed_job_and_challenge(conn, job_url="temporal-1")
    second = _seed_job_and_challenge(conn, job_url="temporal-2")
    _set_challenge_window(conn, first, base, base + timedelta(minutes=10))
    _set_challenge_window(conn, second, base + timedelta(minutes=2), base + timedelta(minutes=10))
    matches = [
        _match("temporal-a", base + timedelta(minutes=1), value="111111"),
        _match("temporal-b", base + timedelta(minutes=3), value="222222"),
    ]
    now = base + timedelta(minutes=4)

    assert inbox_auth.claim_unique_auth_match(first, matches, now=now).message_id == "temporal-a"
    assert inbox_auth.claim_unique_auth_match(second, matches, now=now).message_id == "temporal-b"


def test_local_assignment_separates_different_providers(tmp_path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    greenhouse = _seed_job_and_challenge(conn, job_url="provider-greenhouse")
    workday = _seed_job_and_challenge(conn, job_url="provider-workday")
    now = datetime.now(timezone.utc)
    conn.execute("UPDATE auth_challenges SET provider='workday.com' WHERE id=?", (workday,))
    conn.commit()
    matches = [
        _match("gh", now + timedelta(seconds=1), value="111111"),
        _match("wd", now + timedelta(seconds=1), provider="workday.com", value="222222"),
    ]

    assert inbox_auth.claim_unique_auth_match(greenhouse, matches, now=now).message_id == "gh"
    assert inbox_auth.claim_unique_auth_match(workday, matches, now=now).message_id == "wd"


def test_claim_auth_match_is_atomic_across_spawned_processes(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    challenges = [
        _seed_job_and_challenge(conn, job_url="process-1"),
        _seed_job_and_challenge(conn, job_url="process-2"),
    ]
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_process_claim, args=(str(db_path), challenge, "process-message", queue))
        for challenge in challenges
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(queue.get(timeout=2) for _ in processes) == [False, True]
    assert conn.execute(
        "SELECT COUNT(*) FROM auth_challenges WHERE inbox_event_id IS NOT NULL"
    ).fetchone()[0] == 1
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()


def test_sqlite_lock_is_released_when_claim_process_terminates(tmp_path) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_process_die_with_write_lock, args=(str(db_path),))
    process.start()
    process.join(20)

    assert process.exitcode == 17
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()


def test_claimed_message_lookup_is_bounded_to_candidate_ids():
    observed = {}

    class _Conn:
        def execute(self, query, params):
            observed["query"] = query
            observed["params"] = list(params)
            return self

        def fetchall(self):
            return []

    assert inbox_auth.claimed_auth_message_ids(
        {"candidate-a", "candidate-b"}, connection=_Conn()
    ) == set()
    assert " IN (" in observed["query"]
    assert observed["params"] == ["candidate-a", "candidate-b"]
