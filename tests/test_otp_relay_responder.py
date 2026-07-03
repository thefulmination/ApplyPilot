"""Home responder: match Gmail codes to pending requests (time-based, single-assign)."""
import datetime as dt

from applypilot.apply import pgqueue
from applypilot.fleet import otp_relay, schema as fleet_schema


class _Cand:
    def __init__(self, value, kind="code"):
        self.value, self.kind = value, kind


class _Match:
    def __init__(self, message_id, received_at, value, kind="code"):
        self.message_id = message_id
        self.received_at = received_at  # RFC2822 string
        self.candidate = _Cand(value, kind)


class _FakeGmail:
    """Stands in for a Gmail service via scan_gmail_for_auth_codes monkeypatch."""
    def __init__(self, matches):
        self.matches = matches


def _rfc(when: dt.datetime) -> str:
    from email.utils import format_datetime
    return format_datetime(when)


def _fresh(fleet_db):
    conn = pgqueue.connect(fleet_db)
    fleet_schema.ensure_schema_v3(conn)
    return conn


def _pending(conn, worker_id="mac-0", domain="greenhouse.io"):
    return otp_relay.request_code(conn, worker_id=worker_id, job_url="j",
                                  application_url=f"https://{domain}/a")


def test_answer_writes_code_for_email_after_request(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("m1", _rfc(now + dt.timedelta(seconds=30)), "554466")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 1
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
    assert got is not None and got.value == "554466"


def test_stale_email_before_request_is_not_matched(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    # email arrived 10 minutes BEFORE the request -> must not match
    matches = [_Match("m_old", _rfc(now - dt.timedelta(minutes=10)), "000000")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 0
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
    assert got is None


def test_two_requests_get_distinct_codes_one_message_each(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("mA", _rfc(now + dt.timedelta(seconds=20)), "111111"),
        _Match("mB", _rfc(now + dt.timedelta(seconds=40)), "222222"),
    ]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        r1 = _pending(conn, worker_id="mac-0")
        r2 = _pending(conn, worker_id="m2-0")
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 2
        c1 = otp_relay.poll_for_code(conn, r1, timeout_seconds=1, poll_seconds=0.1)
        c2 = otp_relay.poll_for_code(conn, r2, timeout_seconds=1, poll_seconds=0.1)
    vals = {c1.value, c2.value}
    assert vals == {"111111", "222222"}  # distinct codes, no double-assignment


def test_purge_expired_nulls_code_keeps_row(fleet_db, monkeypatch):
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **kw: [])
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE otp_request SET code='777', code_kind='code', "
                        "expires_at=now() - interval '1 min' WHERE id=%s", (rid,))
        conn.commit()
        purged = otp_relay.purge_expired(conn)
        assert purged == 1
        with conn.cursor() as cur:
            cur.execute("SELECT code, worker_id FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
    assert row["code"] is None and row["worker_id"] == "mac-0"  # audit row kept
