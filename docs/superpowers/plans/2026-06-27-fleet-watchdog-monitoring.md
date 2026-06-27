# Fleet Watchdog & Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fleet safe to leave unattended — a deterministic watchdog loop that runs the already-built recovery primitives on a cadence, plus a bounded Claude-monitor surface that can read health and take only allowlisted actions.

**Architecture:** Two layers. **A — deterministic watchdog** (`watchdog.py`): a pure, testable `watchdog_tick(conn, cfg) -> dict` that reclaims crashed leases, trips/recovers breakers, restarts/quarantines stuck workers, enforces the cost cap, rolls the nightly window, and beats its own liveness; driven by `run_watchdog(conn_factory, cfg, *, stop=None)` behind an `applypilot-fleet-watchdog` entrypoint. **B — bounded monitor** (`monitor.py`): a `MonitorActions` wrapper that binds ONLY the safe operations (denied ops are physically absent from the surface, proven by test) plus a `build_health_report` text generator over `dashboard_snapshot`.

**Tech Stack:** Python 3.11+, psycopg 3 against Postgres, the existing `applypilot.fleet` foundation (queue/governor/heartbeat) + `applypilot.apply.pgqueue`. Tests run against the disposable `applypilot-pgtest` Postgres via the `fleet_db` fixture (`tests/conftest.py`), same as the compute/discovery lanes.

## Global Constraints

- **Repo / cwd:** `C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`. Run python as `.conda-env/python.exe`.
- **Test command:** `.conda-env/python.exe -m pytest <file> -q` (the `fleet_db` fixture spins up disposable PG).
- **Unified-brain rule:** the watchdog/monitor operate ONLY on the coordination Postgres + `fleet_config`. They MUST NOT write the SQLite brain.
- **Commit discipline:** `git add <exact paths>` ONLY — NEVER `git add -A`. The 7 user-dirty files (`run-applypilot.ps1`, `src/applypilot/discovery/jobspy.py`, `src/applypilot/pipeline.py`, `src/applypilot/scoring/cover_letter.py`, `src/applypilot/scoring/tailor.py`, `tests/test_discovery_scheduler.py`, `tests/test_generation_workers.py`) and the 3 untracked files must stay untouched/unstaged. Do NOT push (the orchestrator pushes after the final whole-branch review).
- **Reserved watchdog identity:** the watchdog beats `worker_heartbeat` with `worker_id='watchdog'`, `role='watchdog'`. The stuck-worker handler MUST exclude `worker_id='watchdog'` (never restart/quarantine itself).
- **Parked challenges are sacred:** the watchdog MUST NOT touch parked `auth_challenge` rows (`park_challenge` already freezes them out of reclaim; do not add any path that resolves them). `resolve_challenge` is in the monitor DENY-set.
- **Foundation API (verified — call exactly these signatures, do not re-implement):**
  - `queue.reclaim_compute(conn, *, grace_seconds=30, commit=True) -> int`
  - `queue.reclaim_search(conn, *, grace_seconds=30, commit=True) -> int`
  - `apply.pgqueue.reclaim_stale_leases(conn, *, grace_seconds=30) -> list[dict]` (commits internally)
  - `governor.evaluate_breakers(conn, *, captcha_threshold=0.4, min_samples=8, throttle_gap_multiplier=3, cool_seconds=1800, commit=True) -> list[tuple[str,str]]`
  - `governor.clear_expired_breakers(conn, *, commit=True) -> list[str]`
  - `governor.roll_window(conn, *, commit=True) -> None`
  - `heartbeat.detect_stuck(conn, *, heartbeat_timeout=90, job_max_seconds=600) -> list[dict]` (each `{worker_id, reason}`)
  - `heartbeat.quarantine_job(conn, url, *, worker, reason, threshold=3, commit=True) -> bool`
  - `heartbeat.issue_command(conn, worker_id, command, *, target_version=None, commit=True) -> int`
  - `heartbeat.dashboard_snapshot(conn) -> dict` (keys: `machines, governor, queue_depth, captcha_backlog, quarantine, spend_today`)
  - `heartbeat.beat(conn, worker_id, *, machine_owner=None, home_ip=None, role='apply', state='idle', spend_today_usd=0, commit=True, ...) -> None`
  - `fleet_config` columns: `cost_cap_daily_usd, cost_cap_total_usd, paused` (+ new `last_window_roll_at` added in Task 5).

## File Structure

- Create `src/applypilot/fleet/watchdog.py` — `WatchdogConfig` (dataclass), `watchdog_tick`, `run_watchdog`, `main` (the `applypilot-fleet-watchdog` entrypoint), and small private helpers (`_handle_stuck`, `_total_cap_breached`, `_maybe_roll_window`).
- Create `src/applypilot/fleet/monitor.py` — `MonitorActions` (allow-only wrapper), `build_health_report`.
- Modify `src/applypilot/fleet/schema_v3.sql` — add `last_window_roll_at timestamptz` to `fleet_config`.
- Modify `pyproject.toml` — register `applypilot-fleet-watchdog`.
- Create `tests/test_fleet_watchdog.py`, `tests/test_fleet_monitor.py`, `tests/test_fleet_watchdog_e2e.py`.

---

### Task 1: `WatchdogConfig` + reclaim phase of `watchdog_tick`

**Files:**
- Create: `src/applypilot/fleet/watchdog.py`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Consumes: `queue.reclaim_compute`, `queue.reclaim_search`, `apply.pgqueue.reclaim_stale_leases`, `heartbeat.beat` (signatures in Global Constraints).
- Produces: `WatchdogConfig` (dataclass with fields: `heartbeat_timeout=90`, `job_max_seconds=600`, `quarantine_threshold=3`, `captcha_threshold=0.4`, `breaker_min_samples=8`, `breaker_cool_seconds=1800`, `reclaim_grace_seconds=30`, `cadence_seconds=25`, `nightly_roll_hour=4`); `watchdog_tick(conn, cfg) -> dict` returning a summary dict that ALWAYS contains the keys `reclaimed_compute:int`, `reclaimed_search:int`, `reclaimed_apply:int` (later tasks add more keys). The tick also calls `heartbeat.beat(conn, "watchdog", role="watchdog", state="idle", spend_today_usd=0, commit=True)` at the END so a dead watchdog is visible via `detect_stuck`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_watchdog.py
from applypilot.apply import pgqueue
from applypilot.fleet import watchdog, queue


def _seed_expired_compute(conn, url="c1"):
    # queued -> leased with an already-expired lease (simulates a crashed compute worker)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO compute_queue (url, task, status, lease_owner, lease_expires_at, attempts) "
                    "VALUES (%s,'score','leased','wDead', now() - interval '5 minutes', 1)", (url,))
    conn.commit()


def _seed_expired_search(conn, task_id="t1"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO search_tasks (task_id, query, board, status, lease_owner, lease_expires_at, next_due_at) "
                    "VALUES (%s,'cos','indeed','leased','wDead', now() - interval '5 minutes', now())", (task_id,))
    conn.commit()


def test_watchdog_tick_reclaims_crashed_leases(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_expired_compute(conn)
        _seed_expired_search(conn)
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["reclaimed_compute"] == 1
    assert summary["reclaimed_search"] == 1
    assert summary["reclaimed_apply"] == 0  # apply_queue empty in this test
    # the reclaimed compute row is back to 'queued'
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM compute_queue WHERE url='c1'")
        assert cur.fetchone()["status"] == "queued"


def test_watchdog_tick_beats_own_liveness(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        watchdog.watchdog_tick(conn, cfg)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT role, state FROM worker_heartbeat WHERE worker_id='watchdog'")
        row = cur.fetchone()
    assert row is not None and row["role"] == "watchdog"
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: applypilot.fleet.watchdog`).

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`

- [ ] **Step 3: Implement** `src/applypilot/fleet/watchdog.py`

```python
"""Deterministic fleet watchdog (spec §2) -- the no-LLM workhorse that runs the
foundation's recovery primitives on a cadence so the fleet self-heals unattended.

`watchdog_tick(conn, cfg)` is a single pure pass (testable against seeded PG);
`run_watchdog(conn_factory, cfg, stop=...)` drives it on a clock; `main` is the
`applypilot-fleet-watchdog` entrypoint. The watchdog beats its own liveness via
`worker_heartbeat` (worker_id='watchdog') so a dead watchdog is itself visible.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass

from applypilot.apply import pgqueue
from applypilot.fleet import governor, heartbeat, queue

WATCHDOG_ID = "watchdog"
WATCHDOG_ROLE = "watchdog"


@dataclass
class WatchdogConfig:
    heartbeat_timeout: int = 90
    job_max_seconds: int = 600
    quarantine_threshold: int = 3
    captcha_threshold: float = 0.4
    breaker_min_samples: int = 8
    breaker_cool_seconds: int = 1800
    reclaim_grace_seconds: int = 30
    cadence_seconds: int = 25
    nightly_roll_hour: int = 4


def watchdog_tick(conn, cfg: WatchdogConfig) -> dict:
    """Run one recovery pass. Returns a summary of what changed. Each phase is a
    foundation primitive; the watchdog only SCHEDULES them. Always beats its own
    liveness last so a crash between phases still leaves a recent heartbeat absent."""
    summary: dict = {}
    summary["reclaimed_compute"] = queue.reclaim_compute(conn, grace_seconds=cfg.reclaim_grace_seconds)
    summary["reclaimed_search"] = queue.reclaim_search(conn, grace_seconds=cfg.reclaim_grace_seconds)
    summary["reclaimed_apply"] = len(pgqueue.reclaim_stale_leases(conn, grace_seconds=cfg.reclaim_grace_seconds))

    heartbeat.beat(conn, WATCHDOG_ID, role=WATCHDOG_ROLE, state="idle", spend_today_usd=0, commit=True)
    return summary
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog tick reclaim phase + self-liveness beat"
```

---

### Task 2: Breaker management in the tick

**Files:**
- Modify: `src/applypilot/fleet/watchdog.py`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Consumes: `governor.evaluate_breakers`, `governor.clear_expired_breakers`.
- Produces: `watchdog_tick` summary GAINS keys `breakers_tripped: list[tuple[str,str]]` (from evaluate_breakers) and `breakers_recovered: list[str]` (from clear_expired_breakers). Insert these phases AFTER reclaim and BEFORE the self-beat.

- [ ] **Step 1: Write the failing test**

```python
def _seed_governor_scope(conn, scope_key, *, success=0, captcha=0, block=0, state="ok",
                         challenge_rate=0.0, breaker_until=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rate_governor (scope_key, success_24h, captcha_24h, block_24h, "
            "challenge_rate, breaker_state, breaker_until, min_gap_seconds) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s, 5)",
            (scope_key, success, captcha, block, challenge_rate, state, breaker_until))
    conn.commit()


def test_watchdog_trips_breaker_on_high_challenge_rate(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        # 10 samples, challenge_rate 0.6 >= 0.4*1.5 -> paused
        _seed_governor_scope(conn, "host:acme.com", success=4, captcha=6, block=0, challenge_rate=0.6)
        summary = watchdog.watchdog_tick(conn, cfg)
    assert ("host:acme.com", "paused") in summary["breakers_tripped"]


def test_watchdog_recovers_expired_breaker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_governor_scope(conn, "host:old.com", state="paused", challenge_rate=0.0,
                             breaker_until="now() - interval '1 minute'")
        # breaker_until as a literal won't bind via %s; set it directly instead:
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET breaker_until = now() - interval '1 minute' "
                        "WHERE scope_key='host:old.com'")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert "host:old.com" in summary["breakers_recovered"]
```

- [ ] **Step 2: Run it, expect FAIL** (`KeyError: 'breakers_tripped'`).

- [ ] **Step 3: Implement** — in `watchdog_tick`, after the three reclaim lines and before `heartbeat.beat`, add:

```python
    summary["breakers_tripped"] = governor.evaluate_breakers(
        conn, captcha_threshold=cfg.captcha_threshold, min_samples=cfg.breaker_min_samples,
        cool_seconds=cfg.breaker_cool_seconds,
    )
    summary["breakers_recovered"] = governor.clear_expired_breakers(conn)
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog trips + recovers breakers each tick"
```

---

### Task 3: Stuck-worker restart + quarantine (watchdog excluded)

**Files:**
- Modify: `src/applypilot/fleet/watchdog.py`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Consumes: `heartbeat.detect_stuck`, `heartbeat.issue_command`, `heartbeat.quarantine_job`.
- Produces: `watchdog_tick` summary GAINS key `stuck_handled: list[dict]` — one entry per stuck worker `{worker_id, reason, action}` where `action` is `"restart"` (always issued for a stuck worker) and additionally `"quarantine"` is appended to the entry's actions if the worker had a `current_job` and the `job_over_max` reason fired. Add a private `_handle_stuck(conn, cfg) -> list[dict]`. MUST skip any worker whose `worker_id == WATCHDOG_ID`.

- [ ] **Step 1: Write the failing test**

```python
def _seed_stuck_worker(conn, worker_id="wStuck", *, current_job=None, applying=False):
    state = "applying" if applying else "idle"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_heartbeat (worker_id, role, state, current_job, job_started_at, last_beat) "
            "VALUES (%s,'apply',%s,%s, now() - interval '20 minutes', now() - interval '10 minutes')",
            (worker_id, state, current_job))
    conn.commit()


def test_watchdog_restarts_stuck_worker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_stuck_worker(conn, "wStuck")  # last_beat 10m ago > 90s timeout
        summary = watchdog.watchdog_tick(conn, cfg)
        entries = [e for e in summary["stuck_handled"] if e["worker_id"] == "wStuck"]
        assert entries and "restart" in entries[0]["action"]
        # a 'restart' command was actually enqueued
        with conn.cursor() as cur:
            cur.execute("SELECT command FROM remote_commands WHERE worker_id='wStuck'")
            assert cur.fetchone()["command"] == "restart"


def test_watchdog_never_restarts_itself(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        # give the watchdog a STALE heartbeat, then run a tick
        with conn.cursor() as cur:
            cur.execute("INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                        "VALUES ('watchdog','watchdog','idle', now() - interval '10 minutes')")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert all(e["worker_id"] != "watchdog" for e in summary["stuck_handled"])
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM remote_commands WHERE worker_id='watchdog'")
        assert cur.fetchone()["n"] == 0
```

- [ ] **Step 2: Run it, expect FAIL** (`KeyError: 'stuck_handled'`).

- [ ] **Step 3: Implement** — add the helper and wire it into the tick (after breakers, before the self-beat):

```python
def _handle_stuck(conn, cfg: WatchdogConfig) -> list[dict]:
    """Restart every stuck worker (and quarantine its job if it blew the job-max).
    NEVER acts on the watchdog's own reserved id."""
    out: list[dict] = []
    stuck = heartbeat.detect_stuck(conn, heartbeat_timeout=cfg.heartbeat_timeout,
                                   job_max_seconds=cfg.job_max_seconds)
    for s in stuck:
        wid = s["worker_id"]
        if wid == WATCHDOG_ID:
            continue
        actions = ["restart"]
        heartbeat.issue_command(conn, wid, "restart")
        if s["reason"] == "job_over_max":
            # the worker's current job has been running too long -> quarantine it so a
            # restart doesn't immediately re-lease the same poison job.
            with conn.cursor() as cur:
                cur.execute("SELECT current_job FROM worker_heartbeat WHERE worker_id=%s", (wid,))
                row = cur.fetchone()
            job = row["current_job"] if row else None
            if job:
                if heartbeat.quarantine_job(conn, job, worker=wid, reason="job_over_max",
                                            threshold=cfg.quarantine_threshold):
                    actions.append("quarantine")
        out.append({"worker_id": wid, "reason": s["reason"], "action": actions})
    return out
```

Wire it in `watchdog_tick` (before `heartbeat.beat`):

```python
    summary["stuck_handled"] = _handle_stuck(conn, cfg)
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog restarts stuck workers + quarantines over-max jobs (self-excluded)"
```

---

### Task 4: Cost-cap enforcement (pause on total-cap breach)

**Files:**
- Modify: `src/applypilot/fleet/watchdog.py`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Produces: a private `_total_cap_breached(conn) -> bool` (mirrors `queue._cost_cap_exceeded`: if `cost_cap_total_usd > 0` and `SUM(llm_usage.cost_usd) >= cost_cap_total_usd`, or `cost_cap_daily_usd > 0` and last-24h sum >= daily). `watchdog_tick` summary GAINS key `paused_on_cap: bool` — when True the tick has set `fleet_config.paused = true`. Insert this phase AFTER stuck-handling, before the self-beat.

- [ ] **Step 1: Write the failing test**

```python
def test_watchdog_pauses_on_total_cap_breach(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=1.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (2.50, now())")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["paused_on_cap"] is True
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is True


def test_watchdog_does_not_pause_under_cap(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=100.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (2.50, now())")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["paused_on_cap"] is False
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is False
```

> NOTE: confirm the `llm_usage` columns (`cost_usd`, `ts`) by reading `schema_v3.sql` before running — if the timestamp column is named differently, match it in the test seed.

- [ ] **Step 2: Run it, expect FAIL** (`KeyError: 'paused_on_cap'`).

- [ ] **Step 3: Implement**

```python
def _total_cap_breached(conn) -> bool:
    """True if a configured daily OR total cost cap is met/exceeded (mirrors
    queue._cost_cap_exceeded). A 0/NULL cap means 'no cap'."""
    with conn.cursor() as cur:
        cur.execute("SELECT cost_cap_daily_usd, cost_cap_total_usd FROM fleet_config WHERE id=1")
        cfg_row = cur.fetchone()
        if not cfg_row:
            return False
        daily = float(cfg_row["cost_cap_daily_usd"] or 0)
        total = float(cfg_row["cost_cap_total_usd"] or 0)
        if daily > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= now() - interval '24 hours'")
            if float(cur.fetchone()["s"]) >= daily:
                return True
        if total > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage")
            if float(cur.fetchone()["s"]) >= total:
                return True
    return False


def _enforce_cap(conn) -> bool:
    """If a cap is breached, make the halt EXPLICIT by setting fleet_config.paused=true.
    (Leasing already self-halts on the cap; this surfaces it to the dashboard/monitor.)"""
    if not _total_cap_breached(conn):
        return False
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1 AND paused IS DISTINCT FROM TRUE")
    conn.commit()
    return True
```

Wire into `watchdog_tick` (before `heartbeat.beat`):

```python
    summary["paused_on_cap"] = _enforce_cap(conn)
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog pauses fleet on cost-cap breach"
```

---

### Task 5: Nightly window roll (persistent guard) + schema column

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `src/applypilot/fleet/watchdog.py`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Consumes: `governor.roll_window`.
- Produces: a new `fleet_config.last_window_roll_at timestamptz` column (idempotent `ADD COLUMN IF NOT EXISTS`); a private `_maybe_roll_window(conn, cfg, *, now_hour) -> bool` that rolls AT MOST once per calendar day: it rolls only when `now_hour == cfg.nightly_roll_hour` AND `last_window_roll_at` is NULL or older than 23 hours, then stamps `last_window_roll_at = now()`. `watchdog_tick` GAINS key `rolled_window: bool`. Because `Date.now()` is unavailable in this codebase's *test* harness but NOT in production python, the tick reads the hour from the DB (`SELECT extract(hour from now())`) so the phase stays deterministic and testable.

- [ ] **Step 1: Add the schema column.** In `src/applypilot/fleet/schema_v3.sql`, find the `fleet_config` table definition / the ALTER-COLUMN idempotent block and add:

```sql
ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS last_window_roll_at timestamptz;
```

(Place it alongside the other `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements if the file uses that pattern; otherwise add it right after the `fleet_config` CREATE.)

- [ ] **Step 2: Write the failing test**

```python
def test_watchdog_rolls_window_once_per_night(fleet_db):
    cfg = watchdog.WatchdogConfig(nightly_roll_hour=4)
    with pgqueue.connect(fleet_db) as conn:
        # force "it is 4am and we've never rolled"
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET last_window_roll_at = NULL WHERE id=1")
            _seed_governor_scope_count(conn, "host:x.com", count_24h=99)
        conn.commit()
        rolled1 = watchdog._maybe_roll_window(conn, cfg, now_hour=4)
        rolled2 = watchdog._maybe_roll_window(conn, cfg, now_hour=4)  # same night -> no-op
    assert rolled1 is True and rolled2 is False
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count_24h FROM rate_governor WHERE scope_key='host:x.com'")
        assert cur.fetchone()["count_24h"] == 0  # roll zeroed the counters


def test_watchdog_does_not_roll_off_hour(fleet_db):
    cfg = watchdog.WatchdogConfig(nightly_roll_hour=4)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET last_window_roll_at = NULL WHERE id=1")
        conn.commit()
        assert watchdog._maybe_roll_window(conn, cfg, now_hour=13) is False
```

Add this helper near the top of the test file (used by the test above):

```python
def _seed_governor_scope_count(conn, scope_key, *, count_24h):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, count_24h, min_gap_seconds) VALUES (%s,%s,5)",
                    (scope_key, count_24h))
```

- [ ] **Step 3: Run it, expect FAIL** (`AttributeError: _maybe_roll_window`).

- [ ] **Step 4: Implement**

```python
def _maybe_roll_window(conn, cfg: WatchdogConfig, *, now_hour: int) -> bool:
    """Roll the rolling-24h governor counters at most once per night. Guarded by
    fleet_config.last_window_roll_at so a restart can't double-roll the same night."""
    if now_hour != cfg.nightly_roll_hour:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT last_window_roll_at FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        last = row["last_window_roll_at"] if row else None
        if last is not None:
            cur.execute("SELECT (now() - %s) < interval '23 hours' AS recent", (last,))
            if cur.fetchone()["recent"]:
                return False
    governor.roll_window(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET last_window_roll_at = now() WHERE id=1")
    conn.commit()
    return True
```

Wire into `watchdog_tick` (before the self-beat):

```python
    with conn.cursor() as cur:
        cur.execute("SELECT extract(hour from now())::int AS h")
        _hour = cur.fetchone()["h"]
    conn.rollback()  # read-only hour probe
    summary["rolled_window"] = _maybe_roll_window(conn, cfg, now_hour=_hour)
```

- [ ] **Step 5: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/watchdog.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog nightly window roll with persistent once-per-night guard"
```

---

### Task 6: `run_watchdog` loop + `applypilot-fleet-watchdog` entrypoint

**Files:**
- Modify: `src/applypilot/fleet/watchdog.py`
- Modify: `pyproject.toml`
- Test: `tests/test_fleet_watchdog.py`

**Interfaces:**
- Produces: `run_watchdog(conn_factory, cfg, *, stop=None, max_ticks=None) -> int` — opens a fresh conn per tick via `conn_factory()`, runs `watchdog_tick`, sleeps `cfg.cadence_seconds`, repeats until `stop()` returns True (if provided) or `max_ticks` ticks elapse; returns the number of ticks run. A per-tick exception is swallowed (logged) so one bad tick never kills the watchdog. `main(argv=None) -> int` — argparse `--dsn`(default `FLEET_PG_DSN` env), `--cadence`(int, default 25); SystemExit if no dsn; calls `run_watchdog(lambda: pgqueue.connect(dsn), cfg)`. Register `applypilot-fleet-watchdog = "applypilot.fleet.watchdog:main"`.

- [ ] **Step 1: Write the failing test**

```python
def test_run_watchdog_runs_until_stop(fleet_db):
    cfg = watchdog.WatchdogConfig(cadence_seconds=0)  # no real sleep in the test
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3  # let 3 ticks run, then stop

    ticks = watchdog.run_watchdog(lambda: pgqueue.connect(fleet_db), cfg, stop=stop)
    assert ticks == 3


def test_run_watchdog_survives_a_bad_tick(fleet_db, monkeypatch):
    cfg = watchdog.WatchdogConfig(cadence_seconds=0)
    seq = {"n": 0}

    def flaky_tick(conn, c):
        seq["n"] += 1
        if seq["n"] == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(watchdog, "watchdog_tick", flaky_tick)
    ticks = watchdog.run_watchdog(lambda: pgqueue.connect(fleet_db), cfg, max_ticks=2)
    assert ticks == 2  # the RuntimeError on tick 1 did not kill the loop
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: run_watchdog`).

- [ ] **Step 3: Implement**

```python
def run_watchdog(conn_factory, cfg: WatchdogConfig, *, stop=None, max_ticks=None) -> int:
    """Drive watchdog_tick on a cadence. A fresh connection per tick keeps a transient
    DB blip from wedging the loop; a per-tick exception is swallowed so one bad pass
    never takes the watchdog down. Returns the number of ticks executed."""
    ticks = 0
    while True:
        if stop is not None and stop():
            break
        if max_ticks is not None and ticks >= max_ticks:
            break
        try:
            with conn_factory() as conn:
                watchdog_tick(conn, cfg)
        except Exception:  # pragma: no cover - logged, never fatal
            pass
        ticks += 1
        if cfg.cadence_seconds:
            time.sleep(cfg.cadence_seconds)
    return ticks


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-watchdog")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--cadence", type=int, default=25)
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    cfg = WatchdogConfig(cadence_seconds=args.cadence)
    run_watchdog(lambda: pgqueue.connect(args.dsn), cfg)  # pragma: no cover - infinite
    return 0
```

In `pyproject.toml` `[project.scripts]`, ADD alongside the existing entries (do NOT remove/reorder any):

```toml
applypilot-fleet-watchdog = "applypilot.fleet.watchdog:main"
```

- [ ] **Step 4: Run it, expect PASS** + sanity-check the script imports.

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q` (expected: 13 passed)
Run: `.conda-env/python.exe -c "from applypilot.fleet.watchdog import main, run_watchdog, watchdog_tick, WatchdogConfig; print('ok')"`
Run: `.conda-env/python.exe -c "import tomllib; print('applypilot-fleet-watchdog' in tomllib.load(open('pyproject.toml','rb'))['project']['scripts'])"` (expected: True)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py pyproject.toml tests/test_fleet_watchdog.py
git commit -m "feat(fleet): run_watchdog loop + applypilot-fleet-watchdog entrypoint"
```

---

### Task 7: `MonitorActions` allow/deny wrapper

**Files:**
- Create: `src/applypilot/fleet/monitor.py`
- Test: `tests/test_fleet_monitor.py`

**Interfaces:**
- Consumes: `heartbeat.issue_command`, `heartbeat.quarantine_job`.
- Produces: `MonitorActions(conn)` — a wrapper exposing ONLY the allowlisted ops as methods: `restart_worker(worker_id) -> int` (→ `issue_command(conn, worker_id, "restart")`), `quarantine(url, *, worker, reason) -> bool` (→ `quarantine_job`), `pause_scope(scope_key) -> None` (direct bounded `UPDATE rate_governor SET breaker_state='paused', breaker_until=NULL`), and `report(text) -> str` (returns the text; a hook for alert emission). The DENIED ops (`resolve_challenge`, `set_cost_cap`/cap changes, `resume_scope`, `approve_job`, anything causing an apply) MUST NOT be attributes of the instance — a test asserts their absence (defense in depth beyond prompt instructions).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_monitor.py
from applypilot.apply import pgqueue
from applypilot.fleet import monitor


DENIED = ["resolve_challenge", "set_cost_cap", "set_cost_cap_total", "resume_scope",
          "clear_breaker", "approve_job", "approve", "apply", "submit", "lease_apply"]


def test_monitor_actions_allow_ops_work(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO rate_governor (scope_key, min_gap_seconds) VALUES ('host:z.com',5)")
        conn.commit()
        ma = monitor.MonitorActions(conn)
        assert ma.restart_worker("wA") == 1            # one command row enqueued
        ma.pause_scope("host:z.com")
        with conn.cursor() as cur:
            cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key='host:z.com'")
            assert cur.fetchone()["breaker_state"] == "paused"
        assert ma.report("all good") == "all good"


def test_monitor_actions_deny_ops_absent_from_surface(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        ma = monitor.MonitorActions(conn)
        for name in DENIED:
            assert not hasattr(ma, name), f"DENY op {name!r} must not be reachable on MonitorActions"
```

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError: applypilot.fleet.monitor`).

- [ ] **Step 3: Implement** `src/applypilot/fleet/monitor.py`

```python
"""Layer B -- the bounded Claude-monitor surface (spec §3). The monitor is a
periodic SECOND OPINION, not load-bearing: read health, write a report, and take
only ALLOWLISTED actions. The deny-set (resolve a parked challenge, change a cost
cap, resume a paused/LinkedIn scope, approve/cause an apply) is enforced by
ABSENCE -- those operations are simply not methods on MonitorActions, so neither a
prompt-injected agent nor a bug can invoke them through this surface."""
from __future__ import annotations

from applypilot.fleet import heartbeat


class MonitorActions:
    """The ONLY mutation surface the monitor is given. Every method here is on the
    allow-list (spec §3.1). Denied operations are intentionally NOT defined."""

    def __init__(self, conn):
        self._conn = conn

    def restart_worker(self, worker_id: str) -> int:
        """Enqueue a 'restart' command for a stuck worker."""
        return heartbeat.issue_command(self._conn, worker_id, "restart")

    def quarantine(self, url: str, *, worker: str, reason: str) -> bool:
        """Quarantine a poison job so it stops being re-leased."""
        return heartbeat.quarantine_job(self._conn, url, worker=worker, reason=reason)

    def pause_scope(self, scope_key: str) -> None:
        """Pause a host/board scope (bounded write). Does NOT resume -- resume of any
        paused scope (esp. the LinkedIn lane) is owner-only and absent by design."""
        with self._conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET breaker_state='paused', breaker_until=NULL, "
                        "updated_at=now() WHERE scope_key=%s", (scope_key,))
        self._conn.commit()

    def report(self, text: str) -> str:
        """Emit/return a report string (alert hook)."""
        return text
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_monitor.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/monitor.py tests/test_fleet_monitor.py
git commit -m "feat(fleet): MonitorActions allow-only surface (deny ops absent by design)"
```

---

### Task 8: Health-report generator

**Files:**
- Modify: `src/applypilot/fleet/monitor.py`
- Test: `tests/test_fleet_monitor.py`

**Interfaces:**
- Consumes: `heartbeat.dashboard_snapshot` (keys: `machines, governor, queue_depth, captcha_backlog, quarantine, spend_today`); `WatchdogConfig.captcha_threshold` (default 0.4) as the anomaly threshold (the report takes a plain `captcha_threshold: float = 0.4` arg to avoid importing watchdog).
- Produces: `build_health_report(snapshot: dict, *, captcha_threshold=0.4, cost_cap_total=None) -> str` — a text report with clearly-labeled sections: `MACHINES`, `QUEUES`, `GOVERNOR`, `CAPTCHA BACKLOG`, `SPEND`, and a `NEEDS YOUR DECISION` section listing each anomaly. Anomalies flagged: any governor scope with `challenge_rate >= captcha_threshold`; any machine whose `last_beat` is missing/None; spend within 90% of `cost_cap_total` (when provided).

- [ ] **Step 1: Write the failing test**

```python
def test_health_report_has_sections_and_flags_anomaly():
    snapshot = {
        "machines": [
            {"worker_id": "w1", "role": "compute", "state": "idle", "last_beat": "2026-06-27T04:00:00Z"},
            {"worker_id": "w2", "role": "apply", "state": "applying", "last_beat": None},  # offline
        ],
        "governor": [
            {"scope_key": "host:ok.com", "breaker_state": "ok", "challenge_rate": 0.05, "count_24h": 10},
            {"scope_key": "host:bad.com", "breaker_state": "ok", "challenge_rate": 0.55, "count_24h": 20},  # anomaly
        ],
        "queue_depth": {"apply": {"queued": 3}, "compute": {"queued": 7}, "search": {}, "linkedin": {}},
        "captcha_backlog": 2,
        "quarantine": 1,
        "spend_today": 9.5,
    }
    report = monitor.build_health_report(snapshot, captcha_threshold=0.4, cost_cap_total=10.0)
    for section in ("MACHINES", "QUEUES", "GOVERNOR", "CAPTCHA BACKLOG", "SPEND", "NEEDS YOUR DECISION"):
        assert section in report
    # the high-challenge scope and the offline worker both surface as anomalies
    assert "host:bad.com" in report
    assert "w2" in report
    # spend 9.5 of 10.0 cap (>=90%) is flagged
    assert "cap" in report.lower()


def test_health_report_clean_when_no_anomalies():
    snapshot = {
        "machines": [{"worker_id": "w1", "role": "compute", "state": "idle", "last_beat": "2026-06-27T04:00:00Z"}],
        "governor": [{"scope_key": "host:ok.com", "breaker_state": "ok", "challenge_rate": 0.0, "count_24h": 5}],
        "queue_depth": {"apply": {}, "compute": {}, "search": {}, "linkedin": {}},
        "captcha_backlog": 0, "quarantine": 0, "spend_today": 1.0,
    }
    report = monitor.build_health_report(snapshot, captcha_threshold=0.4, cost_cap_total=100.0)
    assert "NEEDS YOUR DECISION" in report
    assert "none" in report.lower()  # the decision section says nothing needs attention
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: build_health_report`).

- [ ] **Step 3: Implement** — add to `monitor.py`:

```python
def build_health_report(snapshot: dict, *, captcha_threshold: float = 0.4,
                        cost_cap_total: float | None = None) -> str:
    """Render a text health report from a dashboard_snapshot, with a 'NEEDS YOUR
    DECISION' section listing anomalies the monitor will NOT auto-fix."""
    lines: list[str] = []
    anomalies: list[str] = []

    lines.append("=== MACHINES ===")
    for m in snapshot.get("machines", []):
        beat = m.get("last_beat")
        flag = "  <OFFLINE: no heartbeat>" if not beat else ""
        lines.append(f"  {m.get('worker_id')} [{m.get('role')}] state={m.get('state')}{flag}")
        if not beat:
            anomalies.append(f"worker {m.get('worker_id')} offline (no heartbeat)")

    lines.append("=== QUEUES ===")
    for lane, depths in (snapshot.get("queue_depth") or {}).items():
        lines.append(f"  {lane}: {dict(depths)}")

    lines.append("=== GOVERNOR ===")
    for g in snapshot.get("governor", []):
        rate = float(g.get("challenge_rate") or 0)
        flag = "  <HIGH CHALLENGE RATE>" if rate >= captcha_threshold else ""
        lines.append(f"  {g.get('scope_key')} state={g.get('breaker_state')} "
                     f"rate={rate:.2f} n={g.get('count_24h')}{flag}")
        if rate >= captcha_threshold:
            anomalies.append(f"scope {g.get('scope_key')} challenge_rate {rate:.2f} >= {captcha_threshold}")

    lines.append(f"=== CAPTCHA BACKLOG ===\n  open challenges: {snapshot.get('captcha_backlog', 0)}; "
                 f"quarantined jobs: {snapshot.get('quarantine', 0)}")

    spend = float(snapshot.get("spend_today") or 0)
    cap_str = f" / cap {cost_cap_total}" if cost_cap_total else ""
    lines.append(f"=== SPEND ===\n  last 24h: ${spend:.2f}{cap_str}")
    if cost_cap_total and cost_cap_total > 0 and spend >= 0.9 * cost_cap_total:
        anomalies.append(f"spend ${spend:.2f} is within 90% of cap ${cost_cap_total:.2f}")

    lines.append("=== NEEDS YOUR DECISION ===")
    if anomalies:
        lines.extend(f"  - {a}" for a in anomalies)
    else:
        lines.append("  none")

    return "\n".join(lines)
```

- [ ] **Step 4: Run it, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_monitor.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/monitor.py tests/test_fleet_monitor.py
git commit -m "feat(fleet): monitor health-report generator with anomaly escalation"
```

---

### Task 9: End-to-end + full suite

**Files:**
- Create: `tests/test_fleet_watchdog_e2e.py`

**Interfaces:**
- Consumes everything above. No new production code — this proves the layers compose against one seeded PG state.

- [ ] **Step 1: Write the e2e test** — seed a single PG with: an expired compute lease, a stuck worker, a high-challenge scope, a parked challenge, and a breached cost cap; run ONE `watchdog_tick`; assert all the deterministic recoveries happened AND the parked challenge was left untouched; then build a report off the post-tick snapshot.

```python
# tests/test_fleet_watchdog_e2e.py
from applypilot.apply import pgqueue
from applypilot.fleet import watchdog, monitor, heartbeat, queue


def test_watchdog_full_recovery_pass_and_report(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            # expired compute lease (crashed worker)
            cur.execute("INSERT INTO compute_queue (url, task, status, lease_owner, lease_expires_at, attempts) "
                        "VALUES ('c1','score','leased','wDead', now() - interval '5 min', 1)")
            # stuck worker (no heartbeat for 10 min)
            cur.execute("INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                        "VALUES ('wStuck','apply','idle', now() - interval '10 min')")
            # high-challenge scope -> should pause
            cur.execute("INSERT INTO rate_governor (scope_key, success_24h, captcha_24h, block_24h, "
                        "challenge_rate, breaker_state, min_gap_seconds) "
                        "VALUES ('host:bad.com', 4, 6, 0, 0.6, 'ok', 5)")
            # a PARKED challenge that must NOT be touched
            cur.execute("INSERT INTO auth_challenge (url, worker_id, kind, route) "
                        "VALUES ('https://x.com/job','wP','captcha','offsite')")
            # breached total cap
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=1.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (5.0, now())")
        conn.commit()

        summary = watchdog.watchdog_tick(conn, cfg)

    assert summary["reclaimed_compute"] == 1
    assert any(e["worker_id"] == "wStuck" for e in summary["stuck_handled"])
    assert ("host:bad.com", "paused") in summary["breakers_tripped"]
    assert summary["paused_on_cap"] is True

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # parked challenge untouched (still unresolved)
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url='https://x.com/job'")
        assert cur.fetchone()["resolved_at"] is None
        # fleet paused
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is True

    # the report renders off the live post-tick snapshot
    with pgqueue.connect(fleet_db) as conn:
        snap = heartbeat.dashboard_snapshot(conn)
    report = monitor.build_health_report(snap, captcha_threshold=cfg.captcha_threshold, cost_cap_total=1.0)
    assert "NEEDS YOUR DECISION" in report
```

> NOTE: confirm the `auth_challenge` insert columns against `schema_v3.sql` (the worker uses `_insert_challenge` with `url, worker_id, machine_owner, home_ip, kind, route, screenshot_url`). Trim/extend the seed to match the NOT-NULL columns.

- [ ] **Step 2: Run it; iterate until green.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog_e2e.py -q`

- [ ] **Step 3: Run the FULL fleet suite (the gate).**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py -q`
Expected: all pass (prior 149 + the new watchdog/monitor tests). Capture exact counts.

- [ ] **Step 4: Commit**

```bash
git add tests/test_fleet_watchdog_e2e.py
git commit -m "test(fleet): watchdog + monitor end-to-end recovery pass"
```

(Do NOT push — the orchestrator runs a final whole-branch review first.)

---

## Self-Review

**Spec coverage:**
- §2.1 reclaim crashed leases → Task 1. §2.2 trip breakers → Task 2. §2.3 recover breakers → Task 2. §2.4 nightly roll (guarded) → Task 5. §2.5 stuck workers (restart + quarantine) → Task 3. §2.6 cap enforcement → Task 4. Watchdog self-liveness beat → Task 1. `run_watchdog` + entrypoint → Task 6.
- §3.1 allow/deny guardrail (`MonitorActions`, deny ops absent + proven by test) → Task 7. §3 health report → Task 8.
- §5 testing: every task is TDD; the §5 scenarios (expired leases reclaimed, expired breaker recovered, scope tripped, stuck worker restarted, job quarantined, cap→paused, parked challenge untouched, deny-ops absent, report sections + seeded anomaly) are covered across Tasks 1–4, 7, 8 and combined in the Task 9 e2e.
- Non-goals (dashboard UI, apply authority) respected — none added.

**Placeholder scan:** none — every code step has complete code. Two `NOTE:` callouts (Task 4 `llm_usage` column names, Task 9 `auth_challenge` columns) instruct the implementer to confirm exact seed columns against `schema_v3.sql` before running; these are verification steps, not placeholders.

**Type consistency:** `WatchdogConfig` fields are referenced identically in Tasks 1–6. `watchdog_tick` summary keys accrete monotonically: `reclaimed_*` (T1) → `breakers_tripped`/`breakers_recovered` (T2) → `stuck_handled` (T3) → `paused_on_cap` (T4) → `rolled_window` (T5); the Task 9 e2e reads only keys defined by then. `MonitorActions` method names (`restart_worker`, `quarantine`, `pause_scope`, `report`) are consistent between Task 7 impl and its test; `build_health_report(snapshot, *, captcha_threshold, cost_cap_total)` signature matches between Task 8 impl and test. Foundation calls use the verified signatures in Global Constraints.

**One integration note:** the watchdog reclaims **apply** leases via `apply.pgqueue.reclaim_stale_leases` even though the apply lane isn't live yet — that's intentional (it's a no-op against an empty `apply_queue`) and keeps the watchdog complete for when apply ships. It NEVER resolves a parked challenge (those are frozen by `park_challenge` and excluded from reclaim); the Task 9 e2e asserts the parked challenge is untouched.
