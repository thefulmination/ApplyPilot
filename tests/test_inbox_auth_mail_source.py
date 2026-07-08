"""Proves the OTP/2FA path is parser-identical when routed through
get_mail_source() (MailMessage) instead of the legacy Gmail-API `service`.
extract_verification_candidates (the parser) is UNCHANGED; only the fetch swaps."""
from __future__ import annotations

import datetime as dt

from applypilot import inbox_auth
from applypilot.fleet import otp_relay
from applypilot.mail_source import MailMessage


def _rfc(when: dt.datetime) -> str:
    from email.utils import format_datetime

    return format_datetime(when)


def test_scan_gmail_for_auth_codes_messages_path_extracts_code():
    now = dt.datetime.now(dt.timezone.utc)
    msg = MailMessage(
        id="m1",
        thread_id="t1",
        subject="Your Greenhouse verification code",
        sender="no-reply@greenhouse.io",
        date=_rfc(now),
        body="Use verification code 123456 to continue your application.",
    )

    matches = inbox_auth.scan_gmail_for_auth_codes(messages=[msg])

    assert len(matches) == 1
    match = matches[0]
    assert match.message_id == "m1"
    assert match.thread_id == "t1"
    assert match.candidate.kind == "code"
    assert match.candidate.value == "123456"


def test_scan_gmail_for_auth_codes_messages_path_matches_service_path():
    """Same subject/sender/body through both paths -> identical candidate value
    and confidence, proving the parser itself is untouched."""
    subject = "Verify your email"
    sender = "no-reply@greenhouse.io"
    body = "Use verification code 839214 to continue your application."
    now = dt.datetime.now(dt.timezone.utc)

    msg = MailMessage(
        id="m2", thread_id="t2", subject=subject, sender=sender,
        date=_rfc(now), body=body,
    )
    via_messages = inbox_auth.scan_gmail_for_auth_codes(messages=[msg])

    class _FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class _FakeMessages:
        def list(self, userId, q, maxResults):
            return _FakeRequest({"messages": [{"id": "m2", "threadId": "t2"}]})

        def get(self, userId, id, format):  # noqa: A002
            import base64

            encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii").rstrip("=")
            payload = {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                    {"name": "Date", "value": _rfc(now)},
                ],
                "mimeType": "text/plain",
                "body": {"data": encoded},
            }
            return _FakeRequest({"payload": payload})

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    via_service = inbox_auth.scan_gmail_for_auth_codes(service=_FakeService())

    assert len(via_messages) == 1
    assert len(via_service) == 1
    assert via_messages[0].candidate.value == via_service[0].candidate.value
    assert via_messages[0].candidate.confidence == via_service[0].candidate.confidence


def test_scan_gmail_for_auth_codes_messages_path_keeps_distinct_same_thread_codes():
    now = dt.datetime.now(dt.timezone.utc)
    common = dict(
        subject="Verify your email",
        sender="no-reply@greenhouse.io",
        thread_id="dup",
    )
    msgs = [
        MailMessage(
            id="a",
            date=_rfc(now - dt.timedelta(minutes=2)),
            body="Use verification code 111222 to continue.",
            **common,
        ),
        MailMessage(
            id="b",
            date=_rfc(now - dt.timedelta(minutes=1)),
            body="Use verification code 333444 to continue.",
            **common,
        ),
    ]

    matches = inbox_auth.scan_gmail_for_auth_codes(messages=msgs)

    assert [m.message_id for m in matches] == ["a", "b"]
    assert [m.candidate.value for m in matches] == ["111222", "333444"]


def test_scan_gmail_for_auth_codes_messages_path_drops_codes_outside_minutes_window():
    now = dt.datetime.now(dt.timezone.utc)
    stale = MailMessage(
        id="old",
        thread_id="old",
        subject="Your Greenhouse verification code",
        sender="no-reply@greenhouse.io",
        date=_rfc(now - dt.timedelta(hours=23)),
        body="Use verification code 123456 to continue your application.",
    )

    matches = inbox_auth.scan_gmail_for_auth_codes(
        messages=[stale],
        minutes=10,
        max_messages=25,
    )

    assert matches == []


def test_watch_gmail_for_auth_code_picks_newest_recent_match(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)

    class _FakeSource:
        def fetch(self, *, since_days, max_messages):
            return [
                MailMessage(
                    id="older", thread_id="t-old", subject="Verify your email",
                    sender="no-reply@greenhouse.io", date=_rfc(now - dt.timedelta(minutes=8)),
                    body="Use verification code 111111 to continue.",
                ),
                MailMessage(
                    id="newer", thread_id="t-new", subject="Verify your email",
                    sender="no-reply@greenhouse.io", date=_rfc(now - dt.timedelta(minutes=1)),
                    body="Use verification code 222222 to continue.",
                ),
            ]

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _FakeSource()
    )

    match = inbox_auth.watch_gmail_for_auth_code(
        timeout_seconds=1, poll_seconds=0, max_errors=1, minutes=10, max_messages=25,
    )

    assert match is not None
    assert match.message_id == "newer"
    assert match.candidate.value == "222222"


def test_watch_gmail_for_auth_code_defaults_to_mail_source(monkeypatch):
    """service=None -> resolves via get_mail_source().fetch(...) then scans."""
    seen = {}

    class _FakeSource:
        def fetch(self, *, since_days, max_messages):
            seen["since_days"] = since_days
            seen["max_messages"] = max_messages
            return [
                MailMessage(
                    id="m3", thread_id="t3", subject="Verify your email",
                    sender="no-reply@greenhouse.io", date=_rfc(dt.datetime.now(dt.timezone.utc)),
                    body="Use verification code 654321 to continue.",
                )
            ]

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _FakeSource()
    )

    match = inbox_auth.watch_gmail_for_auth_code(
        timeout_seconds=1, poll_seconds=0, max_errors=1, minutes=10, max_messages=25,
    )

    assert match is not None
    assert match.candidate.value == "654321"
    assert seen["max_messages"] == 25
    assert seen["since_days"] == 1


def test_answer_pending_defaults_to_mail_source(monkeypatch):
    """gmail_service=None -> answer_pending resolves via get_mail_source() and
    passes the fetched MailMessage list into scan_gmail_for_auth_codes(messages=...)."""
    fetch_calls = {}
    scan_calls = {}

    class _FakeSource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            fetch_calls["since_days"] = since_days
            fetch_calls["max_messages"] = max_messages
            fetch_calls["gmail_raw_query"] = gmail_raw_query
            return ["sentinel-messages"]

    def _fake_scan(*, messages=None, service=None, minutes=10, max_messages=25):
        scan_calls["messages"] = messages
        scan_calls["minutes"] = minutes
        scan_calls["max_messages"] = max_messages
        return []

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _FakeSource()
    )
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", _fake_scan)

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **kw):
            pass

        def fetchall(self):
            return self._rows

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return _FakeCursor(self._rows)

        def commit(self):
            pass

    pending_row = {"id": 1, "requested_at": dt.datetime.now(dt.timezone.utc)}
    conn = _FakeConn([pending_row])

    n = otp_relay.answer_pending(conn, window_minutes=15, max_messages=25)

    assert n == 0
    assert scan_calls["messages"] == ["sentinel-messages"]
    assert fetch_calls["max_messages"] == 25
    assert "verification" in fetch_calls["gmail_raw_query"]


def test_answer_pending_writes_code_via_mail_source(fleet_db, monkeypatch):
    """End-to-end against the real fleet_db harness: a seeded otp_request row is
    answered when get_mail_source() is monkeypatched to a fake IMAP-backed source."""
    from applypilot.apply import pgqueue
    from applypilot.fleet import schema as fleet_schema

    now = dt.datetime.now(dt.timezone.utc)

    class _FakeSource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            return [
                MailMessage(
                    id="m9", thread_id="t9", subject="Verify your email",
                    sender="no-reply@greenhouse.io",
                    date=_rfc(now + dt.timedelta(seconds=30)),
                    body="Use verification code 998877 to continue.",
                )
            ]

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _FakeSource()
    )

    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        n = otp_relay.answer_pending(conn)
        assert n == 1
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)

    assert got is not None
    assert got.value == "998877"


def test_answer_pending_fetches_enough_mail_for_busy_inbox(fleet_db, monkeypatch):
    """A busy inbox can push a real auth mail outside the newest 100 messages.

    The relay must fetch a larger slice so the older-in-window OTP email is still
    scanned and matched.
    """
    from applypilot.apply import pgqueue
    from applypilot.fleet import schema as fleet_schema

    now = dt.datetime.now(dt.timezone.utc)
    auth_msg = MailMessage(
        id="auth-1",
        thread_id="auth-1",
        subject="Your Greenhouse verification code",
        sender="no-reply@greenhouse.io",
        date=_rfc(now + dt.timedelta(seconds=1)),
        body="Use verification code 445566 to continue your application.",
    )
    filler = [
        MailMessage(
            id=f"fill-{i}",
            thread_id=f"fill-{i}",
            subject="Status update",
            sender="noreply@example.com",
            date=_rfc(now + dt.timedelta(seconds=10 + i)),
            body="Nothing to verify here.",
        )
        for i in range(119)
    ]
    mailbox = [auth_msg, *filler]

    class _BusySource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            return mailbox[-max_messages:]

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _BusySource()
    )

    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        n = otp_relay.answer_pending(conn)
        assert n == 1
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)

    assert got is not None
    assert got.value == "445566"
