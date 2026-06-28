# Fleet Apply Lane B (LinkedIn) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the fleet submit LinkedIn Easy-Apply applications — the catastrophe lane — behind an atomic single-event halt, a separate LinkedIn canary, a mandatory supervised-conflict interlock, and the lease-time dedup, all proven by tests.

**Architecture:** `_tick_linkedin` (role `linkedin`) leases via `lease_linkedin` (one-IP, account mutex), applies via A's `apply_fn`, routes off `run_job`'s verdict (re-implemented passthrough writing `linkedin_queue`); a single challenge writes `account:linkedin.halted_until` at park time in one tx, and `min_gap=ttl=1200s` makes the next lease ineligible until the prior lease ends. A separate `fleet_config.linkedin_canary_*` caps applies at K=1. A PG advisory lock interlocks the fleet vs the supervised LinkedIn driver. Built ON TOP of A (additive).

**Tech Stack:** Python 3.11+, psycopg 3 + Postgres, the proven `apply/launcher.py` apply path + the `li_at` Chrome profile, tested against the disposable `applypilot-pgtest` Postgres via `fleet_db`.

## Global Constraints

- **Repo / cwd:** `C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`. Run python as `.conda-env/python.exe`. Test: `.conda-env/python.exe -m pytest <file> -q`.
- **Catastrophe-class:** every safety gate PROVEN by a test. The halt-race test (drive an apply LONGER than the agent timeout → next lease still blocked until the halt lands) and the canary test (K=1 → exactly 1 lease) are MANDATORY.
- **Regression baseline (green every task that touches lease/worker/watchdog):** `tests/test_fleet_v3_governor_queue.py`, `tests/test_fleet_v3_worker.py`, the A apply-lane tests (`tests/test_fleet_apply_lane.py`/`_home.py`/`_e2e.py`), `tests/test_fleet_watchdog.py`. The four lease SQL constants (`_LEASE_APPLY/_COMPUTE/_SEARCH/_LINKEDIN`) are independent — a change to `_LEASE_LINKEDIN` can't ripple. **B is ADDITIVE to A** — do NOT modify `_LEASE_APPLY`, `apply_worker_main`, `apply_home_main`.
- **LinkedIn only from the owner IP, single worker, min_gap = ttl (1200s).** `applied_set` is shared (cross-lane dedup). Apply-as-is (base-resume).
- **Commit discipline:** `git add <exact paths>` ONLY — NEVER `git add -A`. Do NOT push. The 7 user-dirty files stay untouched. In-scope shared edits: `fleet/queue.py`, `fleet/worker.py`, `fleet/schema_v3.sql`, `fleet/watchdog.py`, `tests/conftest.py`, and `apply/launcher.py` (the supervised interlock side, Task 7).

## File Structure
- Modify `src/applypilot/fleet/schema_v3.sql` — `rate_governor.halted_until`, `fleet_config.linkedin_canary_*`.
- Modify `tests/conftest.py` — reset the new fleet_config columns.
- Modify `src/applypilot/fleet/queue.py` — `_LEASE_LINKEDIN` guards + `lease_linkedin` default; the new LinkedIn helpers.
- Modify `src/applypilot/fleet/worker.py` — `ROLE_LINKEDIN` + dispatch + `_tick_linkedin` + `_linkedin_status_passthrough`.
- Modify `src/applypilot/fleet/watchdog.py` — `reclaim_linkedin` in the tick.
- Create `src/applypilot/fleet/linkedin_worker_main.py`, `src/applypilot/fleet/linkedin_home_main.py`.
- Modify `src/applypilot/apply/launcher.py` — the supervised interlock probe.
- Modify `pyproject.toml` — the two scripts.
- Create `tests/test_fleet_linkedin_lane.py`, `tests/test_fleet_linkedin_home.py`, `tests/test_fleet_linkedin_e2e.py`; add cases to `tests/test_fleet_v3_worker.py`, `tests/test_fleet_watchdog.py`.
- Create `docs/fleet-linkedin-lane-runbook.md`.

---

### Task 1: Schema columns + fixture reset

**Files:** Modify `src/applypilot/fleet/schema_v3.sql`, `tests/conftest.py`; Test `tests/test_fleet_linkedin_lane.py`.

**Interfaces:** Produces `rate_governor.halted_until TIMESTAMPTZ` (NULL = not halted), `fleet_config.linkedin_canary_enabled BOOLEAN NOT NULL DEFAULT FALSE`, `fleet_config.linkedin_canary_remaining INTEGER`. The `fleet_db` fixture resets the two fleet_config columns.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_linkedin_lane.py
from applypilot.apply import pgqueue


def test_linkedin_schema_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_enabled, linkedin_canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["linkedin_canary_enabled"] is False and row["linkedin_canary_remaining"] is None
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES ('account:linkedin')")
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is None
```

- [ ] **Step 2: Run it, expect FAIL** (columns do not exist).

- [ ] **Step 3: Implement** — in `schema_v3.sql`, alongside the existing `ALTER TABLE … ADD COLUMN IF NOT EXISTS` blocks:

```sql
ALTER TABLE rate_governor ADD COLUMN IF NOT EXISTS halted_until TIMESTAMPTZ;
ALTER TABLE fleet_config  ADD COLUMN IF NOT EXISTS linkedin_canary_enabled  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fleet_config  ADD COLUMN IF NOT EXISTS linkedin_canary_remaining INTEGER;
```

In `tests/conftest.py`, add `linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL` to the `fleet_db` fixture's `UPDATE fleet_config SET …` reset.

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql tests/conftest.py tests/test_fleet_linkedin_lane.py
git commit -m "feat(fleet): LinkedIn halt + canary schema columns + fixture reset"
```

---

### Task 2: `_LEASE_LINKEDIN` guards (canary + halt + dedup) + min_gap=ttl

**Files:** Modify `src/applypilot/fleet/queue.py` (`_LEASE_LINKEDIN`, `lease_linkedin`); Test `tests/test_fleet_linkedin_lane.py`.

**Interfaces:** Produces `lease_linkedin` now also (a) skips a job whose `dedup_key` is in `applied_set`; (b) returns None when `account:linkedin.halted_until > now()`; (c) when `fleet_config.linkedin_canary_enabled`, leases at most `linkedin_canary_remaining` then the guard blocks (decremented on EXISTS(next), atomic under the account mutex; `fleet_config` locked FIRST for lock-order with `_LEASE_APPLY`); (d) defaults `min_gap_seconds=1200` (= ttl).

- [ ] **Step 1: Write the failing tests**

```python
def _seed_li(conn, n, *, batch="b1", approved=True):
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, approved_batch, dedup_key) "
                        "VALUES (%s,%s,%s,'queued','ats',%s,%s)",
                        (f"li{i}", f"https://linkedin.com/jobs/{i}", 9.0-i*0.01, batch if approved else None, f"dk{i}"))
        conn.commit()


def test_linkedin_lease_halt_blocks(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO rate_governor (scope_key, halted_until) VALUES ('account:linkedin', now() + interval '1 hour') "
                        "ON CONFLICT (scope_key) DO UPDATE SET halted_until=EXCLUDED.halted_until")
        conn.commit()
        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None


def test_linkedin_lease_dedup_blocks(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk0','Acme')")
        conn.commit()
        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None  # already applied


def test_linkedin_canary_caps(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 3)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1 WHERE id=1")
            cur.execute("UPDATE rate_governor SET min_gap_seconds=0 WHERE scope_key='account:linkedin'")  # not yet created
        conn.commit()
        a = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0)
        b = queue.lease_linkedin(conn, "w2", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0)
    assert a is not None and b is None  # canary capped at 1
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_remaining FROM fleet_config WHERE id=1")
        assert cur.fetchone()["linkedin_canary_remaining"] == 0


def test_linkedin_min_gap_default_is_ttl(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_li(conn, 1)
        queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")  # creates the account row
        with conn.cursor() as cur:
            cur.execute("SELECT min_gap_seconds FROM rate_governor WHERE scope_key='account:linkedin'")
            assert cur.fetchone()["min_gap_seconds"] == 1200
```

- [ ] **Step 2: Run them, expect FAIL** (guards not present; min_gap default 300).

- [ ] **Step 3: Implement** — replace `_LEASE_LINKEDIN` and change the `lease_linkedin` default:

```python
_LEASE_LINKEDIN = """
WITH cfg AS (SELECT linkedin_canary_enabled, linkedin_canary_remaining FROM fleet_config WHERE id=1 FOR UPDATE),
     acct AS (
       SELECT count_24h, daily_cap, last_applied_at, min_gap_seconds, breaker_state, halted_until
       FROM rate_governor WHERE scope_key = 'account:linkedin' FOR UPDATE
     ),
     next AS (
       SELECT q.url FROM linkedin_queue q LEFT JOIN acct a ON TRUE LEFT JOIN cfg ON TRUE
       WHERE q.status='queued' AND q.approved_batch IS NOT NULL
         AND (NOT COALESCE(cfg.linkedin_canary_enabled, FALSE) OR cfg.linkedin_canary_remaining > 0)
         AND (a.halted_until IS NULL OR a.halted_until < now())
         AND (a.count_24h IS NULL OR a.count_24h < a.daily_cap)
         AND COALESCE(a.breaker_state, 'ok') NOT IN ('paused','demoted')
         AND (a.last_applied_at IS NULL OR a.last_applied_at < now() - make_interval(secs => COALESCE(a.min_gap_seconds, 1200)))
         AND NOT EXISTS (SELECT 1 FROM applied_set s WHERE s.dedup_key = q.dedup_key)
       ORDER BY q.score DESC, q.url LIMIT 1 FOR UPDATE OF q SKIP LOCKED
     ),
     reserve AS (
       UPDATE rate_governor SET count_24h = count_24h + 1, last_applied_at = now(), updated_at = now()
       WHERE scope_key = 'account:linkedin' AND EXISTS (SELECT 1 FROM next) RETURNING 1
     ),
     canary AS (
       UPDATE fleet_config SET linkedin_canary_remaining = linkedin_canary_remaining - 1
       WHERE id = 1 AND linkedin_canary_enabled AND EXISTS (SELECT 1 FROM next) RETURNING 1
     )
UPDATE linkedin_queue q SET status='leased', lease_owner=%(worker)s,
  lease_expires_at = now() + make_interval(secs => %(ttl)s), last_attempted_at=now(), attempts=q.attempts+1, updated_at=now()
FROM next WHERE q.url = next.url
RETURNING q.url, q.company, q.title, q.application_url, q.score;
"""
```

Change `lease_linkedin`'s signature default: `min_gap_seconds=1200` (was 300). (The account-row INSERT already uses `min_gap_seconds`, so a freshly-created row gets 1200; the SQL COALESCE fallback also raised to 1200.) Keep a code comment: cfg is locked FIRST then acct (lock-order consistency with `_LEASE_APPLY`); the canary decrement is atomic via the account mutex; min_gap=ttl closes the halt-write race (§4.3).

- [ ] **Step 4: Run the new tests + the regression baseline, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_linkedin_lane.py tests/test_fleet_v3_governor_queue.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/queue.py tests/test_fleet_linkedin_lane.py
git commit -m "feat(fleet): _LEASE_LINKEDIN halt + canary + applied_set dedup guards, min_gap=ttl"
```

---

### Task 3: LinkedIn halt/reclaim helpers (`park_linkedin_challenge`, `reclaim_linkedin`, `clear_linkedin_halt`, `kill_linkedin`)

**Files:** Modify `src/applypilot/fleet/queue.py`; Test `tests/test_fleet_linkedin_lane.py`.

**Interfaces:**
- Produces `park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, commit=True) -> bool` — in ONE tx: freeze the held `linkedin_queue` lease out of reclaim (`apply_status='challenge_pending'`, `lease_expires_at = now() + 3650 days`) AND `INSERT account:linkedin ON CONFLICT DO NOTHING` then `UPDATE … SET halted_until = now() + make_interval(secs => halt_seconds)`. Lease-owner guarded.
- `reclaim_linkedin(conn, *, grace_seconds=30, commit=True) -> int` — sweep `linkedin_queue` leases past `lease_expires_at + grace`; ALL → `crash_unconfirmed`, `attempts=99` (NEVER re-queue — a stale LinkedIn lease is a possible mid-submit). Returns the count.
- `clear_linkedin_halt(conn, *, commit=True)` / `kill_linkedin(conn, *, commit=True)` — set `halted_until` NULL / `now()+100 years`; each `INSERT account:linkedin ON CONFLICT DO NOTHING` first (the row may not exist yet).

- [ ] **Step 1: Write the failing tests**

```python
from applypilot.fleet import governor


def test_park_linkedin_sets_halt_one_tx_even_without_account_row(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner) "
                    "VALUES ('lp','https://linkedin.com/jobs/x','9','leased','ats','w1')")
        conn.commit()
        assert queue.park_linkedin_challenge(conn, "w1", "lp", halt_seconds=21600) is True
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None       # account row was INSERTed + halted
        cur.execute("SELECT status, apply_status FROM linkedin_queue WHERE url='lp'")
        r = cur.fetchone(); assert r["status"] == "leased" and r["apply_status"] == "challenge_pending"  # frozen, not closed


def test_reclaim_linkedin_crash_unconfirmed_only(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner, lease_expires_at, attempts) "
                    "VALUES ('lr','https://linkedin.com/jobs/y','9','leased','ats','wDead', now()-interval '5 min', 1)")
        conn.commit()
        assert queue.reclaim_linkedin(conn) == 1
        cur.execute("SELECT status, attempts FROM linkedin_queue WHERE url='lr'")
        r = cur.fetchone(); assert r["status"] == "crash_unconfirmed" and r["attempts"] == 99  # NEVER re-queued


def test_clear_and_kill_halt(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        queue.kill_linkedin(conn)
        cur.execute("SELECT halted_until > now() + interval '300 days' AS far FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["far"] is True
        queue.clear_linkedin_halt(conn)
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is None
```

- [ ] **Step 2: Run them, expect FAIL** (AttributeError).

- [ ] **Step 3: Implement** — add to `queue.py` (mirror `park_challenge` for the freeze, `apply/pgqueue._RECLAIM_SQL` for reclaim, but LinkedIn-table + crash-only):

```python
def park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, commit=True) -> bool:
    """Freeze the held linkedin_queue lease out of reclaim AND set the account halt, in ONE tx."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET apply_status='challenge_pending', "
            "lease_expires_at = now() + interval '3650 days', updated_at=now() "
            "WHERE url=%s AND lease_owner=%s", (url, worker_id))
        if cur.rowcount == 0:
            conn.rollback(); return False
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (governor.LINKEDIN_ACCOUNT,))
        cur.execute("UPDATE rate_governor SET halted_until = now() + make_interval(secs => %s), updated_at=now() "
                    "WHERE scope_key=%s", (halt_seconds, governor.LINKEDIN_ACCOUNT))
    if commit:
        conn.commit()
    return True


def reclaim_linkedin(conn, *, grace_seconds=30, commit=True) -> int:
    """Stale linkedin_queue leases -> crash_unconfirmed, attempts=99, NEVER re-queued
    (a stale LinkedIn lease may have already submitted)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE linkedin_queue SET status='crash_unconfirmed', apply_error='crash_unconfirmed', "
            "attempts=99, lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE status='leased' AND lease_expires_at < now() - make_interval(secs => %s) "
            "RETURNING url", (grace_seconds,))
        n = len(cur.fetchall())
    if commit:
        conn.commit()
    return n


def _set_linkedin_halt(conn, value_sql, commit):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (governor.LINKEDIN_ACCOUNT,))
        cur.execute(f"UPDATE rate_governor SET halted_until = {value_sql}, updated_at=now() WHERE scope_key=%s",
                    (governor.LINKEDIN_ACCOUNT,))
    if commit:
        conn.commit()


def clear_linkedin_halt(conn, *, commit=True):
    _set_linkedin_halt(conn, "NULL", commit)


def kill_linkedin(conn, *, commit=True):
    _set_linkedin_halt(conn, "now() + interval '36500 days'", commit)
```

> NOTE: confirm `governor.LINKEDIN_ACCOUNT == 'account:linkedin'` (it's referenced in `write_linkedin_result`). If the `linkedin_queue.status` enum lacks `crash_unconfirmed`, it's the shared `apply_queue_status` enum which has it (confirm).

- [ ] **Step 4: Run them, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/queue.py tests/test_fleet_linkedin_lane.py
git commit -m "feat(fleet): park_linkedin_challenge (halt in one tx) + reclaim_linkedin (crash-only) + clear/kill halt"
```

---

### Task 4: `_tick_linkedin` + `ROLE_LINKEDIN`

**Files:** Modify `src/applypilot/fleet/worker.py`; Test `tests/test_fleet_v3_worker.py`.

**Interfaces:** Consumes `queue.lease_linkedin`, `queue.write_linkedin_result`, `queue.park_linkedin_challenge`, `chrome.has_linkedin_session`. Produces: `ROLE_LINKEDIN='linkedin'` (in the `__init__` allowlist + the `run_once` dispatch before the `_tick_apply` fallback); `_tick_linkedin(conn) -> dict` with a re-implemented status-passthrough writing `linkedin_queue`; `_linkedin_halt_seconds` (default from `APPLYPILOT_LINKEDIN_HALT_COOLDOWN`, 21600).

- [ ] **Step 1: Write the failing test** (add to `tests/test_fleet_v3_worker.py`)

```python
def test_tick_linkedin_routes(fleet_db):
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.apply import pgqueue

    def _seed(conn, url):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, approved_batch, dedup_key) "
                        "VALUES (%s,'https://linkedin.com/jobs/x','9','queued','ats','b1',%s)", (url, "dk-"+url))
        conn.commit()

    # applied -> applied + applied_set
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "ka")
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="linkedin",
                      public_ip="1.1.1.1", owner_ip="1.1.1.1",
                      apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.0})
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM linkedin_queue WHERE url='ka'"); assert cur.fetchone()["status"] == "applied"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ka'"); assert cur.fetchone()["n"] == 1

    # failed:no_result_line -> crash_unconfirmed (never phantom-applied)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "kc")
    loop2 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w2", home_ip="1.1.1.1", role="linkedin",
                       public_ip="1.1.1.1", owner_ip="1.1.1.1",
                       apply_fn=lambda job: {"run_status": "failed:no_result_line", "est_cost_usd": 0.0})
    assert loop2.run_once()["action"] == "crash_unconfirmed"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM linkedin_queue WHERE url='kc'"); assert cur.fetchone()["status"] == "crash_unconfirmed"

    # captcha -> parked + halted_until set (one tx)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "kp")
    loop3 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w3", home_ip="1.1.1.1", role="linkedin",
                       public_ip="1.1.1.1", owner_ip="1.1.1.1",
                       apply_fn=lambda job: {"run_status": "captcha", "est_cost_usd": 0.0})
    assert loop3.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='kp' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1
```

- [ ] **Step 2: Run it, expect FAIL** (`ValueError: unknown role: 'linkedin'`).

- [ ] **Step 3: Implement** — in `worker.py`: add `ROLE_LINKEDIN = "linkedin"`; add it to the `__init__` role-allowlist tuple (the one that raises `ValueError`); add a `run_once` dispatch branch `if self.role == ROLE_LINKEDIN: return self._tick_linkedin(conn)` BEFORE the `_tick_apply` fallback. Add:

```python
    _WALL_STATUSES = ("captcha", "login_issue", "auth_required")     # (already defined for apply; reuse)
    _CRASH_STATUSES = ("failed:no_result_line", "failed:timeout")

    def _linkedin_halt_seconds(self) -> int:
        import os
        return int(os.environ.get("APPLYPILOT_LINKEDIN_HALT_COOLDOWN") or 21600)

    def _tick_linkedin(self, conn) -> dict:
        # Pre-checks (belts; the lease SQL is the real enforcement). Session pre-flight +
        # halt pre-check; the interlock is held by the entrypoint for the worker's life.
        job = queue.lease_linkedin(conn, self.worker_id, public_ip=self.public_ip, owner_ip=self.owner_ip)
        if job is None:
            self._beat(conn, state="idle"); return {"action": "idle"}
        url = job["url"]
        self._beat(conn, state="applying", current_job=url)
        if self.apply_fn is None:
            raise RuntimeError("linkedin role requires an injected apply_fn")
        out = self.apply_fn(job)
        run_status = (out or {}).get("run_status") or ""
        cost = (out or {}).get("est_cost_usd", 0)
        if run_status == "applied":
            queue.write_linkedin_result(conn, self.worker_id, url, status="applied", apply_status="applied",
                                        est_cost_usd=cost, outcome="success")
            self._beat(conn, state="idle"); return {"action": "applied", "url": url}
        if run_status in self._WALL_STATUSES:
            queue.park_linkedin_challenge(conn, self.worker_id, url, halt_seconds=self._linkedin_halt_seconds())
            _insert_challenge(conn, url=url, worker_id=self.worker_id, machine_owner=self.machine_owner,
                              home_ip=self.home_ip, kind="visible_captcha" if run_status == "captcha" else "login_gate",
                              route="owner_inbox")
            self._beat(conn, state="challenge_pending", current_job=url)
            return {"action": "parked_challenge", "url": url}
        if run_status in self._CRASH_STATUSES or run_status.startswith("failed:worker_error"):
            queue.write_linkedin_result(conn, self.worker_id, url, status="crash_unconfirmed",
                                        apply_status="crash_unconfirmed", apply_error=run_status[:200], est_cost_usd=cost)
            self._beat(conn, state="idle"); return {"action": "crash_unconfirmed", "url": url}
        queue.write_linkedin_result(conn, self.worker_id, url, status="failed", apply_status="failed",
                                    apply_error=(run_status or "unknown")[:200], est_cost_usd=cost)
        self._beat(conn, state="idle"); return {"action": "failed", "url": url}
```

> NOTE: `park_linkedin_challenge` + `_insert_challenge` run in the same connection/tick; `park_linkedin_challenge(commit=True)` commits the freeze+halt, then `_insert_challenge(commit=True)` adds the challenge row. If the existing `_insert_challenge` defaults to commit, that's two commits — acceptable (the halt is the safety-critical one and lands first). If a single tx is preferred, pass `commit=False` to `park_linkedin_challenge` and commit after `_insert_challenge`; either way the halt is set. The session pre-flight + halt pre-check belts (per spec §3.1) may be added here reading `chrome.has_linkedin_session` + `halted_until`, but the lease SQL halt-guard is the load-bearing enforcement and is already covered by Task 2 — keep the belt minimal.

- [ ] **Step 4: Run the new test + the regression baseline, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_v3_worker.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/worker.py tests/test_fleet_v3_worker.py
git commit -m "feat(fleet): _tick_linkedin + ROLE_LINKEDIN — route off run_job, wall sets halt in one tx"
```

---

### Task 5: Watchdog `reclaim_linkedin` + `halted_until` survives roll_window

**Files:** Modify `src/applypilot/fleet/watchdog.py`; Test `tests/test_fleet_watchdog.py`.

**Interfaces:** Consumes `queue.reclaim_linkedin`, `governor.roll_window`. Produces: `watchdog_tick` summary GAINS `reclaimed_linkedin: int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_watchdog_reclaims_linkedin(fleet_db):
    from applypilot.fleet import watchdog
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner, lease_expires_at, attempts) "
                    "VALUES ('lw','https://linkedin.com/jobs/z','9','leased','ats','wDead', now()-interval '5 min', 1)")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["reclaimed_linkedin"] == 1


def test_roll_window_preserves_halt(fleet_db):
    from applypilot.fleet import governor
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, halted_until) VALUES ('account:linkedin', now()+interval '6 hours')")
        conn.commit()
        governor.roll_window(conn)
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None  # nightly roll did NOT clear the halt
```

- [ ] **Step 2: Run them, expect FAIL** (`KeyError: 'reclaimed_linkedin'`).

- [ ] **Step 3: Implement** — in `watchdog_tick`, alongside the other reclaim calls, add `summary["reclaimed_linkedin"] = queue.reclaim_linkedin(conn, grace_seconds=cfg.reclaim_grace_seconds)`. Add a one-line comment on `governor.roll_window` that `halted_until` is deliberately NOT reset by the nightly window roll (the second test already proves it).

- [ ] **Step 4: Run them + the watchdog suite, expect PASS.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_watchdog.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/watchdog.py src/applypilot/fleet/governor.py tests/test_fleet_watchdog.py
git commit -m "feat(fleet): watchdog reclaims stale LinkedIn leases; assert halt survives roll_window"
```

---

### Task 6: `linkedin_worker_main` entrypoint + the advisory-lock interlock (acquire side)

**Files:** Create `src/applypilot/fleet/linkedin_worker_main.py`; Modify `pyproject.toml`; Test `tests/test_fleet_linkedin_lane.py`.

**Interfaces:** Produces `acquire_linkedin_interlock(conn) -> bool` (`SELECT pg_try_advisory_lock(hashtext('applypilot:linkedin_driver'))`), `build_linkedin_loop(*, dsn, worker_id, owner_ip, …) -> WorkerLoop` (role='linkedin', the li_at profile, `public_ip=owner_ip`), `run_linkedin(...)` (the should_halt loop), `main(argv)` (acquires the interlock on a dedicated long-lived connection at startup; exits if the lock is already held).

- [ ] **Step 1: Write the failing test**

```python
def test_linkedin_interlock_refuses_when_held(fleet_db):
    from applypilot.fleet import linkedin_worker_main as lm
    from applypilot.apply import pgqueue
    holder = pgqueue.connect(fleet_db)  # a separate session holds the lock
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('applypilot:linkedin_driver'))")
        holder.commit()
        with pgqueue.connect(fleet_db) as conn:
            assert lm.acquire_linkedin_interlock(conn) is False  # already held -> refuse
    finally:
        holder.close()


def test_build_linkedin_loop_role(fleet_db):
    from applypilot.fleet import linkedin_worker_main as lm
    loop = lm.build_linkedin_loop(dsn=fleet_db, worker_id="w1", owner_ip="1.1.1.1", model="sonnet", agent="claude")
    assert loop.role == "linkedin" and loop.apply_fn is not None and loop.owner_ip == "1.1.1.1"
```

- [ ] **Step 2: Run it, expect FAIL** (ModuleNotFoundError).

- [ ] **Step 3: Implement** `linkedin_worker_main.py` — mirror `apply_worker_main` (port `_setup_apply_env`), but: the apply_fn's Chrome profile is the **`linkedin-seed` clone** (`setup_worker_profile` already prefers it for chrome workers — so no change to `make_apply_fn` IS needed if the seed exists; the entrypoint just must NOT force a fresh profile); `build_linkedin_loop` sets `role="linkedin"`, `public_ip=owner_ip`, `owner_ip=owner_ip`; `main` opens a dedicated connection, `acquire_linkedin_interlock`, and `raise SystemExit("another LinkedIn driver holds the interlock")` if False, holding that connection for the process life; `run_linkedin` is the `should_halt` drive loop (reuse A's `run_apply` shape). Register `applypilot-fleet-linkedin = "applypilot.fleet.linkedin_worker_main:main"`.

```python
def acquire_linkedin_interlock(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('applypilot:linkedin_driver')) AS ok")
        ok = cur.fetchone()["ok"]
    conn.commit()
    return bool(ok)
```

- [ ] **Step 4: Run it + import + pyproject check, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/linkedin_worker_main.py pyproject.toml tests/test_fleet_linkedin_lane.py
git commit -m "feat(fleet): applypilot-fleet-linkedin entrypoint + advisory-lock interlock (acquire)"
```

---

### Task 7: Supervised interlock probe (`apply/launcher.py`)

**Files:** Modify `src/applypilot/apply/launcher.py`; Test `tests/test_fleet_linkedin_lane.py`.

**Interfaces:** Produces `fleet_linkedin_active(pg_dsn) -> bool` — opens the fleet PG and `pg_try_advisory_lock` on `hashtext('applypilot:linkedin_driver')`; if the lock CANNOT be acquired (the fleet holds it) returns True (then immediately unlocks if it did acquire). The supervised `worker_loop` calls it and sets `exclude_li=True` when the fleet owns the LinkedIn lane.

- [ ] **Step 1: Write the failing test**

```python
def test_supervised_detects_fleet_linkedin(fleet_db):
    from applypilot.apply import launcher, pgqueue
    holder = pgqueue.connect(fleet_db)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('applypilot:linkedin_driver'))")
        holder.commit()
        assert launcher.fleet_linkedin_active(fleet_db) is True   # fleet holds it
    finally:
        holder.close()
    assert launcher.fleet_linkedin_active(fleet_db) is False      # lock free now
```

- [ ] **Step 2: Run it, expect FAIL** (AttributeError).

- [ ] **Step 3: Implement** — add `fleet_linkedin_active(pg_dsn)` to `launcher.py` (lazy psycopg import; `pg_try_advisory_lock`, and `pg_advisory_unlock` if it acquired — so the probe is non-destructive). In `worker_loop`, where `exclude_li` is computed (launcher.py ~1858), OR-in `fleet_linkedin_active(os.environ.get("FLEET_PG_DSN"))` when a fleet DSN is set (skip the probe entirely if no fleet DSN — pure supervised installs are unaffected). Guard the probe so a missing/unreachable fleet PG does NOT crash the supervised run (treat an error as "fleet not active" = supervised may proceed — the runbook is the backstop for that edge).

> NOTE: this is the one B task that touches the supervised path. Keep it minimal: a single new function + one OR-clause + the env guard. Do NOT alter any other supervised behavior. `launcher.py` is NOT one of the 7 user-dirty files (confirm with `git status` before editing; if it IS dirty, STOP and report).

- [ ] **Step 4: Run it, expect PASS.** Also run a supervised-path smoke (`tests/test_discovery_scheduler.py` is unrelated; if a launcher test exists, run it) to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/apply/launcher.py tests/test_fleet_linkedin_lane.py
git commit -m "feat(apply): supervised LinkedIn lane defers to the fleet via the shared interlock"
```

---

### Task 8: `linkedin_home_main` driver + push/approve/resolve helpers

**Files:** Create `src/applypilot/fleet/linkedin_home_main.py`; Modify `src/applypilot/fleet/queue.py` (push_linkedin_jobs, approve_linkedin_jobs, resolve_linkedin_challenge), `src/applypilot/fleet/sync.py` (push_linkedin_eligible); Modify `pyproject.toml`; Test `tests/test_fleet_linkedin_home.py`.

**Interfaces:** `push_linkedin_jobs(conn, rows, *, approved_batch=None) -> int` (UPSERT `linkedin_queue`, `dedup_key=_dedup.dedup_key(company,title)`); `approve_linkedin_jobs(conn, urls, batch) -> int`; `resolve_linkedin_challenge(conn, url, *, requeue=True) -> bool`; `sync.push_linkedin_eligible(*, sqlite_conn=None, pg_conn=None, score_floor=7, approved_batch=None, limit=None) -> int` (the effective-host LinkedIn select, stages unapproved). The home driver: `push`/`approve [--all-pushed]`(refuses unless `_linkedin_canary_armed`)/`pull`/`linkedin-canary K`/`lift-linkedin-canary`/`challenges`/`resolve-challenge`/`clear-halt`/`kill`/`status`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_linkedin_home.py
from applypilot.apply import pgqueue


def test_linkedin_approve_gated_by_canary(fleet_db):
    from applypilot.fleet import linkedin_home_main as hm, queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane) "
                    "VALUES ('q1','https://linkedin.com/jobs/1','9','queued','ats')")
        conn.commit()
        try:
            hm.approve(conn, all_pushed=True); assert False, "must refuse without canary"
        except SystemExit:
            pass
        hm.set_linkedin_canary(conn, 1)
        token = hm.approve(conn, all_pushed=True)
        cur.execute("SELECT approved_batch FROM linkedin_queue WHERE url='q1'")
        assert cur.fetchone()["approved_batch"] == token


def test_push_linkedin_jobs_dedup_key(fleet_db):
    from applypilot.fleet import queue, dedup as _dedup
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        queue.push_linkedin_jobs(conn, [{"url": "p1", "company": "Acme", "title": "COS",
                                         "application_url": "https://linkedin.com/jobs/1", "score": 9}], approved_batch=None)
        cur.execute("SELECT dedup_key, lane FROM linkedin_queue WHERE url='p1'")
        r = cur.fetchone()
        assert r["dedup_key"] == _dedup.dedup_key("Acme", "COS")  # same key as offsite -> cross-lane dedup
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement** the queue helpers (mirror `push_apply_jobs`/`approve_jobs`/`resolve_challenge` over `linkedin_queue`), `sync.push_linkedin_eligible` (mirror `push_apply_eligible` but the SELECT predicate is the **inverse** effective-host LinkedIn filter: `(CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'`, and it calls `push_linkedin_jobs`), and `linkedin_home_main.py` (mirror `apply_home_main` with its OWN `set_linkedin_canary`/`lift_linkedin_canary`/`_linkedin_canary_armed`/`approve`(reading/writing `linkedin_queue` + `linkedin_canary_*`)/`pull`/`challenges`/`resolve_challenge_cmd`/`clear_halt`/`kill`/`status`). Register `applypilot-fleet-linkedin-home`.

- [ ] **Step 4: Run it + pyproject check, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/linkedin_home_main.py src/applypilot/fleet/queue.py src/applypilot/fleet/sync.py pyproject.toml tests/test_fleet_linkedin_home.py
git commit -m "feat(fleet): applypilot-fleet-linkedin-home driver + push/approve/resolve LinkedIn helpers"
```

---

### Task 9: End-to-end + full suite + runbook

**Files:** Create `tests/test_fleet_linkedin_e2e.py`, `docs/fleet-linkedin-lane-runbook.md`.

- [ ] **Step 1: Write the e2e test** — the LinkedIn canary path: seed approved-able LinkedIn rows (distinct dedup_key, min_gap=0 for the test), arm `linkedin-canary 1`, approve, run >1 tick with a stub apply_fn → exactly 1 applied; then a wall tick sets `halted_until` and the next lease is blocked by the halt.

```python
def test_linkedin_canary_then_halt(fleet_db):
    from applypilot.apply import pgqueue
    from applypilot.fleet import linkedin_home_main as hm, queue
    from applypilot.fleet.worker import WorkerLoop
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        for i in range(3):
            cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, dedup_key) "
                        "VALUES (%s,%s,%s,'queued','ats',%s)", (f"e{i}", f"https://linkedin.com/jobs/{i}", 9-i*0.01, f"dke{i}"))
        # account row with min_gap=0 so the canary (not min-gap) is what caps
        cur.execute("INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds) VALUES ('account:linkedin', 20, 0)")
        conn.commit()
        hm.set_linkedin_canary(conn, 1); hm.approve(conn, all_pushed=True)
    applied = 0
    for i in range(3):
        loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), f"w{i}", home_ip="1.1.1.1", role="linkedin",
                          public_ip="1.1.1.1", owner_ip="1.1.1.1",
                          apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.0})
        if loop.run_once().get("action") == "applied":
            applied += 1
    assert applied == 1  # canary capped LinkedIn at exactly 1
```

- [ ] **Step 2: Run it; iterate until green.**

- [ ] **Step 3: Run the FULL fleet suite.**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_codex_bridge.py tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py -q`
Expected: all pass (prior 201+ plus the new LinkedIn tests). Capture exact counts; 0 failures.

- [ ] **Step 4: Write the runbook** `docs/fleet-linkedin-lane-runbook.md` per spec §8: preconditions (supervised LinkedIn OFF — the interlock enforces it; watchdog running; fresh `li_at`; owner box), then dry-run → pull → push → `linkedin-canary 1` → approve → start `applypilot-fleet-linkedin` (acquires the interlock) → applies ONE → review (halted_until still NULL?) → re-arm or raise → `kill` if anything's off. Residuals (6h cooldown, stale-cookie over-halt, presence-stamp approval).

- [ ] **Step 5: Commit** (do NOT push)

```bash
git add tests/test_fleet_linkedin_e2e.py docs/fleet-linkedin-lane-runbook.md
git commit -m "test(fleet): LinkedIn canary-then-halt e2e + runbook"
```

---

## Self-Review

**Spec coverage:** §3.1 `_tick_linkedin`+ROLE → Task 4. §3.2 park/reclaim/clear/kill → Task 3; push/approve/resolve → Task 8. §3.3 entrypoint → Task 6. §3.4 home driver → Task 8. §4.3 atomic halt (park-time, one tx) + min_gap=ttl → Task 2 (lease guard + default) + Task 3 (park-time write) + Task 4 (wall routes through park). §4.4 separate canary → Task 1 (schema) + Task 2 (lease) + Task 8 (arm/lift). §4.7 added applied_set dedup → Task 2. §4.8 phantom passthrough → Task 4. §5.3 MANDATORY interlock → Task 6 (acquire) + Task 7 (supervised probe). §5.4 watchdog reclaim_linkedin + roll_window-preserves-halt → Task 5. §6 error handling → Tasks 3/4. §7 testing/regression baseline → every task; Task 9 full suite. §8 runbook → Task 9.

**Placeholder scan:** none — complete code in every code step. Three `NOTE:`s (Task 3 `governor.LINKEDIN_ACCOUNT`/enum confirm; Task 4 the two-commit park+challenge ordering; Task 7 launcher.py-not-dirty check) are verification instructions.

**Type consistency:** the `apply_fn` contract `{"run_status","est_cost_usd"}` matches A and is used in Tasks 4/6/9. `park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, commit)`, `reclaim_linkedin(conn, *, grace_seconds, commit)`, `clear_linkedin_halt`/`kill_linkedin`, `push_linkedin_jobs`/`approve_linkedin_jobs`/`resolve_linkedin_challenge`, `set_linkedin_canary`/`_linkedin_canary_armed`, `acquire_linkedin_interlock`/`fleet_linkedin_active` are named identically across their defining task and their callers. `ROLE_LINKEDIN='linkedin'` matches the role string in every test. The advisory-lock key `hashtext('applypilot:linkedin_driver')` is identical in Task 6 (acquire) and Task 7 (probe) — a mismatch there would silently disable the interlock, so it is a single literal repeated verbatim.

**One integration note:** the interlock key string MUST be byte-identical in Task 6 and Task 7 (`'applypilot:linkedin_driver'`) — the final whole-branch review must verify this, since a typo makes the two drivers lock on different keys (no mutual exclusion). The halt write (Task 3, park-time) + min_gap=ttl (Task 2) together close the lease-commit race; Task 4's wall branch must route through `park_linkedin_challenge` (not `write_linkedin_result`) so the halt is set — the Task 4 test asserts `halted_until` is set on a wall, which fails if the routing regresses.
