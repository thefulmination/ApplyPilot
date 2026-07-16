"""Home responder: match Gmail codes to pending requests (time-based, single-assign)."""
import datetime as dt
import sys

import pytest
from psycopg.pq import TransactionStatus

from applypilot import auth_matching
from applypilot import mail_source
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


def test_two_same_provider_requests_and_two_messages_fail_closed(fleet_db, monkeypatch):
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
        assert n == 0
        c1 = otp_relay.poll_for_code(conn, r1, timeout_seconds=1, poll_seconds=0.1)
        c2 = otp_relay.poll_for_code(conn, r2, timeout_seconds=1, poll_seconds=0.1)
    assert c1 is None
    assert c2 is None


def test_two_same_provider_requests_and_one_message_fail_closed(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("mA", _rfc(now + dt.timedelta(seconds=20)), "111111")]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kw: matches,
    )

    with _fresh(fleet_db) as conn:
        r1 = _pending(conn, worker_id="same-provider-1")
        r2 = _pending(conn, worker_id="same-provider-2")
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        assert otp_relay.poll_for_code(conn, r1, timeout_seconds=0) is None
        assert otp_relay.poll_for_code(conn, r2, timeout_seconds=0) is None


def test_different_provider_requests_get_matching_messages(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("greenhouse-message", _rfc(now + dt.timedelta(seconds=20)), "111111"),
        _Match(
            "workday-message",
            _rfc(now + dt.timedelta(seconds=40)),
            "222222",
            sender="Workday <no-reply@myworkday.com>",
        ),
    ]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kw: matches,
    )

    with _fresh(fleet_db) as conn:
        greenhouse = _pending(conn, worker_id="provider-greenhouse")
        workday = _pending(
            conn,
            worker_id="provider-workday",
            domain="tenant.myworkdayjobs.com",
        )
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 2
        greenhouse_code = otp_relay.poll_for_code(conn, greenhouse, timeout_seconds=0)
        workday_code = otp_relay.poll_for_code(conn, workday, timeout_seconds=0)

    assert greenhouse_code is not None and greenhouse_code.value == "111111"
    assert workday_code is not None and workday_code.value == "222222"


def test_mixed_graph_assigns_only_mutual_unique_pair(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("greenhouse-1", _rfc(now + dt.timedelta(seconds=10)), "111111"),
        _Match("greenhouse-2", _rfc(now + dt.timedelta(seconds=20)), "222222"),
        _Match(
            "workday-1",
            _rfc(now + dt.timedelta(seconds=30)),
            "333333",
            sender="Workday <no-reply@myworkday.com>",
        ),
    ]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kw: matches,
    )

    with _fresh(fleet_db) as conn:
        greenhouse_1 = _pending(conn, worker_id="mixed-greenhouse-1")
        greenhouse_2 = _pending(conn, worker_id="mixed-greenhouse-2")
        workday = _pending(
            conn,
            worker_id="mixed-workday",
            domain="tenant.myworkdayjobs.com",
        )
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 1
        assert otp_relay.poll_for_code(conn, greenhouse_1, timeout_seconds=0) is None
        assert otp_relay.poll_for_code(conn, greenhouse_2, timeout_seconds=0) is None
        workday_code = otp_relay.poll_for_code(conn, workday, timeout_seconds=0)

    assert workday_code is not None and workday_code.value == "333333"


def test_unique_assignments_finds_globally_unique_complete_matching():
    base = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone.utc)
    requests = [
        {"id": 1, "requested_at": base, "sender_hint": "greenhouse.io"},
        {
            "id": 2,
            "requested_at": base + dt.timedelta(minutes=2),
            "sender_hint": "greenhouse.io",
        },
    ]
    first = _Match("m1", _rfc(base + dt.timedelta(minutes=1)), "111111")
    second = _Match("m2", _rfc(base + dt.timedelta(minutes=3)), "222222")

    assignments = otp_relay._unique_assignments(
        requests,
        [
            (first, base + dt.timedelta(minutes=1)),
            (second, base + dt.timedelta(minutes=3)),
        ],
        set(),
        0,
    )

    assert [(request["id"], match.message_id) for request, match in assignments] == [
        (1, "m1"),
        (2, "m2"),
    ]


def test_perfect_matching_handles_1000_item_augmenting_path_iteratively():
    size = 1000
    request_edges = {
        request_id: list(range(size - 1, request_id - 1, -1))
        for request_id in range(size)
    }

    original_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(500)
        matching = auth_matching.perfect_component_matching(range(size), request_edges)
    finally:
        sys.setrecursionlimit(original_recursion_limit)

    assert matching == {request_id: request_id for request_id in range(size)}
    assert auth_matching.matching_is_unique(matching, request_edges) is True
    assert not hasattr(otp_relay, "_perfect_component_matching")
    assert not hasattr(otp_relay, "_matching_is_unique")


def test_answer_pending_finds_globally_unique_complete_matching(fleet_db, monkeypatch):
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=4)
    matches = [
        _Match("global-m1", _rfc(base + dt.timedelta(minutes=1)), "111111"),
        _Match("global-m2", _rfc(base + dt.timedelta(minutes=3)), "222222"),
    ]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kw: matches,
    )

    with _fresh(fleet_db) as conn:
        first = _pending(conn, worker_id="global-first")
        second = _pending(conn, worker_id="global-second")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE otp_request SET requested_at=%s WHERE id=%s",
                (base, first),
            )
            cur.execute(
                "UPDATE otp_request SET requested_at=%s WHERE id=%s",
                (base + dt.timedelta(minutes=2), second),
            )
        conn.commit()

        assert otp_relay.answer_pending(
            conn, _FakeGmail(matches), skew_seconds=0,
        ) == 2
        first_code = otp_relay.poll_for_code(conn, first, timeout_seconds=0)
        second_code = otp_relay.poll_for_code(conn, second, timeout_seconds=0)

    assert first_code is not None and first_code.value == "111111"
    assert second_code is not None and second_code.value == "222222"


def test_legacy_null_expiry_rows_cannot_starve_current_request(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("current-message", _rfc(now + dt.timedelta(seconds=10)), "654321"),
    ]
    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **_kw: matches,
    )

    with _fresh(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request "
                "(worker_id, url, sender_hint, requested_at, expires_at) "
                "SELECT 'legacy-' || value, 'https://greenhouse.io/' || value, "
                "       'greenhouse.io', now() - interval '1 day', NULL "
                "FROM generate_series(1, 1001) AS value"
            )
        conn.commit()
        current = _pending(conn, worker_id="current-after-legacy")

        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 1
        code = otp_relay.poll_for_code(conn, current, timeout_seconds=0)

    assert code is not None and code.value == "654321"


def test_answer_pending_bounds_pending_and_candidate_matching_sets(
    fleet_db, monkeypatch,
):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match(f"bounded-{index}", _rfc(now), f"{index % 1_000_000:06d}")
        for index in range(1001)
    ]
    observed = {}

    def scan(**kwargs):
        observed["scan_max_messages"] = kwargs["max_messages"]
        return matches

    def capture(pending, parsed, used_ids, skew_seconds):
        observed["pending"] = len(pending)
        observed["parsed"] = len(parsed)
        return []

    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", scan)
    monkeypatch.setattr(otp_relay, "_unique_assignments", capture)

    with _fresh(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
                "SELECT 'bounded-' || value, 'https://greenhouse.io/' || value, "
                "       'greenhouse.io', now() + interval '5 minutes' "
                "FROM generate_series(1, 1000) AS value"
            )
        conn.commit()

        assert otp_relay.answer_pending(
            conn, _FakeGmail(matches), max_messages=5000,
        ) == 0

    assert observed == {
        "scan_max_messages": 1000,
        "pending": 1000,
        "parsed": 1000,
    }


def test_answer_pending_request_snapshot_overflow_fails_before_scan(
    fleet_db, monkeypatch,
):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("overflow-message", _rfc(now), "123456")]
    scan_called = False

    def fail_if_scanned(**_kwargs):
        nonlocal scan_called
        scan_called = True
        raise AssertionError("overflowed request snapshot must not scan mailbox")

    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        fail_if_scanned,
    )

    with _fresh(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
                "SELECT 'overflow-' || value, 'https://greenhouse.io/' || value, "
                "       'greenhouse.io', now() + interval '5 minutes' "
                "FROM generate_series(1, 1001) AS value"
            )
        conn.commit()

        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        assert conn.info.transaction_status == TransactionStatus.IDLE
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS changed FROM otp_request "
                "WHERE code IS NOT NULL OR answered_at IS NOT NULL "
                "   OR matched_message_id IS NOT NULL"
            )
            assert cur.fetchone()["changed"] == 0
        conn.commit()
        with _fresh(fleet_db) as verifier:
            with verifier.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (otp_relay._responder_lock_key(),),
                )
                assert cur.fetchone()["acquired"] is True
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (otp_relay._responder_lock_key(),),
                )
            verifier.commit()

    assert scan_called is False


def test_answer_pending_candidate_overflow_returns_zero_and_releases_lock(
    fleet_db, monkeypatch,
):
    def overflow(**_kwargs):
        raise mail_source.MailSourceOverflowError("candidate overflow")

    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        overflow,
    )

    with _fresh(fleet_db) as conn:
        request_id = _pending(conn, worker_id="candidate-overflow")
        assert otp_relay.answer_pending(conn, _FakeGmail([])) == 0
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert otp_relay.poll_for_code(
            conn,
            request_id,
            timeout_seconds=0,
        ) is None
        with _fresh(fleet_db) as verifier:
            with verifier.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (otp_relay._responder_lock_key(),),
                )
                assert cur.fetchone()["acquired"] is True
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (otp_relay._responder_lock_key(),),
                )
            verifier.commit()


def test_answer_pending_returns_without_scan_when_responder_lock_is_held(
    fleet_db, monkeypatch,
):
    scanner_called = False

    def fail_if_scanned(**_kwargs):
        nonlocal scanner_called
        scanner_called = True
        raise AssertionError("mail scanner must not run without the responder lock")

    monkeypatch.setattr(
        otp_relay.inbox_auth, "scan_gmail_for_auth_codes", fail_if_scanned,
    )

    with _fresh(fleet_db) as owner, _fresh(fleet_db) as contender:
        _pending(contender, worker_id="lock-contender")
        with owner.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (otp_relay._responder_lock_key(),),
            )
            assert cur.fetchone()["acquired"] is True
        owner.commit()

        try:
            assert otp_relay.answer_pending(contender, _FakeGmail([])) == 0
            assert scanner_called is False
        finally:
            with owner.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s) AS released",
                    (otp_relay._responder_lock_key(),),
                )
                assert cur.fetchone()["released"] is True
            owner.commit()


def test_answer_pending_releases_responder_lock_when_scanner_aborts_transaction(
    fleet_db, monkeypatch,
):
    with _fresh(fleet_db) as first, _fresh(fleet_db) as second:
        _pending(first, worker_id="lock-release")

        def abort_scan(**_kwargs):
            with first.cursor() as cur:
                cur.execute("SELECT 1 / 0")

        monkeypatch.setattr(
            otp_relay.inbox_auth, "scan_gmail_for_auth_codes", abort_scan,
        )

        with pytest.raises(Exception, match="division by zero"):
            otp_relay.answer_pending(first, _FakeGmail([]))

        with second.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (otp_relay._responder_lock_key(),),
            )
            assert cur.fetchone()["acquired"] is True
            cur.execute(
                "SELECT pg_advisory_unlock(%s) AS released",
                (otp_relay._responder_lock_key(),),
            )
            assert cur.fetchone()["released"] is True
        second.commit()


def test_answer_pending_preserves_original_error_when_cleanup_rollback_fails(
    monkeypatch,
):
    class CleanupFailure(Exception):
        pass

    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "pg_try_advisory_lock" in query:
                self.row = {"acquired": True}
            elif "pg_advisory_unlock" in query:
                self.conn.unlock_attempted = True

        def fetchone(self):
            return self.row

    class Conn:
        unlock_attempted = False

        def cursor(self):
            return Cursor(self)

        def commit(self):
            pass

        def rollback(self):
            raise CleanupFailure("rollback failed")

    def fail_responder(*_args, **_kwargs):
        raise RuntimeError("scanner failed")

    conn = Conn()
    monkeypatch.setattr(otp_relay, "_answer_pending_locked", fail_responder)

    with pytest.raises(RuntimeError, match="scanner failed"):
        otp_relay.answer_pending(conn, _FakeGmail([]))
    assert conn.unlock_attempted is True


def test_responder_lock_key_is_stable_signed_64_bit():
    key = otp_relay._responder_lock_key()

    assert -(2**63) <= key < 2**63
    assert key == otp_relay._responder_lock_key()
    assert key == 6115416092012062024


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
            if "pg_try_advisory_lock" in query:
                self.rows = [{"acquired": True}]
            elif "pg_advisory_unlock" in query:
                self.rows = []
            elif "fleet_controller_otp_pending" in query:
                queries.append((query, params))
                self.rows = [{"pending": [{
                    "id": 1, "requested_at": now.isoformat(), "sender_hint": "greenhouse.io",
                }]}]
            elif "fleet_controller_otp_used_messages" in query:
                queries.append((query, params))
                candidate_ids = params[0]
                assert set(candidate_ids) == {"candidate-1", "candidate-2"}
                assert len(candidate_ids) == 2
                self.rows = [{"used": [
                    message_id for message_id in candidate_ids if message_id in historical_ids
                ]}]
            else:
                raise AssertionError(f"unexpected query: {query}")

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def rollback(self):
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
                if "pg_try_advisory_lock" in query:
                    self._row = {"acquired": True}
                elif "pg_advisory_unlock" in query:
                    self._row = None
                elif "fleet_controller_otp_pending" in query:
                    queries.append((query, params))
                    self._row = {"pending": [{
                        "id": 1,
                        "requested_at": now.isoformat(),
                        "sender_hint": "greenhouse.io",
                    }]}
                else:
                    queries.append((query, params))

        def fetchone(self):
            return getattr(self, "_row", None)

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def rollback(self):
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


def test_same_provider_requests_do_not_use_arrival_order_to_disambiguate(
    fleet_db, monkeypatch,
):
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
        assert otp_relay.answer_pending(conn, _FakeGmail(matches)) == 0
        c_old = otp_relay.poll_for_code(conn, r_old, timeout_seconds=1, poll_seconds=0.1)
        c_new = otp_relay.poll_for_code(conn, r_new, timeout_seconds=1, poll_seconds=0.1)
    assert c_old is None
    assert c_new is None


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
