# Fleet Apply Lane A (Offsite-ATS Go-Live) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the distributed fleet to submit offsite ATS applications behind a fleet-wide canary auto-pause, owner-approval, halt, and double-apply-vs-home-history guards — all proven by tests.

**Architecture:** A new `applypilot-fleet-apply` worker injects an `apply_fn` (wrapping the proven `launcher.run_job`) into `WorkerLoop(role='apply')`, whose `_tick_apply` gains a status-passthrough branch (route off `run_job`'s verdict, never re-classify HTML). The `_LEASE_APPLY` SQL gains an atomic canary counter + a paused guard (mirroring `_LEASE_LINKEDIN`'s `FOR UPDATE`+reserve pattern). A home driver (`applypilot-fleet-apply-home`) does push(+`applied_set` backfill)/approve/canary/pull/resolve-challenge. The old v1 `lease_one` door is gated.

**Tech Stack:** Python 3.11+, psycopg 3 + Postgres (coordination), the proven `apply/launcher.py` apply path, tested against the disposable `applypilot-pgtest` Postgres via the `fleet_db` fixture.

## Global Constraints

- **Repo / cwd:** `C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`. Run python as `.conda-env/python.exe`. Test: `.conda-env/python.exe -m pytest <file> -q` (the `fleet_db` fixture spins up disposable PG).
- **Catastrophe-class:** every safety gate must be **proven by a test, not asserted**. The canary concurrency test (N>K workers → exactly K leases) and the `failed:no_result_line → crash_unconfirmed` (never phantom-applied) test are MANDATORY.
- **Regression baseline (must stay green every task that touches lease/worker):** `tests/test_fleet_v3_governor_queue.py` (apply-lease tests) + `tests/test_fleet_v3_worker.py` (apply-tick e2e) + `tests/test_fleet_pgqueue.py` (lease_one tests, if present). The four lease SQL constants (`_LEASE_APPLY/_COMPUTE/_SEARCH/_LINKEDIN`) are independent — a change to one cannot ripple to the others.
- **Apply gate stays authoritative:** the lane reads `COALESCE(audit_score, fit_score)`; research scores are not consulted. No tailoring (base-resume).
- **Commit discipline:** `git add <exact paths>` ONLY — NEVER `git add -A`. Do NOT push (the orchestrator pushes after the final whole-branch review). The 7 user-dirty files stay untouched. In-scope-and-expected edits to shared files: `fleet/queue.py`, `fleet/worker.py`, `apply/pgqueue.py`, `fleet/sync.py`, `fleet/schema_v3.sql`, `tests/conftest.py`.

## File Structure

- Modify `src/applypilot/fleet/schema_v3.sql` — add `fleet_config.canary_enabled` + `canary_remaining`.
- Modify `tests/conftest.py` — reset the new canary columns.
- Modify `src/applypilot/fleet/queue.py` — `_LEASE_APPLY` canary+paused CTEs.
- Modify `src/applypilot/fleet/worker.py` — `_tick_apply` status-passthrough branch + `_apply_status_passthrough` helper; fix docstring.
- Create `src/applypilot/fleet/apply_worker_main.py` — the `applypilot-fleet-apply` entrypoint + `apply_fn` + `should_halt` drive loop.
- Modify `src/applypilot/apply/pgqueue.py` — gate `_LEASE_SQL` (lease_one) with `approved_batch IS NOT NULL`.
- Modify `src/applypilot/fleet/sync.py` — `push_apply_eligible` `applied_set` backfill + `_PUSH_APPLY_SELECT` applications-ledger cross-check.
- Create `src/applypilot/fleet/apply_home_main.py` — the `applypilot-fleet-apply-home` driver.
- Modify `pyproject.toml` — register the two scripts.
- Create `tests/test_fleet_apply_lane.py`, `tests/test_fleet_apply_home.py`, `tests/test_fleet_apply_e2e.py`; add cases to `tests/test_fleet_v3_worker.py`.
- Create `docs/fleet-apply-lane-runbook.md`.

---

### Task 1: Canary schema columns + fixture reset

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `tests/conftest.py`
- Test: `tests/test_fleet_apply_lane.py`

**Interfaces:**
- Produces: `fleet_config.canary_enabled BOOLEAN NOT NULL DEFAULT FALSE`, `fleet_config.canary_remaining INTEGER` (NULL when disabled). The `fleet_db` fixture resets both.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_apply_lane.py
from applypilot.apply import pgqueue


def test_canary_columns_exist_and_default(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    assert row["canary_enabled"] is False
    assert row["canary_remaining"] is None
```

- [ ] **Step 2: Run it, expect FAIL** (`column "canary_enabled" does not exist`).

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_lane.py -q`

- [ ] **Step 3: Implement** — in `schema_v3.sql`, alongside the existing `ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS …` block (where `cost_cap_*`/`last_window_roll_at` were added), add:

```sql
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_enabled  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS canary_remaining INTEGER;
```

In `tests/conftest.py`, the `fleet_db` fixture's `UPDATE fleet_config SET …` (the one that resets `spend_cap_usd`, `paused`, `cost_cap_daily_usd`, `cost_cap_total_usd`, `last_window_roll_at`) — add `canary_enabled=FALSE, canary_remaining=NULL` to the SET list.

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql tests/conftest.py tests/test_fleet_apply_lane.py
git commit -m "feat(fleet): canary fleet_config columns + fixture reset"
```

---

### Task 2: Atomic canary auto-pause + paused guard in `_LEASE_APPLY`

**Files:**
- Modify: `src/applypilot/fleet/queue.py` (`_LEASE_APPLY`)
- Test: `tests/test_fleet_apply_lane.py`

**Interfaces:**
- Consumes: `fleet_config.canary_enabled/canary_remaining/paused` (Task 1 + existing).
- Produces: `lease_apply` now (a) returns None when `fleet_config.paused`; (b) when `canary_enabled`, leases at most `canary_remaining` jobs fleet-wide — atomically (serialized on the `fleet_config` row via `FOR UPDATE`), decrementing per lease and setting `paused=TRUE` at 0; (c) returns None when a `spend_cap_usd` is set and cumulative apply spend (`SUM(apply_queue.est_cost_usd)`) has reached it — a HARD lease guard (G5), not just the worker's soft `should_halt` belt. Decrement is at-lease, never on a no-op lease.

- [ ] **Step 1: Write the failing tests** (the catastrophe gate — concurrency is mandatory)

```python
import concurrent.futures as cf


def _seed_approved_apply_rows(conn, n, *, batch="b1"):
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                "VALUES (%s,%s,%s,'queued','ats',%s,%s,'acme.com')",
                (f"u{i}", f"http://acme.com/{i}", 9.0 - i*0.01, batch, f"dk{i}"))
        conn.commit()


def test_lease_blocked_when_paused(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 1)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None


def test_canary_caps_total_leases_fleetwide(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 5)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=2, paused=FALSE WHERE id=1")
        conn.commit()
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        b = queue.lease_apply(conn, "w2", home_ip="1.1.1.1")
        c = queue.lease_apply(conn, "w3", home_ip="1.1.1.1")
    assert a is not None and b is not None and c is None  # exactly 2 leases
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_remaining, paused FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["canary_remaining"] == 0 and row["paused"] is True


def test_canary_atomic_under_concurrency(fleet_db):
    # N concurrent workers, K=1 -> EXACTLY one lease succeeds (no overshoot).
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 8)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=1, paused=FALSE WHERE id=1")
        conn.commit()

    def _lease(i):
        with pgqueue.connect(fleet_db) as c:
            return queue.lease_apply(c, f"w{i}", home_ip="1.1.1.1") is not None

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_lease, range(8)))
    assert sum(results) == 1  # exactly one of eight workers leased


def test_canary_disabled_does_not_decrement(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 1)  # canary disabled by fixture default
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None
        with conn.cursor() as cur:
            cur.execute("SELECT canary_remaining FROM fleet_config WHERE id=1")
            assert cur.fetchone()["canary_remaining"] is None  # untouched


def test_lease_blocked_when_spend_cap_breached(fleet_db):
    # G5 as a HARD lease guard: cumulative apply spend >= spend_cap_usd -> no lease.
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_approved_apply_rows(conn, 1)
        cur.execute("UPDATE apply_queue SET est_cost_usd = 5.0 WHERE url='u0'")  # already-spent row
        # add a second leasable row so the SUM (5.0) is what blocks, not an empty queue
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                    "VALUES ('u1','http://acme.com/1','8','queued','ats','b1','dk1','acme.com')")
        cur.execute("UPDATE fleet_config SET spend_cap_usd = 1.0 WHERE id=1")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None  # 5.0 >= 1.0 cap
```

- [ ] **Step 2: Run them, expect FAIL** (canary not enforced; paused not enforced).

- [ ] **Step 3: Implement** — rewrite `_LEASE_APPLY` in `queue.py`. Add a `cfg` CTE that locks the `fleet_config` row `FOR UPDATE` (serializes all apply leases on it), guard `next_job` on `cfg.paused`/`cfg.canary_*`, and add a data-modifying `reserve` CTE (executes even though unreferenced — Postgres runs all WITH data-modifying CTEs to completion):

```python
_LEASE_APPLY = """
WITH cfg AS (SELECT canary_enabled, canary_remaining, paused, spend_cap_usd FROM fleet_config WHERE id=1 FOR UPDATE),
     home AS (SELECT count_24h, daily_cap, breaker_state FROM rate_governor WHERE scope_key = %(home_scope)s),
     glob AS (SELECT count_24h, daily_cap FROM rate_governor WHERE scope_key = 'global'),
     next_job AS (
       SELECT q.url
       FROM apply_queue q
       LEFT JOIN rate_governor g ON g.scope_key = 'host:' || COALESCE(q.target_host, q.apply_domain)
       LEFT JOIN home ON TRUE
       LEFT JOIN glob ON TRUE
       LEFT JOIN cfg ON TRUE
       WHERE q.status = 'queued' AND q.lane = 'ats' AND q.approved_batch IS NOT NULL
         AND NOT COALESCE(cfg.paused, FALSE)
         AND (NOT COALESCE(cfg.canary_enabled, FALSE) OR cfg.canary_remaining > 0)
         AND (COALESCE(cfg.spend_cap_usd, 0) <= 0
              OR (SELECT COALESCE(SUM(est_cost_usd), 0) FROM apply_queue) < cfg.spend_cap_usd)
         AND (glob.count_24h IS NULL OR glob.count_24h < glob.daily_cap)
         AND COALESCE(home.breaker_state, 'ok') NOT IN ('paused','demoted')
         AND (home.count_24h IS NULL OR home.count_24h < home.daily_cap)
         AND COALESCE(g.breaker_state, 'ok') NOT IN ('paused','demoted')
         AND COALESCE(g.count_24h, 0) < COALESCE(g.daily_cap, 2000000000)
         AND (g.last_applied_at IS NULL
              OR g.last_applied_at < now() - make_interval(secs => COALESCE(g.min_gap_seconds, 90) * (0.7 + random()*0.7)))
         AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)
       ORDER BY q.score DESC, q.url
       LIMIT 1
       FOR UPDATE OF q SKIP LOCKED
     ),
     reserve AS (
       UPDATE fleet_config
          SET canary_remaining = canary_remaining - 1,
              paused = (paused OR canary_remaining - 1 <= 0)
        WHERE id = 1 AND canary_enabled AND EXISTS (SELECT 1 FROM next_job)
       RETURNING 1
     )
UPDATE apply_queue q
SET status='leased', lease_owner=%(worker)s, lease_expires_at = now() + make_interval(secs => %(ttl)s),
    last_attempted_at = now(), attempts = q.attempts + 1, updated_at = now(), worker_home_ip = %(home_ip)s
FROM next_job WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url,
          COALESCE(q.target_host, q.apply_domain) AS target_host, q.score, q.dedup_key, q.attempts;
"""
```

Key correctness notes (keep in a code comment): the `cfg … FOR UPDATE` row-lock makes every apply lease serialize on the single `fleet_config` row (exactly like `_LEASE_LINKEDIN` locks `account:linkedin`); the `reserve` UPDATE only fires when a job is actually reserved (`EXISTS(next_job)`), so a no-op lease never decrements; `paused = (paused OR …)` only ever SETS paused (never clears it), so a cost-pause is preserved.

- [ ] **Step 4: Run the new tests + the regression baseline, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_lane.py tests/test_fleet_v3_governor_queue.py -q`
Expected: all pass (the existing apply-lease tests seed un-armed canary rows, so the new guards are transparent).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/queue.py tests/test_fleet_apply_lane.py
git commit -m "feat(fleet): atomic canary auto-pause + paused guard in apply lease"
```

---

### Task 3: `_tick_apply` status-passthrough (route off run_job's verdict, never re-classify)

**Files:**
- Modify: `src/applypilot/fleet/worker.py` (`_tick_apply` + new `_apply_status_passthrough`; module docstring)
- Test: `tests/test_fleet_v3_worker.py`

**Interfaces:**
- Consumes: `queue.write_apply_result(status=…)`, `_raise_and_park`.
- Produces: `_tick_apply` now accepts an `apply_fn` that returns a **dict** `{"run_status": <run_job status str>, "est_cost_usd": <float>}` (the new contract) and routes off it directly — applied→`write_apply_result('applied')`; captcha/login_issue/auth_required→park; `failed:no_result_line`/`failed:timeout`/`failed:worker_error*`→`write_apply_result('crash_unconfirmed')`; expired/other `failed:*`/skipped/dry_run→`write_apply_result('failed')`. **Back-compat:** an `apply_fn` returning a string/4-tuple (the existing test fakes) still goes through the old `captcha.classify` path unchanged.

- [ ] **Step 1: Write the failing test** (add to `tests/test_fleet_v3_worker.py`)

```python
def test_tick_apply_status_passthrough(fleet_db):
    # The new contract: apply_fn returns {"run_status": ...}. Prove crash != phantom-applied.
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.apply import pgqueue
    from applypilot.fleet import queue

    def _seed(conn, url):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                        "VALUES (%s,'http://acme.com/x','9','queued','ats','b1',%s,'acme.com')", (url, "dk-"+url))
        conn.commit()

    # applied -> applied + applied_set
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "ja")
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="apply",
                      apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.01})
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='ja'"); assert cur.fetchone()["status"] == "applied"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ja'"); assert cur.fetchone()["n"] == 1

    # failed:no_result_line -> crash_unconfirmed, NOT applied
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jc")
    loop2 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w2", home_ip="1.1.1.1", role="apply",
                       apply_fn=lambda job: {"run_status": "failed:no_result_line", "est_cost_usd": 0.0})
    assert loop2.run_once()["action"] == "crash_unconfirmed"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='jc'"); assert cur.fetchone()["status"] == "crash_unconfirmed"

    # captcha -> parked (auth_challenge raised, lease frozen)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jp")
    loop3 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w3", home_ip="1.1.1.1", role="apply",
                       apply_fn=lambda job: {"run_status": "captcha", "est_cost_usd": 0.0})
    assert loop3.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='jp' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1
```

- [ ] **Step 2: Run it, expect FAIL** (the dict is fed to `_normalize_apply_output` → mis-handled).

- [ ] **Step 3: Implement** — in `worker.py`, at the TOP of `_tick_apply`'s post-lease body (right after `out = self.apply_fn(job)`), branch on the new contract; add the helper. Keep the existing html-classify path below for the old contract.

```python
        out = self.apply_fn(job)
        if isinstance(out, dict) and "run_status" in out:
            return self._apply_status_passthrough(conn, url, target_host, out)
        # --- legacy html-classify path (existing test fakes return html/4-tuple) ---
        html, frames_text, final_url, status = _normalize_apply_output(out)
        # ... existing body unchanged ...
```

Add the helper (route off run_job's authoritative verdict; NEVER re-classify):

```python
    _WALL_STATUSES = ("captcha", "login_issue", "auth_required")
    _CRASH_STATUSES = ("failed:no_result_line", "failed:timeout")

    def _apply_status_passthrough(self, conn, url, target_host, res: dict) -> dict:
        """Route off run_job's terminal status (the agent already classified by SEEING
        the page). applied -> applied; a wall -> park; a ran-but-no-clean-result crash
        -> crash_unconfirmed (possibly submitted, never re-leased); else -> failed."""
        run_status = res.get("run_status") or ""
        cost = res.get("est_cost_usd", 0)
        if run_status == "applied":
            queue.write_apply_result(conn, self.worker_id, url, status="applied", apply_status="applied",
                                     target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost, outcome="success")
            self._beat(conn, state="idle")
            return {"action": "applied", "url": url}
        if run_status in self._WALL_STATUSES:
            kind = "login_gate" if run_status in ("login_issue", "auth_required") else "visible_captcha"
            route = _captcha.route_for(kind, on_owner_machine=self.on_owner_machine)
            wall_outcome = None if kind == "login_gate" else "captcha"
            self._raise_and_park(conn, url, kind, route=route, outcome=wall_outcome, target_host=target_host)
            return {"action": "parked_challenge", "url": url}
        if run_status in self._CRASH_STATUSES or run_status.startswith("failed:worker_error"):
            queue.write_apply_result(conn, self.worker_id, url, status="crash_unconfirmed",
                                     apply_status="crash_unconfirmed", apply_error=run_status[:200],
                                     target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost)
            self._beat(conn, state="idle")
            return {"action": "crash_unconfirmed", "url": url}
        queue.write_apply_result(conn, self.worker_id, url, status="failed", apply_status="failed",
                                 apply_error=(run_status or "unknown")[:200],
                                 target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost)
        self._beat(conn, state="idle")
        return {"action": "failed", "url": url}
```

Fix the module docstring (lines ~17/21): `apply_fn` wraps `launcher.run_job` (NOT `container_worker.run_job`), and in production returns `{"run_status", "est_cost_usd"}` — not html.

- [ ] **Step 4: Run the new test + the regression baseline, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_v3_worker.py -q`
Expected: all pass (the existing apply-tick tests return html → legacy path, unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/worker.py tests/test_fleet_v3_worker.py
git commit -m "feat(fleet): _tick_apply status-passthrough — route off run_job, never phantom-apply"
```

---

### Task 4: `applypilot-fleet-apply` entrypoint (env contract + apply_fn + should_halt loop)

**Files:**
- Create: `src/applypilot/fleet/apply_worker_main.py`
- Modify: `pyproject.toml`
- Test: `tests/test_fleet_apply_lane.py`

**Interfaces:**
- Consumes: `WorkerLoop(role='apply', apply_fn=…)`, `apply.pgqueue.should_halt`, `apply/launcher.run_job`, `apply/chrome.launch_chrome`+`cleanup_worker`, `container_worker._map_status` is NOT used (worker maps; see Task 3).
- Produces: `_setup_apply_env()` (ports container_worker._setup_env, home-box flavored), `make_apply_fn(model, agent)` returning `apply_fn(job) -> {"run_status", "est_cost_usd"}`, `build_apply_loop(dsn, worker_id, home_ip, …) -> WorkerLoop`, `run_apply(conn_factory, loop, cfg) -> None` (the should_halt drive loop), `main(argv)`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_apply_loop_wires_apply_fn(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DB_PATH", "x")  # _setup_apply_env may setdefault
    from applypilot.fleet import apply_worker_main as am
    loop = am.build_apply_loop(dsn=fleet_db, worker_id="w1", home_ip="1.1.1.1", model="sonnet", agent="claude")
    assert loop.role == "apply" and loop.apply_fn is not None


def test_apply_env_sets_base_resume():
    from applypilot.fleet import apply_worker_main as am
    am._setup_apply_env()
    import os
    assert os.environ.get("APPLYPILOT_BASE_RESUME") == "1"
    assert os.environ.get("APPLYPILOT_LANE_FILTER") == "0"


def test_run_apply_idles_when_halted(fleet_db):
    # should_halt True (paused) -> the loop does not lease; it returns after one idle pass.
    from applypilot.apply import pgqueue
    from applypilot.fleet import apply_worker_main as am
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.commit()
    ticks = am.run_apply(lambda: pgqueue.connect(fleet_db),
                         am.build_apply_loop(dsn=fleet_db, worker_id="w1", home_ip="1.1.1.1",
                                             model="sonnet", agent="claude"),
                         max_iterations=2, idle_sleep=0)
    assert ticks["halted"] >= 1 and ticks["applied"] == 0
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: apply_worker_main`).

- [ ] **Step 3: Implement** `apply_worker_main.py`. Critical: `_setup_apply_env()` runs BEFORE importing `applypilot.apply.launcher`.

```python
"""applypilot-fleet-apply: an OFFSITE apply worker for owner-controlled machines.
Wraps the proven launcher.run_job into an apply_fn and drives WorkerLoop(role='apply').
Respects fleet_config.paused via should_halt; never leases through a pause/canary-pause."""
from __future__ import annotations

import argparse
import os
import time


def _setup_apply_env() -> None:
    """Point config at writable locations + base-resume BEFORE importing applypilot
    (ports container_worker._setup_env, home-box flavored). run_job records agent cost
    to a home SQLite; the fleet has none, so sink it to a throwaway DB and read the REAL
    cost from launcher._last_run_stats."""
    os.environ["APPLYPILOT_BASE_RESUME"] = "1"
    os.environ["APPLYPILOT_LANE_FILTER"] = "0"
    os.environ.setdefault("APPLYPILOT_DB_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "fleet_apply_throwaway.db"))
    os.environ.setdefault("CHROME_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "chrome-workers"))
    os.environ.setdefault("APPLY_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "apply-workers"))
    os.environ.setdefault("APPLYPILOT_AGENT_TIMEOUT", "300")


def make_apply_fn(model: str, agent: str):
    """Return apply_fn(job) -> {"run_status", "est_cost_usd"} wrapping launcher.run_job.
    Imports launcher LAZILY (after _setup_apply_env)."""
    from applypilot.apply import launcher, chrome
    from applypilot.apply.container_worker import _real_cost

    def apply_fn(job: dict) -> dict:
        worker_id = 0
        port = chrome.launch_chrome(worker_id)  # real browser; not exercised in unit tests
        try:
            status, _dur = launcher.run_job(job, port, worker_id, model=model, agent=agent)
            stats = (getattr(launcher, "_last_run_stats", {}) or {}).get(worker_id, {})
            return {"run_status": status, "est_cost_usd": _real_cost(stats, model)}
        finally:
            try:
                chrome.cleanup_worker(worker_id)
            except Exception:
                pass
    return apply_fn


def build_apply_loop(*, dsn, worker_id, home_ip, model="sonnet", agent="claude", machine_owner=None):
    _setup_apply_env()
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import WorkerLoop
    return WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="apply",
                      apply_fn=make_apply_fn(model, agent), machine_owner=machine_owner)


def run_apply(conn_factory, loop, *, max_iterations=None, idle_sleep=5.0) -> dict:
    """Drive the apply loop. Before each iteration check should_halt (paused/spend cap)
    and idle when halted. A per-tick error backs off (no hot crash loop). Returns a
    counts dict (testable). Production calls with max_iterations=None (forever)."""
    from applypilot.apply import pgqueue
    counts = {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    it = 0
    while max_iterations is None or it < max_iterations:
        it += 1
        try:
            with conn_factory() as conn:
                if pgqueue.should_halt(conn):
                    counts["halted"] += 1
                    if idle_sleep:
                        time.sleep(idle_sleep)
                    continue
            res = loop.run_once()
            action = res.get("action")
            if action == "applied":
                counts["applied"] += 1
            elif action == "idle":
                counts["idle"] += 1
                if idle_sleep:
                    time.sleep(idle_sleep)
        except Exception:  # pragma: no cover - logged, backed off, never fatal
            counts["error"] += 1
            if idle_sleep:
                time.sleep(idle_sleep)
    return counts


def main(argv=None) -> int:  # pragma: no cover - long-running
    p = argparse.ArgumentParser(prog="applypilot-fleet-apply")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--home-ip", default=os.environ.get("FLEET_HOME_IP", "0.0.0.0"))
    p.add_argument("--model", default="sonnet")
    p.add_argument("--agent", default="claude")
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER"))
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    from applypilot.apply import pgqueue
    loop = build_apply_loop(dsn=args.dsn, worker_id=args.worker_id, home_ip=args.home_ip,
                            model=args.model, agent=args.agent, machine_owner=args.machine_owner)
    run_apply(lambda: pgqueue.connect(args.dsn), loop)
    return 0
```

> NOTE: confirm `apply/chrome.py` exposes `launch_chrome(worker_id) -> port` and `cleanup_worker(worker_id)` (container_worker uses them). If the names differ, match them; the unit tests never call `apply_fn`, so the wiring tests pass regardless, but the names must be right for production.

Register in `pyproject.toml [project.scripts]` (alongside the existing `applypilot-fleet-*`): `applypilot-fleet-apply = "applypilot.fleet.apply_worker_main:main"`.

- [ ] **Step 4: Run the tests + import + pyproject check, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_lane.py -q`
Run: `.conda-env/python.exe -c "import tomllib; s=tomllib.load(open('pyproject.toml','rb'))['project']['scripts']; print('applypilot-fleet-apply' in s)"` (expected: True)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/apply_worker_main.py pyproject.toml tests/test_fleet_apply_lane.py
git commit -m "feat(fleet): applypilot-fleet-apply entrypoint — apply_fn + should_halt loop"
```

---

### Task 5: Gate the v1 `lease_one` door

**Files:**
- Modify: `src/applypilot/apply/pgqueue.py` (`_LEASE_SQL`)
- Test: `tests/test_fleet_apply_lane.py`

**Interfaces:**
- Produces: `lease_one` now refuses to lease a row that is not `approved_batch IS NOT NULL` — closing the ungated second door into `apply_queue`.

- [ ] **Step 1: Write the failing test**

```python
def test_lease_one_requires_approval(fleet_db):
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('uone','http://x','9','queued','ats','x.com')")  # approved_batch NULL
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        assert pgqueue.lease_one(conn, "w1", politeness_seconds=0) is None  # not leasable: unapproved
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE apply_queue SET approved_batch='b1' WHERE url='uone'")
        conn.commit()
    with pgqueue.connect(fleet_db) as conn:
        assert pgqueue.lease_one(conn, "w1", politeness_seconds=0) is not None  # now leasable
```

- [ ] **Step 2: Run it, expect FAIL** (lease_one leases the unapproved row).

- [ ] **Step 3: Implement** — read `_LEASE_SQL` in `apply/pgqueue.py` (the SQL constant `lease_one` executes). In its inner row-selection `WHERE` (where it filters `status='queued'`), add `AND approved_batch IS NOT NULL`. (One added predicate; do not otherwise alter the SQL.)

- [ ] **Step 4: Run the new test + the pgqueue regression tests, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_lane.py tests/test_fleet_pgqueue.py -q`
> If existing pgqueue tests seed rows WITHOUT `approved_batch`, they will now correctly fail to lease. Those tests must be updated to stamp `approved_batch` on the rows they expect to lease — that is the intended behavior change (one gated door). Make that minimal test-seed fix and note it in the report.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/apply/pgqueue.py tests/test_fleet_apply_lane.py tests/test_fleet_pgqueue.py
git commit -m "feat(fleet): gate v1 lease_one with approved_batch (one gated door)"
```

---

### Task 6: Double-apply backfill + push cross-check (G6)

**Files:**
- Modify: `src/applypilot/fleet/sync.py` (`push_apply_eligible`, `_PUSH_APPLY_SELECT`)
- Test: `tests/test_fleet_apply_home.py`

**Interfaces:**
- Consumes: `_dedup.dedup_key(company, title)`, `queue.push_apply_jobs`.
- Produces: `push_apply_eligible` now also **backfills `applied_set`** (idempotent) from the home brain's already-applied history before/at push, AND `_PUSH_APPLY_SELECT` excludes jobs already in the `applications` ledger. Add a private `backfill_applied_set(sqlite_conn, pg_conn) -> int`.

- [ ] **Step 1: Write the failing test** (drive the backfill directly against seeded SQLite + PG)

```python
# tests/test_fleet_apply_home.py
import sqlite3
from applypilot.apply import pgqueue


def _home_sqlite(tmp_path):
    db = tmp_path / "home.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT, "
        "apply_status TEXT, apply_error TEXT, audit_score REAL, fit_score REAL, liveness_status TEXT, duplicate_of_url TEXT);"
        "CREATE TABLE applications (job_url TEXT, application_url TEXT, status TEXT);")
    return conn


def test_backfill_applied_set_from_home_history(fleet_db, tmp_path):
    from applypilot.fleet import sync
    sq = _home_sqlite(tmp_path)
    sq.execute("INSERT INTO jobs (url, company, title, apply_status) VALUES ('h1','Acme','COS','applied')")
    sq.execute("INSERT INTO applications (job_url, status) VALUES ('h2','applied')")  # ledger-only
    sq.execute("INSERT INTO jobs (url, company, title) VALUES ('h2','Beta','PM')")
    sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = sync.backfill_applied_set(sq, pg)
        with pg.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM applied_set")
            assert cur.fetchone()["n"] >= 1  # Acme|COS dedup_key landed
    assert n >= 1
    # idempotent second run
    with pgqueue.connect(fleet_db) as pg:
        assert sync.backfill_applied_set(sq, pg) >= 0
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: backfill_applied_set`).

- [ ] **Step 3: Implement** — in `sync.py`, add `backfill_applied_set` and call it from `push_apply_eligible` (after the push, or as a first step). Use the same `dedup_key(company, title)` the lease guard compares against.

```python
def backfill_applied_set(sqlite_conn, pg_conn) -> int:
    """Seed PG applied_set (the lease-time R9 dedup) from the home brain's apply history,
    so the fleet never re-applies a job already applied OUTSIDE the fleet. Idempotent."""
    rows = sqlite_conn.execute(
        "SELECT DISTINCT company, title FROM jobs "
        "WHERE apply_status = 'applied' OR apply_error IN ('no_confirmation','crash_unconfirmed') "
        "UNION "
        "SELECT DISTINCT j.company, j.title FROM applications a JOIN jobs j ON j.url = a.job_url "
        "WHERE a.status = 'applied'"
    ).fetchall()
    n = 0
    with pg_conn.cursor() as cur:
        for r in rows:
            dk = _dedup.dedup_key(r["company"], r["title"])
            if not dk:
                continue
            cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES (%s,%s) "
                        "ON CONFLICT (dedup_key) DO NOTHING", (dk, r["company"]))
            n += cur.rowcount
    pg_conn.commit()
    return n
```

In `_PUSH_APPLY_SELECT`, add the `applications`-ledger cross-check (mirroring home `acquire_job`) so already-applied jobs aren't pushed:

```sql
  AND COALESCE(application_url, url) NOT IN (
      SELECT COALESCE(NULLIF(application_url,''), job_url) FROM applications WHERE status = 'applied')
```

Wire `backfill_applied_set(sq, pg)` into `push_apply_eligible` (it already opens `sq`/`pg`).

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_home.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/sync.py tests/test_fleet_apply_home.py
git commit -m "feat(fleet): backfill applied_set from home apply-history + push ledger cross-check (anti double-apply)"
```

---

### Task 7: `applypilot-fleet-apply-home` driver

**Files:**
- Create: `src/applypilot/fleet/apply_home_main.py`
- Modify: `pyproject.toml`
- Test: `tests/test_fleet_apply_home.py`

**Interfaces:**
- Consumes: `sync.push_apply_eligible` (+ backfill), `queue.approve_jobs(conn, urls, batch)`, `sync.pull_apply_results`, `queue.resolve_challenge(conn, url, requeue=…)`.
- Produces: subcommands `push`, `approve [--all-pushed]`, `pull`, `canary K`, `lift-canary`, `challenges`, `resolve-challenge <url> [--skip]`, `status`. `approve` generates a batch token and **refuses if canary is not armed**.

- [ ] **Step 1: Write the failing test** (drive the helper functions directly — avoid argparse in tests)

```python
def test_apply_home_canary_and_approve_gate(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain) "
                    "VALUES ('q1','http://x','9','queued','ats','x.com')")  # unapproved
        conn.commit()
    # approve refuses when canary not armed
    with pgqueue.connect(fleet_db) as conn:
        try:
            hm.approve(conn, all_pushed=True)
            assert False, "approve must refuse when canary not armed"
        except SystemExit:
            pass
    # arm canary, then approve
    with pgqueue.connect(fleet_db) as conn:
        hm.set_canary(conn, 3)
        token = hm.approve(conn, all_pushed=True)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        assert cur.fetchone()["canary_remaining"] == 3
        cur.execute("SELECT approved_batch FROM apply_queue WHERE url='q1'")
        assert cur.fetchone()["approved_batch"] == token


def test_apply_home_resolve_challenge(fleet_db):
    from applypilot.fleet import apply_home_main as hm
    from applypilot.fleet import queue
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain, lease_owner) "
                    "VALUES ('p1','http://x','9','leased','ats','x.com','w1')")
        cur.execute("INSERT INTO auth_challenge (url, worker_id, kind, route) VALUES ('p1','w1','captcha','owner_inbox')")
        conn.commit()
        queue.park_challenge(conn, "w1", "p1")  # freeze (sets apply_status, 3650d lease)
        hm.resolve_challenge_cmd(conn, "p1", skip=False)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='p1'"); assert cur.fetchone()["status"] == "queued"
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url='p1'"); assert cur.fetchone()["resolved_at"] is not None
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: apply_home_main`).

- [ ] **Step 3: Implement** `apply_home_main.py`. Helper functions take a `conn` (testable); `main` wires argparse + opens conns.

```python
"""applypilot-fleet-apply-home: the owner driver for the offsite apply lane.
push (stage UNAPPROVED + backfill applied_set), approve (arm a batch; refuse unless the
canary is armed), pull, canary/lift-canary, challenges + resolve-challenge, status."""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import uuid

from applypilot.apply import pgqueue
from applypilot.fleet import queue, sync


def set_canary(conn, k: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=%s, paused=FALSE WHERE id=1", (k,))
    conn.commit()


def lift_canary(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL WHERE id=1")
    conn.commit()


def _canary_armed(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT canary_enabled FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    return bool(row and row["canary_enabled"])


def approve(conn, *, urls=None, all_pushed=False) -> str:
    """Stamp a fresh batch token on the given (or all queued-unapproved) rows. REFUSES
    unless the canary is armed (so the runbook's arm-then-approve order can't invert)."""
    if not _canary_armed(conn):
        raise SystemExit("refusing to approve: arm the canary first (apply-home canary <K>)")
    if all_pushed:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue WHERE status='queued' AND approved_batch IS NULL")
            urls = [r["url"] for r in cur.fetchall()]
    token = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    queue.approve_jobs(conn, urls or [], token)
    return token


def resolve_challenge_cmd(conn, url: str, *, skip: bool) -> bool:
    return queue.resolve_challenge(conn, url, requeue=not skip)


def list_challenges(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, worker_id, kind, route, raised_at FROM auth_challenge "
                    "WHERE resolved_at IS NULL ORDER BY raised_at DESC")
        return [dict(r) for r in cur.fetchall()]


def main(argv=None) -> int:  # pragma: no cover - CLI wiring
    p = argparse.ArgumentParser(prog="applypilot-fleet-apply-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("push"); sp.add_argument("--score-floor", type=int, default=7); sp.add_argument("--limit", type=int, default=None)
    sub.add_parser("pull")
    ca = sub.add_parser("canary"); ca.add_argument("k", type=int)
    sub.add_parser("lift-canary")
    ap = sub.add_parser("approve"); ap.add_argument("--all-pushed", action="store_true")
    sub.add_parser("challenges")
    rc = sub.add_parser("resolve-challenge"); rc.add_argument("url"); rc.add_argument("--skip", action="store_true")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    with pgqueue.connect(args.dsn) as conn:
        if args.cmd == "push":
            print("pushed", sync.push_apply_eligible(pg_conn=conn, score_floor=args.score_floor, limit=args.limit))
        elif args.cmd == "pull":
            print("pulled", sync.pull_apply_results(pg_conn=conn))
        elif args.cmd == "canary":
            set_canary(conn, args.k); print("canary armed", args.k)
        elif args.cmd == "lift-canary":
            lift_canary(conn); print("canary lifted")
        elif args.cmd == "approve":
            print("approved batch", approve(conn, all_pushed=args.all_pushed))
        elif args.cmd == "challenges":
            for c in list_challenges(conn): print(c)
        elif args.cmd == "resolve-challenge":
            print("resolved", resolve_challenge_cmd(conn, args.url, skip=args.skip))
        elif args.cmd == "status":
            _print_status(conn)
    return 0


def _print_status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) AS n FROM apply_queue GROUP BY status")
        depth = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT paused, canary_enabled, canary_remaining, spend_cap_usd FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(est_cost_usd),0) AS s FROM apply_queue")
        spend = float(cur.fetchone()["s"])
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE resolved_at IS NULL")
        open_ch = cur.fetchone()["n"]
    print({"queue": depth, "paused": cfg["paused"], "canary_remaining": cfg["canary_remaining"],
           "spend_cap_usd": float(cfg["spend_cap_usd"] or 0), "apply_spend": spend, "open_challenges": open_ch})
```

> NOTE: confirm `sync.push_apply_eligible`/`pull_apply_results` accept `pg_conn=` and open their own SQLite (they do per the verified signatures). If `push_apply_eligible` needs the sqlite_conn for the backfill, it opens `_home_conn()` itself.

Register `applypilot-fleet-apply-home = "applypilot.fleet.apply_home_main:main"` in `pyproject.toml`.

- [ ] **Step 4: Run the tests + pyproject check, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_apply_home.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/apply_home_main.py pyproject.toml tests/test_fleet_apply_home.py
git commit -m "feat(fleet): applypilot-fleet-apply-home driver (push/approve/canary/pull/resolve-challenge)"
```

---

### Task 8: End-to-end + full suite + runbook

**Files:**
- Create: `tests/test_fleet_apply_e2e.py`
- Create: `docs/fleet-apply-lane-runbook.md`

**Interfaces:**
- Consumes everything above.

- [ ] **Step 1: Write the e2e test** — the canary go-live path against one seeded PG with a stub apply_fn.

```python
# tests/test_fleet_apply_e2e.py
from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main as hm
from applypilot.fleet.worker import WorkerLoop


def test_canary_go_live_path(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        for i in range(5):
            cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain, dedup_key) "
                        "VALUES (%s,%s,%s,'queued','ats','acme.com',%s)",
                        (f"e{i}", f"http://acme.com/{i}", 9.0 - i*0.01, f"dke{i}"))
        conn.commit()
        hm.set_canary(conn, 2)           # arm K=2
        hm.approve(conn, all_pushed=True)  # approve all (gated by canary)

    applied = 0
    for i in range(6):  # more iterations than the canary budget
        loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), f"w{i}", home_ip="1.1.1.1", role="apply",
                          apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.01})
        if loop.run_once().get("action") == "applied":
            applied += 1
    assert applied == 2  # canary capped the fleet at exactly K
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is True  # auto-paused for review
```

- [ ] **Step 2: Run it; iterate until green.**

- [ ] **Step 3: Run the FULL fleet suite (the gate).**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_codex_bridge.py tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py -q`
Expected: all pass (prior 186 + the new apply-lane tests; 0 failures). Capture exact counts.

- [ ] **Step 4: Write the runbook** `docs/fleet-apply-lane-runbook.md` (verbatim from spec §7): the v1-fleet-off precondition, watchdog-running requirement, and the ordered steps `pull → push → canary K → approve --all-pushed → start applypilot-fleet-apply → it applies ≤K then auto-pauses → pull + review (+ challenges/resolve-challenge) → canary N or lift-canary + set spend_cap_usd`. Document the residuals (aggregator, approved_batch presence-stamp, per-destination block).

- [ ] **Step 5: Commit** (do NOT push)

```bash
git add tests/test_fleet_apply_e2e.py docs/fleet-apply-lane-runbook.md
git commit -m "test(fleet): apply lane A canary-go-live e2e + runbook"
```

---

## Self-Review

**Spec coverage:** §3.1 entrypoint+contract → Task 3 (worker) + Task 4 (entrypoint). §3.2 home driver → Task 7. §3.3 v1 isolation → Task 5. §4.2 approval gate → built + Task 7 driver. §4.3 canary → Task 1 (schema) + Task 2 (atomic lease) + Task 7 (arm/lift) + Task 8 (e2e). §4.4 paused → Task 2 (lease guard) + Task 4 (should_halt loop). §4.5 cost cap (G5) → **HARD lease guard in Task 2** (`spend_cap_usd` in the locked `cfg` CTE; `SUM(apply_queue.est_cost_usd) < spend_cap_usd`) **plus** the worker's `should_halt` belt in Task 4 — enforced at the lease like the canary/paused guards, not operator discipline. The owner sets the cap via `set_spend_cap`. §4.6 G6 backfill + push cross-check → Task 6. §3.2 resolve-challenge → Task 7. §6 testing/regression baseline → every task names it; Task 8 runs the full suite. §7 runbook → Task 8.

**Placeholder scan:** none — every step has complete code. Three `NOTE:` callouts (Task 4 chrome fn names; Task 5 pre-existing pgqueue test-seed fix; Task 7 sync conn kwargs) are verification instructions, not placeholders.

**Type consistency:** the apply_fn contract `{"run_status": str, "est_cost_usd": float}` is identical in Task 3 (worker consumes), Task 4 (entrypoint produces), and Task 8 (e2e stub). `queue.write_apply_result(status=…)` signature matches across Task 3. `set_canary`/`lift_canary`/`approve`/`resolve_challenge_cmd` names match between Task 7 impl and its tests + the Task 8 e2e. The `_LEASE_APPLY` canary columns (`canary_enabled`/`canary_remaining`) match Task 1's schema. The `should_halt` cost gate (over `apply_queue.est_cost_usd`) is consistent with §4.5's decision (NOT the llm_usage `_cost_cap_exceeded`).

**One integration note:** Task 2's `cfg … FOR UPDATE` serializes ALL apply leases on the single `fleet_config` row (even canary-disabled) — the same posture the proven `_LEASE_LINKEDIN` takes on `account:linkedin`. At apply rates (gap-jittered ≥90s/host) the contention is negligible and the safety (no canary overshoot) is the point; the plan accepts this. The four lease SQL constants are independent, so this change cannot affect compute/search/linkedin.
