"""Fleet LinkedIn lane B — canary-then-halt end-to-end test.

Two properties proved in one test:

1. CANARY CAPS AT EXACTLY 1
   Three rows are seeded, canary=1, all approved.  Three ticks run with a stub
   apply_fn that always returns 'applied'.  Exactly one tick succeeds; the other
   two get idle (canary_remaining hits 0 after the first lease).

2. WALL TICK SETS HALT → NEXT LEASE IS BLOCKED
   After the canary lands, re-arm the canary (set_linkedin_canary(conn, 2)) so
   there is capacity for a new lease, then run a tick whose apply_fn returns
   'captcha'.  The worker calls park_linkedin_challenge which:
     a) freezes the linkedin_queue row (apply_status='challenge_pending'), and
     b) sets rate_governor.halted_until = now() + halt_seconds.
   After the wall tick, a fresh approved row is inserted, the canary is re-armed
   again, and a THIRD tick runs — the lease must return None (idle) because
   halted_until is in the future.

Seeding notes:
  - DISTINCT dedup_key per row (dke0, dke1, …) so dedup never interferes.
  - rate_governor row inserted with min_gap_seconds=0 so the min-gap guard
    (last_applied_at < now() - gap) is trivially satisfied and the CANARY, not
    the gap, is what caps at 1.
  - daily_cap=20 — high enough not to interfere with the canary cap.
  - The APPLYPILOT_LINKEDIN_HALT_COOLDOWN env-var is unset in the test; the
    worker therefore uses its default of 21600s, which is in the future from
    Postgres's perspective.  That is what blocks the next lease.
"""
from __future__ import annotations

import datetime as _dt

from applypilot.apply import pgqueue
from applypilot.fleet import linkedin_home_main as hm
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop

_OWNER_IP = "1.1.1.1"


def _make_loop(fleet_db, worker_id: str, apply_fn):
    """Return a WorkerLoop wired to the test DB with a given apply_fn stub."""
    return WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        worker_id,
        home_ip=_OWNER_IP,
        role="linkedin",
        public_ip=_OWNER_IP,
        owner_ip=_OWNER_IP,
        apply_fn=apply_fn,
    )


def test_linkedin_canary_then_halt(fleet_db):
    # -----------------------------------------------------------------------
    # PART 1 — seed 3 rows, arm canary=1, approve all, run 3 ticks.
    # Exactly 1 must be 'applied'; the other 2 get idle.
    # -----------------------------------------------------------------------
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        for i in range(3):
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, application_url, score, status, lane, dedup_key) "
                "VALUES (%s,%s,%s,'queued','ats',%s)",
                (f"e{i}", f"https://linkedin.com/jobs/{i}", 9 - i * 0.01, f"dke{i}"),
            )
        # Seed the account governor with min_gap=0 so min-gap never caps first.
        cur.execute(
            "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds) "
            "VALUES ('account:linkedin', 20, 0)"
        )
        conn.commit()
        hm.set_linkedin_canary(conn, 1)       # arm canary at K=1
        hm.approve(conn, all_pushed=True)      # approve all 3 rows

    applied = 0
    for i in range(3):
        loop = _make_loop(fleet_db, f"w{i}", lambda job: {"run_status": "applied", "est_cost_usd": 0.0})
        if loop.run_once().get("action") == "applied":
            applied += 1

    assert applied == 1, (
        f"expected exactly 1 applied (canary K=1) but got {applied}; "
        "the LinkedIn canary decrement+block in lease_linkedin may not be atomic"
    )

    # Verify the canary remaining is now 0 (or NULL) — it was consumed.
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_remaining FROM fleet_config WHERE id=1")
        rem = cur.fetchone()["linkedin_canary_remaining"]
    assert rem is not None and rem <= 0, (
        f"linkedin_canary_remaining should be 0 after K=1 canary, got {rem!r}"
    )

    # -----------------------------------------------------------------------
    # PART 2 — wall tick: arm canary=2, run a tick with apply_fn='captcha'.
    # Assert halted_until IS set on account:linkedin after the tick.
    # Then assert that the NEXT lease attempt returns idle (blocked by halt).
    # -----------------------------------------------------------------------
    # Re-arm so the lease SQL can actually pick up a row (canary>0 is needed).
    # The row e0 is now 'applied' (applied_set entry written); rows e1, e2 are
    # still 'queued' and approved — the wall tick will lease one of them.
    with pgqueue.connect(fleet_db) as conn:
        hm.set_linkedin_canary(conn, 2)  # re-arm with headroom

    wall_action = _make_loop(fleet_db, "wall-worker",
                             lambda job: {"run_status": "captcha", "est_cost_usd": 0.0}).run_once()

    assert wall_action.get("action") == "parked_challenge", (
        f"expected parked_challenge from a captcha wall, got {wall_action!r}; "
        "check that _WALL_STATUSES in worker._tick_linkedin includes 'captcha'"
    )

    # halted_until must be set (in the future) on account:linkedin.
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'"
        )
        row = cur.fetchone()

    assert row is not None, "rate_governor row for account:linkedin missing after wall tick"
    halted_until = row["halted_until"]
    assert halted_until is not None, (
        "halted_until must be set on account:linkedin after a wall tick; "
        "check that park_linkedin_challenge sets it"
    )
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    # halted_until must be strictly in the future (the default cooldown is 6h).
    if hasattr(halted_until, "tzinfo") and halted_until.tzinfo is None:
        # naive datetime from psycopg — compare naively
        now_naive = _dt.datetime.utcnow()
        assert halted_until > now_naive, (
            f"halted_until {halted_until!r} is not in the future; halt was not set correctly"
        )
    else:
        assert halted_until > now_utc, (
            f"halted_until {halted_until!r} is not in the future; halt was not set correctly"
        )

    # -----------------------------------------------------------------------
    # PART 3 — insert a fresh row, re-arm canary, try to lease: must be idle.
    # The halt is the only blocker (min_gap=0, canary>0, fresh row approved).
    # -----------------------------------------------------------------------
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO linkedin_queue "
            "(url, application_url, score, status, lane, dedup_key) "
            "VALUES (%s,%s,%s,'queued','ats',%s)",
            ("e99", "https://linkedin.com/jobs/99", 9.0, "dke99"),
        )
        conn.commit()
        hm.set_linkedin_canary(conn, 5)       # plenty of canary headroom
        hm.approve(conn, all_pushed=True)      # approve the fresh row

    post_halt_result = _make_loop(
        fleet_db, "post-halt-worker",
        lambda job: {"run_status": "applied", "est_cost_usd": 0.0},
    ).run_once()

    assert post_halt_result.get("action") == "idle", (
        f"expected idle while halted_until is in the future, got {post_halt_result!r}; "
        "the halt guard in lease_linkedin may not be working"
    )
