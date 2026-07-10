"""Worker side: file an otp_request, poll the row, consume the code exactly once."""
import datetime as dt
import time

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
