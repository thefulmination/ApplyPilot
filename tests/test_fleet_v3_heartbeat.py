"""PG-backed tests for the fleet health substrate (heartbeat module, R7 / spec §11).

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied
+ truncated). Real SQL against real Postgres -- no mocks: stuck-detection, poison
quarantine, command fan-out (incl. broadcast '*') and the dashboard rollup are the
ones that gate the owner's safety/visibility, so they're exercised end-to-end.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import governor, heartbeat


# ---------------------------------------------------------------------------
# beat: insert + update (upsert)
# ---------------------------------------------------------------------------
def test_beat_upserts_and_updates(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "w1", machine_owner="jon", home_ip="1.2.3.4",
                       role="apply", state="idle", browser_count=2, sw_version="v1")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM worker_heartbeat")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT state, browser_count, machine_owner, last_beat "
                        "FROM worker_heartbeat WHERE worker_id='w1'")
            r1 = cur.fetchone()
        assert r1["state"] == "idle"
        assert r1["browser_count"] == 2
        assert r1["machine_owner"] == "jon"
        first_beat = r1["last_beat"]

        # second beat for the same worker UPDATES in place (no duplicate row),
        # advances last_beat, and changes mutable fields.
        heartbeat.beat(conn, "w1", state="applying", current_job="u/a",
                       success_today=3, spend_today_usd=0.42)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM worker_heartbeat")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT state, current_job, success_today, spend_today_usd, "
                        "machine_owner, last_beat FROM worker_heartbeat WHERE worker_id='w1'")
            r2 = cur.fetchone()
        assert r2["state"] == "applying"
        assert r2["current_job"] == "u/a"
        assert r2["success_today"] == 3
        assert float(r2["spend_today_usd"]) == 0.42
        # COALESCE-preserved identity field (machine_owner passed as None this beat)
        assert r2["machine_owner"] == "jon"
        assert r2["last_beat"] >= first_beat


# ---------------------------------------------------------------------------
# detect_stuck: old heartbeat AND long-running 'applying' worker
# ---------------------------------------------------------------------------
def test_detect_stuck_flags_dead_and_overrunning(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # healthy worker: fresh beat, idle -> never flagged
        heartbeat.beat(conn, "healthy", state="idle")
        # dead worker: fresh insert, then back-date last_beat
        heartbeat.beat(conn, "dead", state="idle")
        # overrunning worker: applying, job started long ago, fresh heartbeat
        heartbeat.beat(conn, "slow", state="applying", current_job="u/long")
        with conn.cursor() as cur:
            cur.execute("UPDATE worker_heartbeat SET last_beat = now() - make_interval(secs => 300) "
                        "WHERE worker_id='dead'")
            cur.execute("UPDATE worker_heartbeat SET job_started_at = now() - make_interval(secs => 1200) "
                        "WHERE worker_id='slow'")
        conn.commit()

        found = {(r["worker_id"], r["reason"]) for r in
                 heartbeat.detect_stuck(conn, heartbeat_timeout=90, job_max_seconds=600)}
    assert ("dead", "no_heartbeat") in found
    assert ("slow", "job_over_max") in found
    # the healthy worker is never flagged for either reason
    assert not any(wid == "healthy" for wid, _ in found)
    # 'slow' has a fresh heartbeat so it must NOT be flagged no_heartbeat
    assert ("slow", "no_heartbeat") not in found


def test_beat_keepalive_preserves_in_flight_job_started_at(fleet_db):
    # A keepalive on the SAME job that doesn't re-pass job_started_at must NOT clear
    # it -- else job_over_max stuck-detection is silently disabled for that job.
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "w1", state="applying", current_job="u/a")
        with conn.cursor() as cur:
            cur.execute("UPDATE worker_heartbeat SET job_started_at = now() - make_interval(secs => 1200) "
                        "WHERE worker_id='w1'")
        conn.commit()
        # keepalive: same job, state re-passed, job_started_at omitted
        heartbeat.beat(conn, "w1", state="applying", current_job="u/a")
        with conn.cursor() as cur:
            cur.execute("SELECT job_started_at FROM worker_heartbeat WHERE worker_id='w1'")
            assert cur.fetchone()["job_started_at"] is not None  # preserved
        # so the overrunning job is still detected
        assert "job_over_max" in {r["reason"] for r in heartbeat.detect_stuck(conn)
                                  if r["worker_id"] == "w1"}
        # but switching to a DIFFERENT job takes the new (here NULL) value
        heartbeat.beat(conn, "w1", state="applying", current_job="u/b")
        with conn.cursor() as cur:
            cur.execute("SELECT job_started_at FROM worker_heartbeat WHERE worker_id='w1'")
            assert cur.fetchone()["job_started_at"] is None


def test_detect_stuck_boundary(fleet_db):
    # Just-inside the window is NOT flagged; just-outside IS (pins the comparison).
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "inside", state="applying", current_job="u/i")
        heartbeat.beat(conn, "outside", state="applying", current_job="u/o")
        with conn.cursor() as cur:
            # heartbeat_timeout=90: 80s old -> inside (ok); 100s old -> outside (stale)
            cur.execute("UPDATE worker_heartbeat SET last_beat = now() - make_interval(secs => 80) "
                        "WHERE worker_id='inside'")
            cur.execute("UPDATE worker_heartbeat SET last_beat = now() - make_interval(secs => 100) "
                        "WHERE worker_id='outside'")
            # job_max=600: started 590s ago -> inside; 610s ago -> outside
            cur.execute("UPDATE worker_heartbeat SET job_started_at = now() - make_interval(secs => 590) "
                        "WHERE worker_id='inside'")
            cur.execute("UPDATE worker_heartbeat SET job_started_at = now() - make_interval(secs => 610) "
                        "WHERE worker_id='outside'")
        conn.commit()
        found = {(r["worker_id"], r["reason"]) for r in
                 heartbeat.detect_stuck(conn, heartbeat_timeout=90, job_max_seconds=600)}
    assert ("inside", "no_heartbeat") not in found and ("inside", "job_over_max") not in found
    assert ("outside", "no_heartbeat") in found and ("outside", "job_over_max") in found


def test_detect_stuck_can_flag_both_reasons(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "both", state="applying", current_job="u/x")
        with conn.cursor() as cur:
            cur.execute("UPDATE worker_heartbeat SET last_beat = now() - make_interval(secs => 300), "
                        "job_started_at = now() - make_interval(secs => 1200) WHERE worker_id='both'")
        conn.commit()
        reasons = {r["reason"] for r in heartbeat.detect_stuck(conn) if r["worker_id"] == "both"}
    assert reasons == {"no_heartbeat", "job_over_max"}


# ---------------------------------------------------------------------------
# quarantine_job: bumps crash_count, quarantines at threshold, idempotent
# ---------------------------------------------------------------------------
def test_quarantine_after_n_crashes(fleet_db):
    url = "https://jobs.example.com/poison"
    with pgqueue.connect(fleet_db) as conn:
        assert heartbeat.is_quarantined(conn, url) is False
        # strikes 1 and 2 (threshold=3) -> not yet quarantined
        assert heartbeat.quarantine_job(conn, url, worker="w1", reason="crash") is False
        assert heartbeat.quarantine_job(conn, url, worker="w2", reason="crash") is False
        assert heartbeat.is_quarantined(conn, url) is False
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count FROM poison_jobs WHERE url=%s", (url,))
            assert cur.fetchone()["crash_count"] == 2

        # strike 3 -> NEWLY quarantined (returns True exactly once)
        assert heartbeat.quarantine_job(conn, url, worker="w3", reason="timeout") is True
        assert heartbeat.is_quarantined(conn, url) is True

        # further strikes keep counting but do NOT re-report newly-quarantined
        assert heartbeat.quarantine_job(conn, url, worker="w4", reason="crash") is False
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count, quarantined_at FROM poison_jobs WHERE url=%s", (url,))
            r = cur.fetchone()
        assert r["crash_count"] == 4
        assert r["quarantined_at"] is not None


# ---------------------------------------------------------------------------
# issue / poll / ack commands -- incl. fleet-wide '*'
# ---------------------------------------------------------------------------
def test_command_lifecycle_incl_broadcast(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        cid_w1 = heartbeat.issue_command(conn, "w1", "restart")
        cid_all = heartbeat.issue_command(conn, "*", "self_update", target_version="v2")
        heartbeat.issue_command(conn, "w2", "pause")  # addressed to a different worker

        # w1 sees its own command AND the broadcast, but not w2's
        cmds = heartbeat.poll_commands(conn, "w1")
        got = {(c["command"], c["worker_id"]) for c in cmds}
        assert ("restart", "w1") in got
        assert ("self_update", "*") in got
        assert ("pause", "w2") not in got
        # target_version surfaced on the self_update command
        su = next(c for c in cmds if c["command"] == "self_update")
        assert su["target_version"] == "v2"

        # ack w1's own command -> it drops out; the broadcast remains until w1 acks it
        assert heartbeat.ack_command(conn, cid_w1, "w1") is True
        remaining = {c["id"] for c in heartbeat.poll_commands(conn, "w1")}
        assert cid_w1 not in remaining
        assert cid_all in remaining

        # w1 acking the broadcast removes it FOR w1; double-ack by w1 is a no-op
        assert heartbeat.ack_command(conn, cid_all, "w1") is True
        assert heartbeat.ack_command(conn, cid_all, "w1") is False
        assert heartbeat.poll_commands(conn, "w1") == []


def test_broadcast_reaches_every_worker_not_just_first_acker(fleet_db):
    # The real R7 invariant: a fleet-wide '*' command must be delivered to EVERY
    # worker. The old single-acked_at design let the first acker close it for all.
    with pgqueue.connect(fleet_db) as conn:
        cid = heartbeat.issue_command(conn, "*", "self_update", target_version="v2")
        assert any(c["id"] == cid for c in heartbeat.poll_commands(conn, "wA"))
        assert any(c["id"] == cid for c in heartbeat.poll_commands(conn, "wB"))
        # wA acks -> wA no longer sees it, but wB STILL does
        assert heartbeat.ack_command(conn, cid, "wA") is True
        assert not any(c["id"] == cid for c in heartbeat.poll_commands(conn, "wA"))
        assert any(c["id"] == cid for c in heartbeat.poll_commands(conn, "wB")), \
            "a broadcast must reach every worker, not be consumed by the first acker"
        # wB acks its own copy independently
        assert heartbeat.ack_command(conn, cid, "wB") is True
        assert not any(c["id"] == cid for c in heartbeat.poll_commands(conn, "wB"))


# ---------------------------------------------------------------------------
# dashboard_snapshot: returns all keys, populated
# ---------------------------------------------------------------------------
def test_dashboard_snapshot_has_all_keys(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # heartbeats
        heartbeat.beat(conn, "w1", state="applying", current_job="u/a")
        heartbeat.beat(conn, "w2", state="idle")
        # governor scopes (+ an outcome so challenge_rate / counters exist)
        governor.ensure_scope(conn, governor.GLOBAL)
        governor.ensure_scope(conn, governor.host_scope("boards.greenhouse.io"))
        governor.record_outcome(conn, [governor.host_scope("boards.greenhouse.io")],
                                "captcha")
        # queues: seed one apply job (queued) + a compute task
        pgqueue.push_jobs(conn, [{
            "url": "https://jobs.example.com/1", "company": "Acme", "title": "COS",
            "application_url": "https://boards.greenhouse.io/acme/jobs/1",
            "score": 8.0, "apply_domain": "boards.greenhouse.io",
        }])
        with conn.cursor() as cur:
            cur.execute("INSERT INTO compute_queue (url, task, status) "
                        "VALUES ('u/c', 'score', 'queued')")
            cur.execute("INSERT INTO search_tasks (task_id, query, board, status) "
                        "VALUES ('t1', 'analyst', 'indeed', 'queued')")
            # open captcha + a quarantined poison job + a spend row
            cur.execute("INSERT INTO auth_challenge (url, kind) VALUES ('u/cap', 'visible_captcha')")
            cur.execute("INSERT INTO poison_jobs (url, crash_count, quarantined_at) "
                        "VALUES ('u/q', 5, now())")
            cur.execute("INSERT INTO llm_usage (task, cost_usd, ts) VALUES ('score', 0.25, now())")
            cur.execute("INSERT INTO llm_usage (task, cost_usd, ts) "
                        "VALUES ('stale', 9.99, now() - make_interval(hours => 48))")
        conn.commit()

        snap = heartbeat.dashboard_snapshot(conn)

    # all documented keys present
    for k in ("machines", "governor", "queue_depth", "captcha_backlog",
              "quarantine", "spend_today"):
        assert k in snap, f"missing key {k}"

    assert len(snap["machines"]) == 2
    scopes = {g["scope_key"] for g in snap["governor"]}
    assert governor.host_scope("boards.greenhouse.io") in scopes
    assert "global" in scopes

    assert set(snap["queue_depth"].keys()) == {"apply", "compute", "search", "linkedin"}
    assert snap["queue_depth"]["apply"].get("queued") == 1
    assert snap["queue_depth"]["compute"].get("queued") == 1
    assert snap["queue_depth"]["search"].get("queued") == 1

    assert snap["captcha_backlog"] == 1
    assert snap["quarantine"] == 1
    # only the in-window spend ($0.25) counts; the 48h-old $9.99 is excluded
    assert snap["spend_today"] == pytest.approx(0.25)
