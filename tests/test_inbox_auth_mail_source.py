"""Proves the OTP/2FA path is parser-identical when routed through
get_mail_source() (MailMessage) instead of the legacy Gmail-API `service`.
extract_verification_candidates (the parser) is UNCHANGED; only the fetch swaps."""
from __future__ import annotations

import datetime as dt

import pytest

from applypilot import inbox_auth
from applypilot import mail_source
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


def test_message_list_candidate_overflow_fails_closed():
    now = dt.datetime.now(dt.timezone.utc)
    messages = [
        MailMessage(
            id=str(index),
            thread_id=str(index),
            subject="Verify your email",
            sender="no-reply@greenhouse.io",
            date=_rfc(now),
            body="Use verification code 123456 to continue.",
        )
        for index in range(1001)
    ]

    with pytest.raises(mail_source.MailSourceOverflowError):
        inbox_auth.scan_gmail_for_auth_codes(
            messages=messages,
            max_messages=1000,
        )


def test_message_list_exact_candidate_bound_remains_bounded():
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    messages = [
        MailMessage(
            id=str(index),
            thread_id=str(index),
            subject="Unrelated",
            sender="news@example.com",
            date=_rfc(old),
            body="newsletter",
        )
        for index in range(1000)
    ]

    assert inbox_auth.scan_gmail_for_auth_codes(
        messages=messages,
        max_messages=1000,
    ) == []


def test_scan_nonpositive_message_budget_does_not_touch_message_list():
    class _MustNotIterate:
        def __getitem__(self, key):
            raise AssertionError(f"message list touched: {key}")

    assert inbox_auth.scan_gmail_for_auth_codes(
        messages=_MustNotIterate(), max_messages=0
    ) == []
    assert inbox_auth.scan_gmail_for_auth_codes(
        messages=_MustNotIterate(), max_messages=-1
    ) == []


def test_scan_nonpositive_service_budget_does_not_touch_gmail_api():
    class _MustNotBuildService:
        def users(self):
            raise AssertionError("Gmail API touched")

    assert inbox_auth.scan_gmail_for_auth_codes(
        service=_MustNotBuildService(), max_messages=0
    ) == []
    assert inbox_auth.scan_gmail_for_auth_codes(
        service=_MustNotBuildService(), max_messages=-1
    ) == []


def test_watch_nonpositive_budget_does_not_resolve_mail_source(monkeypatch):
    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source",
        lambda: (_ for _ in ()).throw(AssertionError("mail source touched")),
    )

    assert inbox_auth.watch_gmail_for_auth_code(max_messages=0) is None
    assert inbox_auth.watch_gmail_for_auth_code(max_messages=-1) is None


def test_watcher_candidate_overflow_returns_no_hint_without_retry(monkeypatch):
    calls = 0

    class _OverflowSource:
        def fetch(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise mail_source.MailSourceOverflowError("candidate overflow")

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source",
        lambda **_kwargs: _OverflowSource(),
    )

    assert inbox_auth.watch_gmail_for_auth_code(
        timeout_seconds=1,
        poll_seconds=0,
        max_errors=3,
        max_messages=1000,
    ) is None
    assert calls == 1


@pytest.mark.parametrize("budget", [True, 1.5, float("nan"), float("inf"), "1.5", "nan", ""])
def test_scanners_reject_malformed_budget_without_touching_inputs(budget):
    class _MustNotTouch:
        def __getitem__(self, key):
            raise AssertionError(key)

    with pytest.raises((TypeError, ValueError), match="max_messages"):
        inbox_auth.scan_gmail_for_auth_codes(messages=_MustNotTouch(), max_messages=budget)
    with pytest.raises((TypeError, ValueError), match="max_messages"):
        inbox_auth.scan_gmail_for_auth_codes(service=_MustNotTouch(), max_messages=budget)


def test_scanners_reject_budget_above_1000_without_touching_inputs():
    class _MustNotTouch:
        def __getitem__(self, key):
            raise AssertionError(key)

    with pytest.raises(ValueError, match="1000"):
        inbox_auth.scan_gmail_for_auth_codes(messages=_MustNotTouch(), max_messages=1001)
    with pytest.raises(ValueError, match="1000"):
        inbox_auth.scan_gmail_for_auth_codes(service=_MustNotTouch(), max_messages=1001)


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

        def get(  # noqa: A002
            self,
            userId,
            id,
            format,
            fields=None,
            metadataHeaders=None,
        ):
            assert format == "metadata"
            return _FakeRequest(
                {
                    "id": id,
                    "threadId": "t2",
                    "sizeEstimate": 100,
                    "snippet": body,
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": subject},
                            {"name": "From", "value": sender},
                            {"name": "Date", "value": _rfc(now)},
                        ]
                    },
                }
            )

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
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            assert "verification" in gmail_raw_query
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


def test_watch_continues_after_losing_atomic_message_claim(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    calls = {"fetch": 0, "claim": []}

    def message(message_id, code, seconds):
        return MailMessage(
            id=message_id,
            thread_id=message_id,
            subject="Verify your email",
            sender="no-reply@greenhouse.io",
            date=_rfc(now + dt.timedelta(seconds=seconds)),
            body=f"Use verification code {code} to continue.",
        )

    class _RacingSource:
        def fetch(self, **_kwargs):
            calls["fetch"] += 1
            if calls["fetch"] == 1:
                return [message("lost", "111111", 1)]
            return [message("lost", "111111", 1), message("won", "222222", 2)]

    def claim(match):
        calls["claim"].append(match.message_id)
        return match.message_id == "won"

    monkeypatch.setattr("applypilot.mail_source.get_mail_source", lambda: _RacingSource())

    match = inbox_auth.watch_gmail_for_auth_code(
        timeout_seconds=1,
        poll_seconds=0,
        max_errors=1,
        minutes=10,
        max_messages=25,
        claim_match=claim,
    )

    assert match is not None
    assert match.message_id == "won"
    assert calls["claim"] == ["lost", "won"]


def test_watch_rejects_prechallenge_and_wrong_provider_messages(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)

    class _FakeSource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            assert "verification" in gmail_raw_query
            return [
                MailMessage(
                    id="stale-greenhouse", thread_id="1", subject="Verify your email",
                    sender="no-reply@greenhouse.io", date=_rfc(now - dt.timedelta(minutes=2)),
                    body="Use verification code 111111 to continue.",
                ),
                MailMessage(
                    id="fresh-workday", thread_id="2", subject="Verify your email",
                    sender="no-reply@workday.com", date=_rfc(now + dt.timedelta(seconds=5)),
                    body="Use verification code 222222 to continue.",
                ),
                MailMessage(
                    id="fresh-greenhouse", thread_id="3", subject="Verify your email",
                    sender="no-reply@greenhouse-mail.io", date=_rfc(now + dt.timedelta(seconds=10)),
                    body="Use verification code 333333 to continue.",
                ),
            ]

    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: _FakeSource()
    )

    match = inbox_auth.watch_gmail_for_auth_code(
        not_before=now,
        provider_domain="greenhouse.io",
        timeout_seconds=1,
        poll_seconds=0,
        max_errors=1,
        minutes=15,
        max_messages=1000,
    )

    assert match is not None
    assert match.message_id == "fresh-greenhouse"
    assert match.candidate.value == "333333"


def test_watch_gmail_for_auth_code_defaults_to_mail_source(monkeypatch):
    """service=None -> resolves via get_mail_source().fetch(...) then scans."""
    seen = {}

    class _FakeSource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            seen["since_days"] = since_days
            seen["max_messages"] = max_messages
            seen["gmail_raw_query"] = gmail_raw_query
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
    assert "verification" in seen["gmail_raw_query"]


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
            self._row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            if "pg_try_advisory_lock" in query:
                self._row = {"acquired": True}
            elif "pg_advisory_unlock" in query:
                self._row = {"released": True}
            elif "fleet_controller_otp_pending" in query:
                self._row = {"pending": self._rows}

        def fetchone(self):
            return self._row

        def fetchall(self):
            return self._rows

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return _FakeCursor(self._rows)

        def commit(self):
            pass

        def rollback(self):
            pass

    pending_row = {"id": 1, "requested_at": dt.datetime.now(dt.timezone.utc)}
    conn = _FakeConn([pending_row])

    n = otp_relay.answer_pending(conn, window_minutes=15, max_messages=25)

    assert n == 0
    assert scan_calls["messages"] == ["sentinel-messages"]
    assert fetch_calls["max_messages"] == 25
    assert "verification" in fetch_calls["gmail_raw_query"]


def test_answer_pending_does_not_fetch_mail_without_pending_rows(fleet_db, monkeypatch):
    from applypilot.apply import pgqueue
    from applypilot.fleet import schema as fleet_schema

    def fail_if_resolved():
        raise AssertionError("mail source must not be resolved without pending rows")

    monkeypatch.setattr("applypilot.mail_source.get_mail_source", fail_if_resolved)

    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        assert otp_relay.answer_pending(conn) == 0


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
