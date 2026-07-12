"""Tests for the challenge triage ops in run_action's registry:
challenge_requeue / challenge_skip / challenge_skip_host.

These are the ONLY console mutations for parked auth walls, and per the task's
safety contract they must route EXCLUSIVELY through the existing
queue.resolve_challenge (apply/ats lane) and queue.resolve_linkedin_challenge
(linkedin lane) primitives -- no new UPDATE of queue rows here.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app
from applypilot.fleet import queue


def _seed_apply(conn, url, *, host, score=5.0, company="Co", title="Role", approved="b1"):
    queue.push_apply_jobs(conn, [{
        "url": url, "company": company, "title": title,
        "application_url": f"{url}/apply", "score": score,
        "target_host": host,
    }], approved_batch=approved)


def _seed_li(conn, url, *, score=9.0, company="Co", title="Role", batch="b1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO linkedin_queue "
            "(url, company, title, application_url, score, status, lane, approved_batch, dedup_key, "
            "linkedin_resolve_status, linkedin_resolved_at) "
            "VALUES (%s,%s,%s,%s,%s,'queued','ats',%s,%s,'easy_apply',now())",
            (url, company, title, url, score, batch, f"dk-{url}"),
        )
    conn.commit()


def _park_apply(conn, url, *, worker="w1"):
    """Lease (by url, lease_apply always takes the top score so we can't assume order)
    + park an apply_queue row so it looks like a real frozen auth wall."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET status='leased', lease_owner=%s, "
            "lease_expires_at=now() + interval '20 minutes', attempts=attempts+1, updated_at=now() "
            "WHERE url=%s AND status='queued'", (worker, url))
        assert cur.rowcount == 1
    conn.commit()
    assert queue.park_challenge(conn, worker, url) is True


def _park_li(conn, url, *, worker="w1"):
    leased = queue.lease_linkedin(conn, worker, public_ip="2.2.2.2", owner_ip="2.2.2.2", min_gap_seconds=0)
    assert leased and leased["url"] == url
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET apply_status='challenge_pending', "
            "lease_expires_at = now() + interval '3650 days', updated_at=now() "
            "WHERE url=%s AND lease_owner=%s", (url, worker))
        assert cur.rowcount == 1
    conn.commit()


def _open_challenge(conn, url, *, kind="visible_captcha"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
            "VALUES (%s, %s, %s, now() - interval '1 hour')",
            (url, kind, "home"),
        )
    conn.commit()


# ---- (1) requeue round-trip -----------------------------------------------

def test_challenge_requeue_apply_round_trip_then_idempotent(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "https://boards.greenhouse.io/acme/1", host="boards.greenhouse.io")
        conn.commit()
        _park_apply(conn, "https://boards.greenhouse.io/acme/1")
        _open_challenge(conn, "https://boards.greenhouse.io/acme/1")

    ok, msg = console_app.run_action({
        "action": "challenge_requeue",
        "url": "https://boards.greenhouse.io/acme/1",
        "lane": "apply",
    })
    assert ok is True
    assert "queue_rows: 1" in msg

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status FROM apply_queue WHERE url=%s",
                        ("https://boards.greenhouse.io/acme/1",))
            row = cur.fetchone()
            assert row["status"] == "queued"
            assert row["apply_status"] is None
            cur.execute("SELECT resolved_at, outcome FROM auth_challenge WHERE url=%s",
                        ("https://boards.greenhouse.io/acme/1",))
            ch = cur.fetchone()
            assert ch["resolved_at"] is not None
            assert ch["outcome"] == "solved"

    # Run again -> idempotent success no-op (queue_rows: 0), never an error.
    ok2, msg2 = console_app.run_action({
        "action": "challenge_requeue",
        "url": "https://boards.greenhouse.io/acme/1",
        "lane": "apply",
    })
    assert ok2 is True
    assert "queue_rows: 0" in msg2


# ---- (2) skip -> lane's existing semantics + outcome 'skipped' -------------

def test_challenge_skip_apply_sets_blocked_and_skipped_outcome(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    url = "https://boards.greenhouse.io/acme/2"
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, url, host="boards.greenhouse.io")
        conn.commit()
        _park_apply(conn, url)
        _open_challenge(conn, url)

    ok, msg = console_app.run_action({"action": "challenge_skip", "url": url, "lane": "apply"})
    assert ok is True
    assert "queue_rows: 1" in msg

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url=%s", (url,))
            row = cur.fetchone()
            assert row["status"] == "blocked"
            assert row["apply_status"] == "challenge_skipped"
            assert row["apply_error"] == "challenge_skipped"
            cur.execute("SELECT outcome FROM auth_challenge WHERE url=%s", (url,))
            assert cur.fetchone()["outcome"] == "skipped"


def test_challenge_skip_linkedin_sets_blocked_and_skipped_outcome(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    url = "https://www.linkedin.com/jobs/view/9"
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, url)
        _park_li(conn, url)
        _open_challenge(conn, url, kind="email_otp")

    ok, msg = console_app.run_action({"action": "challenge_skip", "url": url, "lane": "linkedin"})
    assert ok is True
    assert "queue_rows: 1" in msg

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status, apply_error FROM linkedin_queue WHERE url=%s", (url,))
            row = cur.fetchone()
            assert row["status"] == "blocked"
            assert row["apply_status"] == "challenge_skipped"
            assert row["apply_error"] == "challenge_skipped"
            cur.execute("SELECT outcome FROM auth_challenge WHERE url=%s", (url,))
            assert cur.fetchone()["outcome"] == "skipped"


def test_resolving_one_lane_preserves_shared_challenge_for_other_parked_lane(fleet_db):
    url = "https://shared.example/jobs/1"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, lease_owner, lease_expires_at, apply_status) "
            "VALUES (%s,%s,9,'leased','ats','apply-worker',now()+interval '3650 days','challenge_pending')",
            (url, url),
        )
        cur.execute(
            "INSERT INTO linkedin_queue "
            "(url, application_url, score, status, lane, lease_owner, lease_expires_at, apply_status) "
            "VALUES (%s,%s,9,'leased','linkedin','linkedin-worker',now()+interval '3650 days','challenge_pending')",
            (url, url),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route) VALUES (%s,'login_gate','owner_inbox')",
            (url,),
        )
        conn.commit()

        assert queue.resolve_challenge(conn, url, requeue=True) is True
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url=%s", (url,))
        assert cur.fetchone()["resolved_at"] is None
        cur.execute("SELECT apply_status FROM linkedin_queue WHERE url=%s", (url,))
        assert cur.fetchone()["apply_status"] == "challenge_pending"

        assert queue.resolve_linkedin_challenge(conn, url, requeue=True) is True
        cur.execute("SELECT outcome, resolved_at FROM auth_challenge WHERE url=%s", (url,))
        challenge = cur.fetchone()
        assert challenge["outcome"] == "solved"
        assert challenge["resolved_at"] is not None


# ---- (3) lane isolation: same url parked in BOTH queues --------------------

def test_challenge_op_never_crosses_lanes_for_same_url(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    # Deliberately identical url string parked independently in both queues.
    shared_url = "https://shared-host.example.com/job/1"
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, shared_url, host="shared-host.example.com")
        conn.commit()
        _park_apply(conn, shared_url)
        _seed_li(conn, shared_url)
        _park_li(conn, shared_url)

    # Resolve the ATS side only.
    ok, msg = console_app.run_action({"action": "challenge_requeue", "url": shared_url, "lane": "apply"})
    assert ok is True
    assert "queue_rows: 1" in msg

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status FROM apply_queue WHERE url=%s", (shared_url,))
            ats_row = cur.fetchone()
            assert ats_row["status"] == "queued"  # resolved
            cur.execute("SELECT status, apply_status FROM linkedin_queue WHERE url=%s", (shared_url,))
            li_row = cur.fetchone()
            # linkedin row is COMPLETELY untouched by the ats-lane op.
            assert li_row["status"] == "leased"
            assert li_row["apply_status"] == "challenge_pending"

    # Now resolve the LinkedIn side; the (already-resolved) ats row must stay untouched.
    ok2, msg2 = console_app.run_action({"action": "challenge_requeue", "url": shared_url, "lane": "linkedin"})
    assert ok2 is True
    assert "queue_rows: 1" in msg2

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url=%s", (shared_url,))
            assert cur.fetchone()["status"] == "queued"
            cur.execute("SELECT status, apply_status FROM linkedin_queue WHERE url=%s", (shared_url,))
            li_row = cur.fetchone()
            assert li_row["status"] == "queued"
            assert li_row["apply_status"] is None


def test_unknown_lane_returns_false_not_exception(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    ok, msg = console_app.run_action({
        "action": "challenge_requeue", "url": "https://x.example.com/1", "lane": "bogus",
    })
    assert ok is False
    assert msg == "lane must be one of: apply, linkedin; got bogus"


# ---- (4) skip_host caps at 200 and only hits that host ---------------------

def test_challenge_skip_host_scopes_to_host_and_lane(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        for i in range(3):
            url = f"https://boards.greenhouse.io/acme/h{i}"
            _seed_apply(conn, url, host="boards.greenhouse.io")
        conn.commit()
        for i in range(3):
            _park_apply(conn, f"https://boards.greenhouse.io/acme/h{i}", worker=f"w{i}")
        # A parked row on a DIFFERENT host must not be touched.
        other_url = "https://jobs.lever.co/other/1"
        _seed_apply(conn, other_url, host="jobs.lever.co")
        conn.commit()
        _park_apply(conn, other_url, worker="w-other")

    ok, msg = console_app.run_action({
        "action": "challenge_skip_host", "host": "boards.greenhouse.io", "lane": "apply",
    })
    assert ok is True
    assert "3" in msg

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url LIKE %s",
                        ("https://boards.greenhouse.io/%",))
            rows = cur.fetchall()
            assert len(rows) == 3
            assert all(r["status"] == "blocked" for r in rows)
            cur.execute("SELECT status, apply_status FROM apply_queue WHERE url=%s", (other_url,))
            other = cur.fetchone()
            assert other["status"] == "leased"
            assert other["apply_status"] == "challenge_pending"


def test_challenge_skip_host_caps_at_200(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    host = "cap-test.example.com"
    N = 205
    with pgqueue.connect(fleet_db) as conn:
        for i in range(N):
            _seed_apply(conn, f"https://{host}/j{i}", host=host)
        conn.commit()
        for i in range(N):
            _park_apply(conn, f"https://{host}/j{i}", worker=f"cw{i}")

    ok, msg = console_app.run_action({
        "action": "challenge_skip_host", "host": host, "lane": "apply",
    })
    assert ok is True

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM apply_queue WHERE url LIKE %s AND status='blocked'",
                        (f"https://{host}/%",))
            blocked = cur.fetchone()["n"]
            assert blocked == 200
            cur.execute("SELECT count(*) AS n FROM apply_queue WHERE url LIKE %s AND status='leased'",
                        (f"https://{host}/%",))
            still_leased = cur.fetchone()["n"]
            assert still_leased == 5


# ---- (5) LinkedIn halt state untouched --------------------------------------

def test_linkedin_challenge_op_does_not_touch_rate_governor_halt(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    url = "https://www.linkedin.com/jobs/view/77"
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, url)
        _park_li(conn, url)
        _open_challenge(conn, url, kind="visible_captcha")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, halted_until, min_gap_seconds, base_min_gap_seconds, "
                "daily_cap, count_24h, breaker_state) "
                "VALUES ('account:linkedin', now() + interval '6 hours', 45, 45, 20, 3, 'ok') "
                "ON CONFLICT (scope_key) DO UPDATE SET halted_until=EXCLUDED.halted_until, "
                "min_gap_seconds=EXCLUDED.min_gap_seconds, base_min_gap_seconds=EXCLUDED.base_min_gap_seconds, "
                "daily_cap=EXCLUDED.daily_cap, count_24h=EXCLUDED.count_24h, breaker_state=EXCLUDED.breaker_state")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rate_governor WHERE scope_key='account:linkedin'")
            before = dict(cur.fetchone())

    ok, msg = console_app.run_action({"action": "challenge_requeue", "url": url, "lane": "linkedin"})
    assert ok is True

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rate_governor WHERE scope_key='account:linkedin'")
            after = dict(cur.fetchone())

    assert after == before, "resolving a linkedin challenge must NEVER touch rate_governor (halt state)"
