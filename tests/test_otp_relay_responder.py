"""Home responder: match Gmail codes to pending requests (time-based, single-assign)."""
import datetime as dt

from psycopg.pq import TransactionStatus

from applypilot.apply import pgqueue
from applypilot.fleet import otp_relay, schema as fleet_schema


class _Cand:
    def __init__(self, value, kind="code"):
        self.value, self.kind = value, kind


class _Match:
    def __init__(
        self,
        message_id,
        received_at,
        value,
        kind="code",
        sender="Greenhouse <no-reply@greenhouse-mail.io>",
    ):
        self.message_id = message_id
        self.received_at = received_at  # RFC2822 string
        self.sender = sender
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


def test_consumed_message_is_not_reused_in_later_responder_cycle(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("greenhouse-message-1", _rfc(now + dt.timedelta(seconds=20)), "111111")]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **kw: matches,
    )
    with _fresh(fleet_db) as conn:
        first = _pending(conn, worker_id="mac-0")
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 1
        assert otp_relay.poll_for_code(conn, first, timeout_seconds=0).value == "111111"

        second = _pending(conn, worker_id="mac-1")
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        assert otp_relay.poll_for_code(conn, second, timeout_seconds=0) is None

        with conn.cursor() as cur:
            cur.execute(
                "SELECT matched_message_id FROM otp_request WHERE id IN (%s, %s) ORDER BY id",
                (first, second),
            )
            rows = cur.fetchall()

    assert [row["matched_message_id"] for row in rows] == ["greenhouse-message-1", None]


def test_answer_history_lookup_is_bounded_to_candidate_message_ids(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("candidate-1", _rfc(now + dt.timedelta(seconds=10)), "111111"),
        _Match("candidate-2", _rfc(now + dt.timedelta(seconds=20)), "222222"),
    ]
    historical_ids = {f"historical-{index}" for index in range(10_000)}
    historical_ids.update(match.message_id for match in matches)
    queries = []

    class _Cursor:
        rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            if "SELECT id, requested_at, sender_hint" in query:
                assert "array_agg" not in query
                assert "CROSS JOIN" not in query
                self.rows = [{
                    "id": 1,
                    "requested_at": now,
                    "sender_hint": "greenhouse.io",
                }]
            elif "matched_message_id = ANY(%s)" in query:
                candidate_ids = params[0]
                assert set(candidate_ids) == {"candidate-1", "candidate-2"}
                assert len(candidate_ids) == 2
                self.rows = [
                    {"matched_message_id": message_id}
                    for message_id in candidate_ids
                    if message_id in historical_ids
                ]
            else:
                raise AssertionError(f"unexpected query: {query}")

        def fetchall(self):
            return self.rows

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kwargs: matches,
    )

    assert otp_relay.answer_pending(_Conn(), _FakeGmail(matches)) == 0
    assert len(queries) == 2


def test_answer_with_no_candidates_skips_history_query(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    queries = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchall(self):
            return [{
                "id": 1,
                "requested_at": now,
                "sender_hint": "greenhouse.io",
            }]

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kwargs: [],
    )

    assert otp_relay.answer_pending(_Conn(), _FakeGmail([])) == 0
    assert len(queries) == 1


def test_scan_time_reservation_is_filtered_and_other_provider_is_answered(
    fleet_db, monkeypatch,
):
    now = dt.datetime.now(dt.timezone.utc)
    greenhouse = _Match(
        "scan-reserved-greenhouse", _rfc(now + dt.timedelta(seconds=10)), "111111",
    )
    workday = _Match(
        "scan-workday", _rfc(now + dt.timedelta(seconds=20)), "222222",
        sender="Workday <no-reply@myworkday.com>",
    )

    with _fresh(fleet_db) as conn:
        greenhouse_request = _pending(conn, worker_id="scan-greenhouse")
        workday_request = _pending(
            conn, worker_id="scan-workday", domain="tenant.myworkdayjobs.com",
        )

        def scan_and_reserve(**_kwargs):
            with pgqueue.connect(fleet_db) as competing:
                with competing.cursor() as cur:
                    cur.execute(
                        "INSERT INTO otp_request "
                        "(worker_id, matched_message_id, consumed_at) "
                        "VALUES ('scan-audit', %s, now())",
                        (greenhouse.message_id,),
                    )
                competing.commit()
            return [greenhouse, workday]

        monkeypatch.setattr(
            otp_relay.inbox_auth, "scan_gmail_for_auth_codes", scan_and_reserve,
        )
        assert otp_relay.answer_pending(conn, _FakeGmail([])) == 1
        assert otp_relay.poll_for_code(
            conn, greenhouse_request, timeout_seconds=0,
        ) is None
        consumed = otp_relay.poll_for_code(
            conn, workday_request, timeout_seconds=0,
        )

    assert consumed is not None and consumed.value == "222222"


def test_unique_violation_race_rolls_back_and_continues(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    greenhouse = _Match(
        "raced-greenhouse", _rfc(now + dt.timedelta(seconds=10)), "111111",
    )
    workday = _Match(
        "raced-workday", _rfc(now + dt.timedelta(seconds=20)), "222222",
        sender="Workday <no-reply@myworkday.com>",
    )
    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        lambda **_kwargs: [greenhouse, workday],
    )
    original_matcher = otp_relay._match_belongs_to_request
    reserved = False

    def reserve_after_history_lookup(sender_hint, match):
        nonlocal reserved
        if not reserved and match.message_id == greenhouse.message_id:
            with pgqueue.connect(fleet_db) as competing:
                with competing.cursor() as cur:
                    cur.execute(
                        "INSERT INTO otp_request "
                        "(worker_id, matched_message_id, consumed_at) "
                        "VALUES ('race-audit', %s, now())",
                        (greenhouse.message_id,),
                    )
                competing.commit()
            reserved = True
        return original_matcher(sender_hint, match)

    monkeypatch.setattr(
        otp_relay, "_match_belongs_to_request", reserve_after_history_lookup,
    )

    with _fresh(fleet_db) as conn:
        greenhouse_request = _pending(conn, worker_id="race-greenhouse")
        workday_request = _pending(
            conn, worker_id="race-workday", domain="tenant.myworkdayjobs.com",
        )

        assert otp_relay.answer_pending(conn, _FakeGmail([])) == 1
        assert otp_relay.poll_for_code(
            conn, greenhouse_request, timeout_seconds=0,
        ) is None
        consumed = otp_relay.poll_for_code(
            conn, workday_request, timeout_seconds=0,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS usable")
            usable = cur.fetchone()["usable"]

    assert consumed is not None and consumed.value == "222222"
    assert usable == 1


def test_oldest_request_gets_earliest_code(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("mEarly", _rfc(now + dt.timedelta(seconds=20)), "111111"),
        _Match("mLate", _rfc(now + dt.timedelta(seconds=40)), "222222"),
    ]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        r_old = _pending(conn, worker_id="mac-0")   # filed first
        r_new = _pending(conn, worker_id="m2-0")    # filed second
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 2
        c_old = otp_relay.poll_for_code(conn, r_old, timeout_seconds=1, poll_seconds=0.1)
        c_new = otp_relay.poll_for_code(conn, r_new, timeout_seconds=1, poll_seconds=0.1)
    # oldest request gets the earliest code (nearest-in-time pairing)
    assert c_old.value == "111111" and c_new.value == "222222"


def test_answer_does_not_shorten_request_window(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("m1", _rfc(now + dt.timedelta(seconds=10)), "334455")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0", job_url="j",
                                     application_url="https://greenhouse.io/a", ttl_seconds=300)
        # answered_ttl (120s) is shorter than the 300s request ttl; must not shorten expiry
        otp_relay.answer_pending(conn, _FakeGmail(matches), answered_ttl_seconds=120)
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at, (expires_at > now() + interval '200 seconds') AS still_long "
                        "FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
    assert row["still_long"] is True  # window preserved near the original 300s, not cut to 120s


def test_answer_pending_does_not_revive_request_that_expires_during_mail_scan(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("m1", _rfc(now + dt.timedelta(seconds=10)), "334455")]

    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn,
            worker_id="mac-0",
            job_url="j",
            application_url="https://greenhouse.io/a",
            ttl_seconds=300,
        )

        def fake_scan(**_kw):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE otp_request SET expires_at=now() - interval '1 second' WHERE id=%s",
                    (rid,),
                )
            conn.commit()
            return matches

        monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", fake_scan)

        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, answered_at, consumed_at FROM otp_request WHERE id=%s",
                (rid,),
            )
            row = cur.fetchone()

    assert row["code"] is None
    assert row["answered_at"] is None
    assert row["consumed_at"] is None


def test_unparseable_email_date_is_skipped(fleet_db, monkeypatch):
    matches = [_Match("mBad", "not-a-real-date", "000000")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
    assert got is None


def test_future_dated_email_is_not_matched(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("mFuture", _rfc(now + dt.timedelta(days=1)), "123123")]
    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        lambda **kw: matches,
    )

    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)

    assert got is None


def test_answer_pending_releases_select_transaction_before_mail_scan(fleet_db, monkeypatch):
    observed_statuses = []

    with _fresh(fleet_db) as conn:
        _pending(conn)

        def fake_scan(**_kw):
            observed_statuses.append(conn.info.transaction_status)
            return []

        monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", fake_scan)
        assert otp_relay.answer_pending(conn, _FakeGmail([])) == 0

    assert observed_statuses == [TransactionStatus.IDLE]


def test_purge_expired_nulls_code_keeps_row(fleet_db, monkeypatch):
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **kw: [])
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE otp_request SET code='777', code_kind='code', "
                        "matched_message_id='purge-message', wait_started_at=now(), "
                        "expires_at=now() - interval '1 min' WHERE id=%s", (rid,))
        conn.commit()
        purged = otp_relay.purge_expired(conn)
        assert purged == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, worker_id, matched_message_id, wait_started_at "
                "FROM otp_request WHERE id=%s", (rid,),
            )
            row = cur.fetchone()
    assert row["code"] is None and row["worker_id"] == "mac-0"  # audit row kept
    assert row["matched_message_id"] == "purge-message"
    assert row["wait_started_at"] is not None
