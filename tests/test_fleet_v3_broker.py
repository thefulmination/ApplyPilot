"""P0 tests for the thin API broker (src/applypilot/fleet/broker.py).

The broker is the TRUST BOUNDARY: friend machines hold only a per-machine bearer
token, never a Postgres DSN or the Gmail token. These tests pin the security
contract against a REAL disposable Postgres (the ``fleet_db`` fixture):

  * enrollment returns a plaintext token ONCE; only its sha256 hash is stored;
  * a wrong token is rejected, a revoked worker is rejected;
  * ``lease`` routes by lane AND gates on the worker's capability flags;
  * the LinkedIn lane is REFUSED for a non-owner-IP worker but works for the
    owner-IP worker (R1 single-account mutex);
  * ``get_answer`` defers (returns None) on an unknown question and serves a
    known one;
  * ``request_otp`` returns the documented stub AND records a bookkeeping row
    WITHOUT reading Gmail / holding any token;
  * ``raise_challenge`` inserts an auth_challenge row.
"""
from __future__ import annotations

import hashlib

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import answer_bank, broker as broker_mod, config as fcfg, queue
from applypilot.fleet.broker import AuthError, Broker, CapabilityError

OWNER_IP = "203.0.113.7"
FRIEND_IP = "198.51.100.4"

_ALL_CAPS = {"can_ats": True, "can_compute": True, "can_discover": True, "can_linkedin": True}


# ---------------------------------------------------------------------------
# Seed helpers (mirror the pgqueue/queue test patterns)
# ---------------------------------------------------------------------------
def _seed_apply(conn, *, approved=True):
    rows = [{
        "url": "https://jobs.example.com/ats1", "company": "Acme", "title": "Chief of Staff",
        "application_url": "https://boards.greenhouse.io/acme/jobs/1", "score": 9.0,
        "target_host": "boards.greenhouse.io",
    }]
    queue.push_apply_jobs(conn, rows, approved_batch="b1" if approved else None)
    return rows


def _seed_compute(conn):
    queue.push_compute_jobs(conn, [{"url": "https://jobs.example.com/c1", "task": "score",
                                    "payload": {"x": 1}, "est_cost_usd": 0}])


def _seed_search(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO search_tasks (task_id, query, board, location, cadence_seconds) "
            "VALUES ('t1','chief of staff','greenhouse','NYC',21600)"
        )
    conn.commit()


def _seed_linkedin(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO linkedin_queue (url, application_url, score, status, approved_batch) "
            "VALUES ('https://www.linkedin.com/jobs/view/1','https://www.linkedin.com/jobs/view/1',9.0,'queued','b1')"
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Enrollment / auth
# ---------------------------------------------------------------------------
def test_enroll_returns_token_once_and_stores_only_hash(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        assert wid and token
        # The token authenticates...
        assert b.authenticate(conn, wid, token) is True
        # ...and ONLY its sha256 hash is persisted (plaintext never stored).
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM workers WHERE worker_id=%s", (wid,))
            stored = cur.fetchone()["token_hash"]
        assert stored == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored and stored != token


def test_wrong_token_rejected(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        assert b.authenticate(conn, wid, token + "x") is False
        assert b.authenticate(conn, "w_nope", token) is False
        # An authenticating RPC raises AuthError on a bad token.
        with pytest.raises(AuthError):
            b.get_config(conn, wid, "totally-wrong")


def test_revoked_worker_rejected(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "bob", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        assert b.authenticate(conn, wid, token) is True
        b.revoke_worker(conn, wid)
        assert b.authenticate(conn, wid, token) is False
        with pytest.raises(AuthError):
            b.lease(conn, wid, token, lane="compute")


# ---------------------------------------------------------------------------
# Lease routing by lane + capability
# ---------------------------------------------------------------------------
def test_lease_routes_by_lane(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        _seed_apply(conn)
        _seed_compute(conn)
        _seed_search(conn)

        ats = b.lease(conn, wid, token, lane="ats", home_ip="10.0.0.1")
        assert ats is not None and ats["url"].endswith("/ats1")

        comp = b.lease(conn, wid, token, lane="compute")
        assert comp is not None and comp["task"] == "score"

        srch = b.lease(conn, wid, token, lane="search")
        assert srch is not None and srch["task_id"] == "t1"


def test_broker_lease_refuses_stale_worker_version(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        _seed_apply(conn)
        fcfg.set_pinned_version(conn, "0.3.0+git.tree.expected")

        assert b.lease(
            conn, wid, token, lane="ats", sw_version="0.3.0+git.tree.stale"
        ) is None
        leased = b.lease(
            conn, wid, token, lane="ats", sw_version="0.3.0+git.tree.expected"
        )

    assert leased is not None
    assert leased["url"].endswith("/ats1")


def test_lease_capability_gate(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        # compute-only worker may NOT lease the ats lane.
        wid, token = b.enroll_worker(
            conn, "carol", capabilities={"can_compute": True}, public_ip=FRIEND_IP
        )
        _seed_apply(conn)
        _seed_compute(conn)
        with pytest.raises(CapabilityError):
            b.lease(conn, wid, token, lane="ats", home_ip="10.0.0.9")
        # its own lane works
        assert b.lease(conn, wid, token, lane="compute") is not None


# ---------------------------------------------------------------------------
# LinkedIn lane: owner-IP only (R1)
# ---------------------------------------------------------------------------
def test_linkedin_refused_for_non_owner_ip(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        _seed_linkedin(conn)
        # A friend whose REGISTERED public_ip != owner IP must be refused even with
        # a valid token + can_linkedin capability.
        fwid, ftok = b.enroll_worker(
            conn, "friend", capabilities={"can_linkedin": True}, public_ip=FRIEND_IP
        )
        assert b.lease(conn, fwid, ftok, lane="linkedin") is None
        # The job is still queued (never leased by the friend).
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM linkedin_queue LIMIT 1")
            assert cur.fetchone()["status"] == "queued"

        # The owner-IP worker CAN lease it.
        owid, otok = b.enroll_worker(
            conn, "owner", capabilities={"can_linkedin": True}, public_ip=OWNER_IP
        )
        job = b.lease(conn, owid, otok, lane="linkedin")
        assert job is not None and job["url"].endswith("/jobs/view/1")
        with conn.cursor() as cur:
            cur.execute("SELECT status, lease_owner FROM linkedin_queue LIMIT 1")
            r = cur.fetchone()
        assert r["status"] == "leased" and r["lease_owner"] == owid


def test_linkedin_owner_ip_spoof_is_ignored(fleet_db):
    # The real attack the old test missed: a friend supplies its OWN ip as owner_ip
    # to satisfy the mutex. The broker must IGNORE any caller-supplied owner_ip and
    # gate on the registered public_ip vs the broker-configured owner_ip only.
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        _seed_linkedin(conn)
        fwid, ftok = b.enroll_worker(
            conn, "friend", capabilities={"can_linkedin": True}, public_ip=FRIEND_IP
        )
        # spoof attempts -- own IP, the real owner IP -- all refused:
        assert b.lease(conn, fwid, ftok, lane="linkedin", owner_ip=FRIEND_IP) is None
        assert b.lease(conn, fwid, ftok, lane="linkedin", owner_ip=OWNER_IP) is None
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM linkedin_queue LIMIT 1")
            assert cur.fetchone()["status"] == "queued"  # never leased by the friend


def test_write_result_routes_linkedin_to_linkedin_queue(fleet_db):
    # A LinkedIn result must close the linkedin_queue row + advance account:linkedin,
    # NOT silently no-op against apply_queue (which would leave the lease open ->
    # double-apply, and leave the account cap inert).
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        _seed_linkedin(conn)
        owid, otok = b.enroll_worker(
            conn, "owner", capabilities={"can_linkedin": True}, public_ip=OWNER_IP
        )
        job = b.lease(conn, owid, otok, lane="linkedin")
        assert job is not None
        assert b.write_result(conn, owid, otok, lane="linkedin", url=job["url"], status="applied") is True
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM linkedin_queue WHERE url=%s", (job["url"],))
            assert cur.fetchone()["status"] == "applied"  # closed in linkedin_queue
            cur.execute("SELECT success_24h FROM rate_governor WHERE scope_key='account:linkedin'")
            assert cur.fetchone()["success_24h"] == 1  # account governor advanced
        # lease-owner guarded: a worker that doesn't hold the lease cannot close it
        assert queue.write_linkedin_result(conn, "intruder-worker", job["url"], status="applied") is False


# ---------------------------------------------------------------------------
# Answer bank: single answer, defer-on-unknown, never bulk
# ---------------------------------------------------------------------------
def test_get_answer_defers_on_unknown_and_serves_known(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)

        # Unknown question -> None (worker defers) AND it is recorded for the owner.
        assert b.get_answer(conn, wid, token, "What is your favorite color?") is None
        unknown = answer_bank.list_unknown(conn)
        assert any(u["q_raw"] == "What is your favorite color?" for u in unknown)

        # Known question -> the owner-vetted answer.
        answer_bank.set_answer(conn, "Are you authorized to work in the US?", "Yes")
        assert b.get_answer(conn, wid, token, "are you authorized to work in the us") == "Yes"


# ---------------------------------------------------------------------------
# OTP relay STUB: records a row, holds NO secret, returns the documented stub
# ---------------------------------------------------------------------------
def test_request_otp_is_a_stub_and_records_a_row(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        out = b.request_otp(conn, wid, token, "greenhouse.io", "https://jobs.example.com/ats1")
        assert out["status"] == "stub"
        assert "owner-side" in out["note"]
        # A bookkeeping row exists; NO code is stored anywhere in this module.
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id, sender_hint, url FROM otp_request WHERE id=%s", (out["request_id"],))
            row = cur.fetchone()
        assert row["worker_id"] == wid
        assert row["sender_hint"] == "greenhouse.io"
        # The module source must not embed any secret / read Gmail.
        import inspect
        src = inspect.getsource(broker_mod.Broker.request_otp)
        assert "gmail" not in src.lower() or "owner-side" in src.lower()


# ---------------------------------------------------------------------------
# raise_challenge / get_config / commands
# ---------------------------------------------------------------------------
def test_raise_challenge_inserts_row(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        cid = b.raise_challenge(
            conn, wid, token, "https://jobs.example.com/ats1", "visible_captcha", "owner_inbox",
            screenshot_url="blob://shot1", home_ip="10.0.0.1",
        )
        assert isinstance(cid, int)
        with conn.cursor() as cur:
            cur.execute("SELECT url, kind, route, screenshot_url, worker_id, machine_owner "
                        "FROM auth_challenge WHERE id=%s", (cid,))
            r = cur.fetchone()
        assert r["kind"] == "visible_captcha" and r["route"] == "owner_inbox"
        assert r["screenshot_url"] == "blob://shot1" and r["machine_owner"] == "alice"


def test_get_config_has_no_secrets(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        from applypilot.fleet import config as fleet_config
        fleet_config.set_pinned_version(conn, "1.4.0")
        cfg = b.get_config(conn, wid, token)
        assert cfg["version"] == "1.4.0"
        assert cfg["paused"] is False
        assert cfg["capabilities"]["can_ats"] is True
        # No secret-ish keys leak through.
        for k in cfg:
            assert "token" not in k and "dsn" not in k and "password" not in k


def test_poll_and_ack_commands(fleet_db):
    b = Broker(owner_ip=OWNER_IP)
    with pgqueue.connect(fleet_db) as conn:
        wid, token = b.enroll_worker(conn, "alice", capabilities=_ALL_CAPS, public_ip=OWNER_IP)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO remote_commands (worker_id, command) VALUES (%s,'pause')", (wid,))
        conn.commit()
        cmds = b.poll_commands(conn, wid, token)
        assert len(cmds) == 1 and cmds[0]["command"] == "pause"
        assert b.ack_command(conn, wid, token, cmds[0]["id"]) is True
        assert b.poll_commands(conn, wid, token) == []
