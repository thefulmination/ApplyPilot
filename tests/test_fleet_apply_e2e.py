"""Fleet apply lane A — canary go-live end-to-end test.

Headline property: the canary caps the fleet at EXACTLY K applications then
auto-pauses fleet_config.  This is the lane-level catastrophe proof (spec §4.3/§4.4).

Seed note (host-gap avoidance):
  Each row uses a DISTINCT apply_domain (acme0.com … acme4.com) so that the per-host
  min-gap governor (which stamps last_applied_at=now() on a confirmed apply and blocks
  the SAME host for ~90s) can never cap the run before the canary does.  Without
  distinct domains the second lease would be blocked by the host gap rather than the
  canary, making the assertion `applied == 2` incidentally true for the wrong reason.
  The point of the test is that the CANARY (not the host gap) is what caps at K.
"""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main as hm
from applypilot.fleet.worker import WorkerLoop


def test_canary_go_live_path(fleet_db):
    # ---- seed 5 approveable offsite rows (one distinct host each) ---------------
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        for i in range(5):
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, application_url, score, status, lane, apply_domain, dedup_key) "
                "VALUES (%s,%s,%s,'queued','ats',%s,%s)",
                (f"e{i}", f"http://acme{i}.com/{i}", 9.0 - i * 0.01,
                 f"acme{i}.com", f"dke{i}"),
            )
        conn.commit()

        # ---- arm canary K=2 and approve all queued rows -------------------------
        hm.set_canary(conn, 2)            # canary_enabled=TRUE, canary_remaining=2
        hm.approve(conn, all_pushed=True) # stamps approved_batch (canary must be armed)

    # ---- run 6 worker ticks with a stub apply_fn (more than the canary budget) --
    applied = 0
    for i in range(6):
        loop = WorkerLoop(
            lambda: pgqueue.connect(fleet_db),
            f"w{i}",
            home_ip="1.1.1.1",
            role="apply",
            apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.01},
        )
        if loop.run_once().get("action") == "applied":
            applied += 1

    # ---- canary capped the fleet at exactly K=2 ---------------------------------
    assert applied == 2, (
        f"expected exactly 2 applied (canary K=2) but got {applied}; "
        "check that the canary decrement+pause is atomic in queue.lease_apply"
    )

    # ---- ATS lane auto-stopped for operator review ------------------------------
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused, ats_apply_mode FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["ats_apply_mode"] == "stopped"
        assert row["paused"] is False
