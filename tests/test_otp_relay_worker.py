"""Worker side: file an otp_request, poll the row, consume the code exactly once."""
import concurrent.futures as cf
import datetime as dt
import time
from email.utils import format_datetime

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import otp_relay, schema as fleet_schema


def _fresh(fleet_db):
    conn = pgqueue.connect(fleet_db)
    fleet_schema.ensure_schema_v3(conn)
    return conn


def _home_writes_code(conn, request_id, code="482913", kind="code"):
    with conn.cursor() as cur:
        cur.execute("UPDATE otp_request SET code=%s, code_kind=%s, answered_at=now() WHERE id=%s",
                    (code, kind, request_id))
    conn.commit()


def test_request_code_inserts_pending_row(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="https://li/jobs/1",
                                     application_url="https://job-boards.greenhouse.io/x/jobs/9")
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id, sender_hint, code, consumed_at, expires_at "
                        "FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
    assert row["worker_id"] == "mac-0"
    assert row["sender_hint"] == "job-boards.greenhouse.io"
    assert row["code"] is None and row["consumed_at"] is None
    assert row["expires_at"] is not None


def test_request_code_reuses_active_request_even_when_answered(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="https://li/jobs/1",
            application_url="https://greenhouse.io/a", ttl_seconds=60,
        )
        _home_writes_code(conn, rid)

        reused = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="https://li/jobs/1",
            application_url="https://greenhouse.io/a", ttl_seconds=120,
        )

    assert reused == rid


def test_request_code_does_not_extend_answered_code_lifetime(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    match = type("Match", (), {
        "message_id": "answered-lifetime",
        "received_at": format_datetime(now + dt.timedelta(seconds=5)),
        "sender": "no-reply@greenhouse-mail.io",
        "candidate": type("Candidate", (), {"value": "482913", "kind": "code"})(),
    })()
    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        lambda **_kwargs: [match],
    )

    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn, worker_id="answered-worker", job_url="j",
            application_url="https://greenhouse.io/answered", ttl_seconds=300,
        )
        assert otp_relay.answer_pending(
            conn, object(), answered_ttl_seconds=120,
        ) == 1
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM otp_request WHERE id=%s", (rid,))
            answered_expiry = cur.fetchone()["expires_at"]

        reused = otp_relay.request_code(
            conn, worker_id="answered-worker", job_url="j",
            application_url="https://greenhouse.io/answered", ttl_seconds=900,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM otp_request WHERE id=%s", (rid,))
            reused_expiry = cur.fetchone()["expires_at"]

    assert reused == rid
    assert reused_expiry == answered_expiry


@pytest.mark.parametrize("ttl_seconds", [0, -1, 1.5, "1.5", True, 86401])
def test_request_code_rejects_invalid_ttl_before_database_changes(fleet_db, ttl_seconds):
    with _fresh(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM otp_request")
            before = cur.fetchone()["n"]
        conn.commit()

        with pytest.raises(ValueError):
            otp_relay.request_code(
                conn, worker_id="invalid-ttl", job_url="j",
                application_url="https://greenhouse.io/invalid",
                ttl_seconds=ttl_seconds,
            )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM otp_request")
            after = cur.fetchone()["n"]

    assert after == before


def test_validate_ttl_accepts_integer_string_and_bounds():
    assert otp_relay._validate_ttl_seconds("300") == 300
    assert otp_relay._validate_ttl_seconds(1) == 1
    assert otp_relay._validate_ttl_seconds(86400) == 86400


def test_request_lock_key_is_stable_signed_64_bit():
    key = otp_relay._request_lock_key(
        "worker-1", "https://example.test/apply",
    )
    assert key == 1397446044822642768
    assert -(2 ** 63) <= key < 2 ** 63
    assert otp_relay._request_lock_key(
        "worker-1", "https://example.test/apply",
    ) == key
    assert otp_relay._request_lock_key("x", "y") == -6285053798989339351


def test_request_lock_timeout_is_bounded_and_rolls_back(monkeypatch):
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert "pg_try_advisory_xact_lock(%s)" in query
            assert params == (17,)

        def fetchone(self):
            return {"acquired": False}

    class _Conn:
        rollbacks = 0

        def cursor(self):
            return _Cursor()

        def rollback(self):
            self.rollbacks += 1

    conn = _Conn()
    monotonic_values = iter((10.0, 10.0, 10.04, 10.11))
    sleeps = []
    monkeypatch.setattr(otp_relay.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(otp_relay.time, "sleep", sleeps.append)

    with pytest.raises(TimeoutError, match="^timed out acquiring OTP request lock$"):
        otp_relay._acquire_request_lock(conn, 17, timeout_seconds=0.1)

    assert conn.rollbacks == 1
    assert all(0 < delay <= 0.05 for delay in sleeps)


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        float("nan"), float("inf"), float("-inf"), -0.01, True, "1", None,
        60.01, 10 ** 1000,
    ],
)
def test_request_lock_rejects_invalid_timeout_before_cursor(timeout_seconds):
    class _Conn:
        def cursor(self):
            raise AssertionError("cursor must not be opened for an invalid timeout")

    with pytest.raises(ValueError):
        otp_relay._acquire_request_lock(
            _Conn(), 17, timeout_seconds=timeout_seconds,
        )


def test_request_lock_zero_timeout_attempts_once_then_times_out(monkeypatch):
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert "pg_try_advisory_xact_lock(%s)" in query
            assert params == (17,)

        def fetchone(self):
            return {"acquired": False}

    class _Conn:
        attempts = 0
        rollbacks = 0

        def cursor(self):
            self.attempts += 1
            return _Cursor()

        def rollback(self):
            self.rollbacks += 1

    conn = _Conn()
    monkeypatch.setattr(otp_relay.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        otp_relay.time,
        "sleep",
        lambda _seconds: pytest.fail("zero timeout must not sleep"),
    )

    with pytest.raises(TimeoutError, match="^timed out acquiring OTP request lock$"):
        otp_relay._acquire_request_lock(conn, 17, timeout_seconds=0)

    assert conn.attempts == 1
    assert conn.rollbacks == 1


def test_concurrent_request_code_calls_share_one_active_row(fleet_db):
    worker_id = "concurrent-worker"
    target = "https://greenhouse.io/concurrent"
    lock_key = otp_relay._request_lock_key(worker_id, target)

    with pgqueue.connect(fleet_db) as blocker:
        fleet_schema.ensure_schema_v3(blocker)
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

        def request():
            with pgqueue.connect(fleet_db) as conn:
                return otp_relay.request_code(
                    conn, worker_id=worker_id, job_url="j",
                    application_url=target,
                )

        with cf.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(request) for _ in range(2)]
            time.sleep(0.1)
            blocker.commit()
            request_ids = [future.result(timeout=10) for future in futures]

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM otp_request "
                "WHERE worker_id=%s AND url=%s AND consumed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())",
                (worker_id, target),
            )
            active_count = cur.fetchone()["n"]

    assert len(set(request_ids)) == 1
    assert active_count == 1


def test_request_code_reuse_is_scoped_to_worker_and_target(fleet_db):
    with _fresh(fleet_db) as conn:
        base = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        other_worker = otp_relay.request_code(
            conn, worker_id="mac-1", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        other_target = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/b",
        )

    assert len({base, other_worker, other_target}) == 3


def test_request_code_reuse_extends_expiry_without_shortening(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="https://fallback.example/jobs/1",
            application_url="", ttl_seconds=300,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at, url, sender_hint FROM otp_request WHERE id=%s", (rid,))
            original = cur.fetchone()

        assert otp_relay.request_code(
            conn, worker_id="mac-0", job_url="https://fallback.example/jobs/1",
            application_url="", ttl_seconds=1,
        ) == rid
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM otp_request WHERE id=%s", (rid,))
            shorter = cur.fetchone()["expires_at"]

        assert otp_relay.request_code(
            conn, worker_id="mac-0", job_url="https://fallback.example/jobs/1",
            application_url="", ttl_seconds=600,
        ) == rid
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM otp_request WHERE id=%s", (rid,))
            extended = cur.fetchone()["expires_at"]

    assert original["url"] == "https://fallback.example/jobs/1"
    assert original["sender_hint"] == "fallback.example"
    assert shorter >= original["expires_at"]
    assert extended > shorter + dt.timedelta(seconds=200)


def test_poll_returns_code_then_single_use(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="j", application_url="https://greenhouse.io/a")
        _home_writes_code(conn, rid, code="482913", kind="code")
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=2, poll_seconds=0.1)
        assert got is not None and got.value == "482913" and got.kind == "code"
        # consumed: code nulled, consumed_at set
        with conn.cursor() as cur:
            cur.execute("SELECT code, consumed_at FROM otp_request WHERE id=%s", (rid,))
            r = cur.fetchone()
        assert r["code"] is None and r["consumed_at"] is not None
        # a second poll finds nothing (single-use)
        again = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
        assert again is None


def test_poll_times_out_when_no_answer(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="j", application_url="https://greenhouse.io/a")
        start = time.monotonic()
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.2)
        assert got is None
        assert time.monotonic() - start >= 1.0


def test_poll_ignores_expired_code(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0", job_url="j",
                                     application_url="https://greenhouse.io/a", ttl_seconds=1)
        with conn.cursor() as cur:  # answer but with an already-past expiry
            cur.execute("UPDATE otp_request SET code='999', code_kind='code', "
                        "answered_at=now(), expires_at=now() - interval '1 second' WHERE id=%s", (rid,))
        conn.commit()
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.2)
    assert got is None


def test_poll_sleep_never_exceeds_remaining_deadline(monkeypatch):
    sleeps = []
    monotonic_values = iter((10.0, 10.0, 11.0))

    monkeypatch.setattr(otp_relay, "_stamp_wait_started", lambda conn, request_id: None)
    monkeypatch.setattr(otp_relay, "_try_consume", lambda conn, request_id: None)
    monkeypatch.setattr(otp_relay.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(otp_relay.time, "sleep", sleeps.append)

    assert otp_relay.poll_for_code(object(), 7, timeout_seconds=1, poll_seconds=5) is None
    assert sum(sleeps) <= 1.0


def test_poll_clamps_nonpositive_interval_without_passing_deadline(monkeypatch):
    monkeypatch.setattr(otp_relay, "_stamp_wait_started", lambda conn, request_id: None)
    monkeypatch.setattr(otp_relay, "_try_consume", lambda conn, request_id: None)

    for poll_seconds in (0, -1):
        sleeps = []
        monotonic_values = iter((20.0, 20.0, 21.0))
        monkeypatch.setattr(otp_relay.time, "monotonic", lambda: next(monotonic_values))
        monkeypatch.setattr(otp_relay.time, "sleep", sleeps.append)

        assert otp_relay.poll_for_code(
            object(),
            8,
            timeout_seconds=1,
            poll_seconds=poll_seconds,
        ) is None
        assert sleeps == [0.05]
        assert sum(sleeps) <= 1.0


def test_poll_attempts_immediate_consume_with_zero_timeout(monkeypatch):
    expected = otp_relay.RelayCode(value="482913", kind="code")
    attempts = []
    sleeps = []

    def fake_consume(conn, request_id):
        attempts.append(request_id)
        return expected

    monkeypatch.setattr(otp_relay, "_stamp_wait_started", lambda conn, request_id: None)
    monkeypatch.setattr(otp_relay, "_try_consume", fake_consume)
    monkeypatch.setattr(otp_relay.time, "monotonic", lambda: 30.0)
    monkeypatch.setattr(otp_relay.time, "sleep", sleeps.append)

    assert otp_relay.poll_for_code(object(), 9, timeout_seconds=0, poll_seconds=5) == expected
    assert attempts == [9]
    assert sleeps == []


def test_poll_stamps_wait_started_at_once_before_immediate_consume(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        _home_writes_code(conn, rid)
        assert otp_relay.poll_for_code(conn, rid, timeout_seconds=0) is not None
        with conn.cursor() as cur:
            cur.execute("SELECT wait_started_at FROM otp_request WHERE id=%s", (rid,))
            stamped = cur.fetchone()["wait_started_at"]

    assert stamped is not None


def test_repeated_poll_preserves_wait_started_at(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="j",
            application_url="https://greenhouse.io/a",
        )
        assert otp_relay.poll_for_code(conn, rid, timeout_seconds=0) is None
        with conn.cursor() as cur:
            cur.execute("SELECT wait_started_at FROM otp_request WHERE id=%s", (rid,))
            first = cur.fetchone()["wait_started_at"]
        assert otp_relay.poll_for_code(conn, rid, timeout_seconds=0) is None
        with conn.cursor() as cur:
            cur.execute("SELECT wait_started_at FROM otp_request WHERE id=%s", (rid,))
            second = cur.fetchone()["wait_started_at"]

    assert first is not None and second == first


def test_poll_does_not_stamp_expired_or_consumed_request(fleet_db):
    with _fresh(fleet_db) as conn:
        expired = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="expired",
            application_url="https://greenhouse.io/expired",
        )
        consumed = otp_relay.request_code(
            conn, worker_id="mac-0", job_url="consumed",
            application_url="https://greenhouse.io/consumed",
        )
        with conn.cursor() as cur:
            cur.execute("UPDATE otp_request SET expires_at=now() - interval '1 second' WHERE id=%s", (expired,))
            cur.execute("UPDATE otp_request SET consumed_at=now() WHERE id=%s", (consumed,))
        conn.commit()

        assert otp_relay.poll_for_code(conn, expired, timeout_seconds=0) is None
        assert otp_relay.poll_for_code(conn, consumed, timeout_seconds=0) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, wait_started_at FROM otp_request WHERE id IN (%s, %s) ORDER BY id",
                (expired, consumed),
            )
            rows = cur.fetchall()

    assert all(row["wait_started_at"] is None for row in rows)
