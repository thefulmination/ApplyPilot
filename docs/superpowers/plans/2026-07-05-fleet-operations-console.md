# Fleet Operations Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an operator-grade ApplyPilot fleet console that explains why the fleet is or is not applying, surfaces model/agent routing, and presents safe next actions without weakening existing fleet safety rails.

**Architecture:** Keep the existing LAN-only stdlib console in `src/applypilot/fleet/console_app.py`, but move heavier read-only diagnosis logic into focused helper modules. Add additive worker heartbeat telemetry for agent/model routing, expose read-only diagnostic endpoints, and update the embedded frontend to render an operator dashboard while preserving the existing token-gated action allow-list.

**Tech Stack:** Python stdlib HTTP server, Postgres via existing `psycopg`/`pgqueue`, embedded HTML/CSS/JS, pytest, existing disposable `fleet_db` fixture.

---

## File Structure

- Create `src/applypilot/fleet/console_diagnosis.py`
  - Owns read-only queue eligibility, why-not-applying state, recommendations, throughput, host/source quality, and worker comparison.
- Create `src/applypilot/fleet/console_agents.py`
  - Owns agent/model routing read model from `worker_heartbeat`, `agent_availability`, `llm_usage`, and recent apply queue rows.
- Create `src/applypilot/fleet/console_browser_health.py`
  - Owns deterministic classification of browser/backend/auth/usage-limit failure text.
- Modify `src/applypilot/fleet/schema_v3.sql`
  - Adds additive `worker_heartbeat` columns for live agent/model telemetry and optional console audit table.
- Modify `src/applypilot/fleet/worker.py`
  - Extends `_heartbeat()` to persist optional agent/model telemetry.
- Modify `src/applypilot/fleet/apply_worker_main.py`
  - Passes current effective agent/model/chain/switch reason into worker heartbeat without changing lease or apply behavior.
- Modify `src/applypilot/fleet/console_app.py`
  - Adds read endpoints, folds small diagnosis summary into `/api/status`, keeps actions allow-listed, and updates embedded UI.
- Create `tests/test_console_diagnosis.py`
  - Covers queue eligibility and why-not-applying diagnosis.
- Create `tests/test_console_agents.py`
  - Covers agent/model availability, dynamic switching verdicts, and spend rollups.
- Create `tests/test_console_browser_health.py`
  - Covers failure classifier behavior.
- Create `tests/test_console_audit.py`
  - Covers audit rows for existing actions and secret scrubbing.
- Modify existing console tests:
  - `tests/test_console_challenges_page.py`
  - `tests/test_console_token.py`
  - `tests/test_fleet_console_doctor.py`
  - `tests/test_fleet_v3_schema.py`
  - `tests/test_fleet_v3_worker.py`
  - `tests/test_apply_worker_switching.py`

Do not read or write the live SQLite brain DB. Do not run fleet apply/discover/score/push/approve/pull workers during implementation. Use disposable test Postgres via `fleet_db`.

---

### Task 1: Queue Diagnosis Core

**Files:**
- Create: `src/applypilot/fleet/console_diagnosis.py`
- Test: `tests/test_console_diagnosis.py`

- [ ] **Step 1: Write failing tests for ATS queue eligibility**

Create `tests/test_console_diagnosis.py` with:

```python
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_diagnosis


def _seed_apply_job(conn, *, url: str, company: str = "Acme", title: str = "Engineer",
                    status: str = "queued", approved_batch: str | None = "batch-1",
                    dedup_key: str | None = None, score: float = 8.0,
                    target_host: str = "boards.greenhouse.io") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, company, title, application_url, score, lane, status, approved_batch, "
            "dedup_key, target_host, apply_domain, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'ats',%s,%s,%s,%s,%s,now())",
            (url, company, title, url + "/apply", score, status, approved_batch,
             dedup_key or f"{company.lower()}::{title.lower()}", target_host, target_host),
        )


def test_ats_queue_diagnosis_counts_dedup_blocked_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(conn, url="https://boards.greenhouse.io/acme/jobs/1",
                        dedup_key="acme::engineer")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::engineer', 'Acme', 'https://already/applied')"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 1
    assert ats["leaseable"] == 0
    assert result["state"]["code"] == "idle_no_leasable_jobs"
    assert "dedup" in result["state"]["reason"].lower()


def test_ats_queue_diagnosis_counts_leaseable_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(conn, url="https://boards.greenhouse.io/acme/jobs/2",
                        dedup_key="acme::analyst")
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 0
    assert ats["leaseable"] == 1
    assert result["state"]["code"] == "ready_to_apply"


def test_linkedin_canary_exhaustion_is_lane_specific(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/1','Beta','Analyst',"
                "'https://www.linkedin.com/jobs/view/1',8,'ats','queued','batch-li','beta::analyst',now())"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=0, canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["linkedin"]["queued"] == 1
    assert result["linkedin"]["approved"] == 1
    assert result["linkedin"]["leaseable"] == 0
    assert result["linkedin"]["canary_exhausted"] is True
    assert result["ats"]["canary_exhausted"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py -q
```

Expected: FAIL with `ImportError: cannot import name 'console_diagnosis'`.

- [ ] **Step 3: Implement minimal diagnosis module**

Create `src/applypilot/fleet/console_diagnosis.py`:

```python
"""Read-only diagnosis helpers for the LAN fleet console.

No live actions are performed here. Every function receives an existing PG connection,
uses parameterized SQL, and rolls back its read transaction before returning.
"""
from __future__ import annotations

from typing import Any


def _scalar(row: Any, key: str, default=0):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _queue_counts(cur, table: str) -> dict[str, int]:
    cur.execute(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")
    return {r["status"]: int(r["n"]) for r in cur.fetchall()}


def _approved_count(cur, table: str) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} "
        "WHERE status='queued' AND approved_batch IS NOT NULL"
    )
    return int(cur.fetchone()["n"])


def _dedup_blocked_count(cur, table: str) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} q "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
    )
    return int(cur.fetchone()["n"])


def _leaseable_count(cur, table: str, *, canary_column: str | None = None,
                     canary_enabled_column: str | None = None) -> int:
    canary_predicate = ""
    if canary_column and canary_enabled_column:
        canary_predicate = (
            f"AND (NOT COALESCE(cfg.{canary_enabled_column}, FALSE) "
            f"     OR COALESCE(cfg.{canary_column}, 0) > 0) "
        )
    cur.execute(
        f"WITH cfg AS (SELECT * FROM fleet_config WHERE id=1) "
        f"SELECT COUNT(*) AS n FROM {table} q, cfg "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        f"{canary_predicate}"
        "AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
    )
    return int(cur.fetchone()["n"])


def queue_diagnosis(conn) -> dict:
    """Return queue eligibility and a plain-English fleet state.

    This intentionally starts with the high-signal guards that explain the current
    fleet confusion: queued, approved, leaseable, dedup-blocked, and canary exhaustion.
    Later tasks add host/governor/browser/recommendation detail on top of this shape.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT paused, ats_paused, canary_enabled, canary_remaining, "
            "linkedin_canary_enabled, linkedin_canary_remaining, spend_cap_usd "
            "FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone() or {}

        ats_depth = _queue_counts(cur, "apply_queue")
        li_depth = _queue_counts(cur, "linkedin_queue")

        ats = {
            "queued": ats_depth.get("queued", 0),
            "leased": ats_depth.get("leased", 0),
            "applied": ats_depth.get("applied", 0),
            "failed": ats_depth.get("failed", 0),
            "blocked": ats_depth.get("blocked", 0),
            "crash_unconfirmed": ats_depth.get("crash_unconfirmed", 0),
            "approved": _approved_count(cur, "apply_queue"),
            "dedup_blocked": _dedup_blocked_count(cur, "apply_queue"),
            "leaseable": _leaseable_count(
                cur, "apply_queue",
                canary_enabled_column="canary_enabled",
                canary_column="canary_remaining",
            ),
            "canary_enabled": bool(cfg.get("canary_enabled")),
            "canary_remaining": cfg.get("canary_remaining"),
            "canary_exhausted": bool(cfg.get("canary_enabled")) and int(cfg.get("canary_remaining") or 0) <= 0,
            "paused": bool(cfg.get("paused")),
            "ats_paused": bool(cfg.get("ats_paused")),
        }
        linkedin = {
            "queued": li_depth.get("queued", 0),
            "leased": li_depth.get("leased", 0),
            "applied": li_depth.get("applied", 0),
            "failed": li_depth.get("failed", 0),
            "approved": _approved_count(cur, "linkedin_queue"),
            "dedup_blocked": _dedup_blocked_count(cur, "linkedin_queue"),
            "leaseable": _leaseable_count(
                cur, "linkedin_queue",
                canary_enabled_column="linkedin_canary_enabled",
                canary_column="linkedin_canary_remaining",
            ),
            "canary_enabled": bool(cfg.get("linkedin_canary_enabled")),
            "canary_remaining": cfg.get("linkedin_canary_remaining"),
            "canary_exhausted": bool(cfg.get("linkedin_canary_enabled"))
            and int(cfg.get("linkedin_canary_remaining") or 0) <= 0,
        }
    conn.rollback()

    if ats["paused"]:
        state = {"code": "paused", "severity": "halted", "reason": "Fleet is paused by the shared kill switch."}
    elif ats["ats_paused"]:
        state = {"code": "ats_paused", "severity": "halted", "reason": "ATS lane is paused."}
    elif ats["canary_exhausted"]:
        state = {"code": "ats_canary_exhausted", "severity": "halted", "reason": "ATS canary is exhausted."}
    elif ats["leaseable"] > 0:
        state = {"code": "ready_to_apply", "severity": "ok", "reason": "Leaseable ATS jobs are available."}
    elif ats["approved"] > 0 and ats["dedup_blocked"] == ats["approved"]:
        state = {
            "code": "idle_no_leasable_jobs",
            "severity": "warn",
            "reason": "Approved queued ATS rows are already protected by applied_set dedup guards.",
        }
    else:
        state = {"code": "idle_no_leasable_jobs", "severity": "warn", "reason": "No leaseable ATS jobs are available."}

    return {"state": state, "ats": ats, "linkedin": linkedin}
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/fleet/console_diagnosis.py tests/test_console_diagnosis.py
git commit -m "Add fleet console queue diagnosis"
```

---

### Task 2: Agent And Model Telemetry Schema

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `src/applypilot/fleet/worker.py`
- Test: `tests/test_fleet_v3_schema.py`
- Test: `tests/test_fleet_v3_worker.py`

- [ ] **Step 1: Write failing schema test**

Append to `tests/test_fleet_v3_schema.py`:

```python
def test_worker_heartbeat_agent_model_columns(fleet_db):
    from applypilot.apply import pgqueue

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='worker_heartbeat'"
            )
            cols = {r["column_name"] for r in cur.fetchall()}

    assert "current_agent" in cols
    assert "current_model" in cols
    assert "agent_chain" in cols
    assert "last_agent_switch_at" in cols
    assert "last_agent_switch_reason" in cols
```

- [ ] **Step 2: Write failing heartbeat persistence test**

Append to `tests/test_fleet_v3_worker.py`:

```python
def test_heartbeat_persists_agent_model_telemetry(fleet_db):
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import _heartbeat

    with pgqueue.connect(fleet_db) as conn:
        _heartbeat(
            conn,
            worker_id="m4-0",
            machine_owner="m4",
            home_ip="100.69.68.103",
            role="apply",
            state="idle",
            current_agent="claude",
            current_model="sonnet",
            agent_chain="claude>codex",
            last_agent_switch_reason="startup",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_agent, current_model, agent_chain, last_agent_switch_reason "
                "FROM worker_heartbeat WHERE worker_id='m4-0'"
            )
            row = cur.fetchone()

    assert row["current_agent"] == "claude"
    assert row["current_model"] == "sonnet"
    assert row["agent_chain"] == "claude>codex"
    assert row["last_agent_switch_reason"] == "startup"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_fleet_v3_schema.py::test_worker_heartbeat_agent_model_columns tests/test_fleet_v3_worker.py::test_heartbeat_persists_agent_model_telemetry -q
```

Expected: FAIL because columns and `_heartbeat()` keyword arguments do not exist.

- [ ] **Step 4: Add schema columns**

Modify `src/applypilot/fleet/schema_v3.sql` after the existing `worker_heartbeat` log columns:

```sql
-- Live apply-agent/model telemetry for the fleet console. Workers own these
-- fields; the console reads them to explain Claude/Codex routing and switching.
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS current_agent TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS current_model TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS agent_chain TEXT;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_agent_switch_at TIMESTAMPTZ;
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS last_agent_switch_reason TEXT;
```

- [ ] **Step 5: Extend `_heartbeat()`**

Modify `src/applypilot/fleet/worker.py` function signature:

```python
def _heartbeat(conn, *, worker_id, machine_owner, home_ip, role, state, current_job=None,
               sw_version=None, last_error=None, recent_log=None, current_agent=None,
               current_model=None, agent_chain=None, last_agent_switch_reason=None,
               commit=True) -> None:
```

Replace the INSERT/UPSERT SQL with:

```python
cur.execute(
    "INSERT INTO worker_heartbeat "
    "(worker_id, machine_owner, home_ip, role, state, current_job, sw_version, "
    "last_error, recent_log, current_agent, current_model, agent_chain, "
    "last_agent_switch_at, last_agent_switch_reason, last_beat) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "CASE WHEN %s IS NULL THEN NULL ELSE now() END,%s,now()) "
    "ON CONFLICT (worker_id) DO UPDATE SET "
    "machine_owner=EXCLUDED.machine_owner, home_ip=EXCLUDED.home_ip, "
    "role=EXCLUDED.role, state=EXCLUDED.state, current_job=EXCLUDED.current_job, "
    "sw_version=COALESCE(EXCLUDED.sw_version, worker_heartbeat.sw_version), "
    "last_error=EXCLUDED.last_error, recent_log=EXCLUDED.recent_log, "
    "current_agent=COALESCE(EXCLUDED.current_agent, worker_heartbeat.current_agent), "
    "current_model=COALESCE(EXCLUDED.current_model, worker_heartbeat.current_model), "
    "agent_chain=COALESCE(EXCLUDED.agent_chain, worker_heartbeat.agent_chain), "
    "last_agent_switch_at=CASE "
    "  WHEN EXCLUDED.last_agent_switch_reason IS NULL THEN worker_heartbeat.last_agent_switch_at "
    "  ELSE EXCLUDED.last_agent_switch_at END, "
    "last_agent_switch_reason=COALESCE(EXCLUDED.last_agent_switch_reason, worker_heartbeat.last_agent_switch_reason), "
    "last_beat=now()",
    (worker_id, machine_owner, home_ip, role, state, current_job, sw_version,
     last_error, recent_log, current_agent, current_model, agent_chain,
     last_agent_switch_reason, last_agent_switch_reason),
)
```

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_fleet_v3_schema.py::test_worker_heartbeat_agent_model_columns tests/test_fleet_v3_worker.py::test_heartbeat_persists_agent_model_telemetry -q
```

Expected: `2 passed`.

- [ ] **Step 7: Commit**

```powershell
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/worker.py tests/test_fleet_v3_schema.py tests/test_fleet_v3_worker.py
git commit -m "Track apply agent model telemetry in heartbeats"
```

---

### Task 3: Apply Worker Agent Switching Telemetry

**Files:**
- Modify: `src/applypilot/fleet/apply_worker_main.py`
- Modify: `src/applypilot/fleet/worker.py`
- Test: `tests/test_apply_worker_switching.py`

- [ ] **Step 1: Write failing test for switch telemetry callback**

Append to `tests/test_apply_worker_switching.py`:

```python
def test_run_apply_updates_loop_agent_telemetry_on_switch(monkeypatch):
    from applypilot.fleet.agent_switch import AgentSwitcher
    from applypilot.fleet import apply_worker_main as M

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class Loop:
        def __init__(self):
            self.apply_fn = None
            self.agent_events = []
            self.calls = 0

        def set_agent_telemetry(self, *, current_agent, current_model, agent_chain,
                                last_agent_switch_reason=None):
            self.agent_events.append({
                "current_agent": current_agent,
                "current_model": current_model,
                "agent_chain": agent_chain,
                "last_agent_switch_reason": last_agent_switch_reason,
            })

        def run_once(self):
            self.calls += 1
            return {"action": "idle"}

    monkeypatch.setattr(M.pgqueue, "ats_should_halt", lambda conn: False, raising=False)
    monkeypatch.setattr(M, "_apply_timeout_override", lambda conn=None, dsn=None: None)

    loop = Loop()
    switcher = AgentSwitcher(agents=["claude", "codex"])
    rebuilt = []

    def rebuild(agent):
        rebuilt.append(agent)
        return lambda job: {"run_status": "failed:no_result_line", "agent": agent}

    M.run_apply(
        lambda: Conn(),
        loop,
        max_iterations=1,
        idle_sleep=0,
        switcher=switcher,
        rebuild_apply_fn=rebuild,
        time_fn=lambda: 1000.0,
    )

    assert rebuilt == ["claude"]
    assert loop.agent_events == [{
        "current_agent": "claude",
        "current_model": None,
        "agent_chain": "claude>codex",
        "last_agent_switch_reason": "startup",
    }]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/test_apply_worker_switching.py::test_run_apply_updates_loop_agent_telemetry_on_switch -q
```

Expected: FAIL because `run_apply()` does not call `set_agent_telemetry()`.

- [ ] **Step 3: Add telemetry setter to `WorkerLoop`**

In `src/applypilot/fleet/worker.py`, add instance fields in `WorkerLoop.__init__`:

```python
self._current_agent = None
self._current_model = None
self._agent_chain = None
self._last_agent_switch_reason = None
```

Add method to `WorkerLoop`:

```python
def set_agent_telemetry(self, *, current_agent=None, current_model=None,
                        agent_chain=None, last_agent_switch_reason=None) -> None:
    self._current_agent = current_agent
    self._current_model = current_model
    self._agent_chain = agent_chain
    self._last_agent_switch_reason = last_agent_switch_reason
```

In every internal call to `_heartbeat()` inside `WorkerLoop._beat`, pass:

```python
current_agent=self._current_agent,
current_model=self._current_model,
agent_chain=self._agent_chain,
last_agent_switch_reason=self._last_agent_switch_reason,
```

After the `_heartbeat()` call succeeds, clear only the switch reason so it is a one-beat event:

```python
self._last_agent_switch_reason = None
```

- [ ] **Step 4: Update `run_apply()` switch branch**

In `src/applypilot/fleet/apply_worker_main.py`, inside:

```python
if agent != current_agent and rebuild_apply_fn is not None:
```

replace the branch with:

```python
if agent != current_agent and rebuild_apply_fn is not None:
    loop.apply_fn = rebuild_apply_fn(agent)
    reason = "startup" if current_agent is None else f"switch:{current_agent}->{agent}"
    current_agent = agent
    setter = getattr(loop, "set_agent_telemetry", None)
    if callable(setter):
        setter(
            current_agent=agent,
            current_model=getattr(loop, "_agent_model", None),
            agent_chain=">".join(getattr(switcher, "agents", [agent])),
            last_agent_switch_reason=reason,
        )
```

- [ ] **Step 5: Set model on production loop**

In `build_apply_loop()`, after constructing the loop, set:

```python
loop = WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="apply",
                  apply_fn=make_apply_fn(model, agent, slot), machine_owner=machine_owner,
                  log_tail_fn=make_log_tail_fn(slot))
loop._agent_model = model
loop.set_agent_telemetry(current_agent=agent, current_model=model, agent_chain=agent)
return loop
```

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_apply_worker_switching.py::test_run_apply_updates_loop_agent_telemetry_on_switch tests/test_fleet_v3_worker.py::test_heartbeat_persists_agent_model_telemetry -q
```

Expected: `2 passed`.

- [ ] **Step 7: Commit**

```powershell
git add src/applypilot/fleet/apply_worker_main.py src/applypilot/fleet/worker.py tests/test_apply_worker_switching.py
git commit -m "Expose agent switching telemetry on apply workers"
```

---

### Task 4: Agent And Model Read API

**Files:**
- Create: `src/applypilot/fleet/console_agents.py`
- Modify: `src/applypilot/fleet/console_app.py`
- Test: `tests/test_console_agents.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_console_agents.py`:

```python
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_agents


def test_agent_summary_reads_worker_heartbeat_blocks_and_spend(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, last_beat, "
                "current_agent, current_model, agent_chain, last_agent_switch_reason) "
                "VALUES ('m4-0','m4','100.69.68.103','apply','idle',now(),"
                "'codex','sonnet','claude>codex','switch:claude->codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall')"
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, task, provider, model, cost_usd, ts) "
                "VALUES ('m4-0','apply_agent','codex','sonnet',0.42,now())"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["workers"][0]["worker_id"] == "m4-0"
    assert result["workers"][0]["current_agent"] == "codex"
    assert result["workers"][0]["current_model"] == "sonnet"
    assert result["availability"]["claude"]["blocked"] is True
    assert result["availability"]["claude"]["reason"] == "usage_limit_wall"
    assert result["spend_24h"][0]["provider"] == "codex"
    assert result["spend_24h"][0]["cost_usd"] == 0.42
    assert result["verdict"]["code"] == "working"


def test_agent_summary_detects_all_agents_blocked(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, role, state, last_beat, current_agent, current_model, agent_chain) "
                "VALUES ('m2-0','apply','idle',now(),'claude','sonnet','claude>codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall'), "
                "('codex', now() + interval '1 hour', 'predictive_spend')"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["verdict"]["code"] == "all_agents_blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_console_agents.py -q
```

Expected: FAIL because `console_agents` does not exist.

- [ ] **Step 3: Implement `console_agents.py`**

Create `src/applypilot/fleet/console_agents.py`:

```python
"""Read-only agent/model routing view for the fleet console."""
from __future__ import annotations

from datetime import timezone


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _chain_agents(chain: str | None) -> list[str]:
    if not chain:
        return []
    return [part.strip() for part in chain.replace(",", ">").split(">") if part.strip()]


def agent_summary(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_id, machine_owner, role, state, last_beat, current_agent, "
            "current_model, agent_chain, last_agent_switch_at, last_agent_switch_reason "
            "FROM worker_heartbeat WHERE role='apply' ORDER BY worker_id"
        )
        worker_rows = cur.fetchall()
        cur.execute(
            "SELECT agent, blocked_until, reason, updated_at, "
            "(blocked_until IS NOT NULL AND blocked_until > now()) AS blocked "
            "FROM agent_availability ORDER BY agent"
        )
        availability_rows = cur.fetchall()
        cur.execute(
            "SELECT provider, model, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost_usd "
            "FROM llm_usage "
            "WHERE task='apply_agent' AND ts > now() - interval '24 hours' "
            "GROUP BY provider, model ORDER BY cost_usd DESC, provider NULLS LAST"
        )
        spend_rows = cur.fetchall()
    conn.rollback()

    workers = [{
        "worker_id": r["worker_id"],
        "machine_owner": r["machine_owner"],
        "role": r["role"],
        "state": r["state"],
        "last_beat": _iso(r["last_beat"]),
        "current_agent": r["current_agent"],
        "current_model": r["current_model"],
        "agent_chain": r["agent_chain"],
        "last_agent_switch_at": _iso(r["last_agent_switch_at"]),
        "last_agent_switch_reason": r["last_agent_switch_reason"],
    } for r in worker_rows]
    availability = {
        r["agent"]: {
            "blocked": bool(r["blocked"]),
            "blocked_until": _iso(r["blocked_until"]),
            "reason": r["reason"],
            "updated_at": _iso(r["updated_at"]),
        }
        for r in availability_rows
    }
    spend_24h = [{
        "provider": r["provider"],
        "model": r["model"],
        "count": int(r["n"] or 0),
        "cost_usd": float(r["cost_usd"] or 0),
    } for r in spend_rows]

    verdict = {"code": "unknown", "severity": "warn", "reason": "No apply worker agent telemetry is available."}
    if workers:
        all_chain_agents = sorted({a for w in workers for a in _chain_agents(w.get("agent_chain"))})
        blocked_agents = {a for a, row in availability.items() if row["blocked"]}
        if all_chain_agents and set(all_chain_agents).issubset(blocked_agents):
            verdict = {"code": "all_agents_blocked", "severity": "halted", "reason": "Every configured apply agent is currently blocked."}
        elif any((w.get("last_agent_switch_reason") or "").startswith("switch:") for w in workers):
            verdict = {"code": "working", "severity": "ok", "reason": "At least one worker reports a recent dynamic agent switch."}
        elif any(row["blocked"] for row in availability.values()):
            verdict = {"code": "partial", "severity": "warn", "reason": "Agent blocks exist, but no worker reports a recent fallback switch."}
        else:
            verdict = {"code": "not_triggered", "severity": "ok", "reason": "No active agent block requires fallback switching."}

    return {
        "workers": workers,
        "availability": availability,
        "spend_24h": spend_24h,
        "verdict": verdict,
    }
```

- [ ] **Step 4: Add console route**

In `src/applypilot/fleet/console_app.py`, import inside handler route to keep startup light:

```python
if path == "/api/agents":
    from applypilot.fleet import console_agents
    conn = pgqueue.connect()
    try:
        self._send_json(200, console_agents.agent_summary(conn))
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    return
```

Place it next to other read-only GET endpoints before `/api/logs`.

- [ ] **Step 5: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_console_agents.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```powershell
git add src/applypilot/fleet/console_agents.py src/applypilot/fleet/console_app.py tests/test_console_agents.py
git commit -m "Add fleet console agent routing API"
```

---

### Task 5: Browser Health Classification

**Files:**
- Create: `src/applypilot/fleet/console_browser_health.py`
- Modify: `src/applypilot/fleet/console_diagnosis.py`
- Test: `tests/test_console_browser_health.py`

- [ ] **Step 1: Write failing classifier tests**

Create `tests/test_console_browser_health.py`:

```python
from applypilot.fleet import console_browser_health as B


def test_classify_browser_backend_crash():
    c = B.classify_text("The Playwright browser backend at port 9401 has crashed and is unavailable")
    assert c["kind"] == "browser_backend_crashed"
    assert c["severity"] == "error"


def test_classify_browser_connection_refused():
    c = B.classify_text("ECONNREFUSED on port 9400; browser_unavailable")
    assert c["kind"] == "browser_service_unavailable"


def test_classify_captcha():
    c = B.classify_text("hCaptcha appeared and CapSolver returned ERROR_INVALID_TASK_DATA")
    assert c["kind"] == "captcha"


def test_classify_login_gate():
    c = B.classify_text("RESULT:AUTH_REQUIRED login page detected")
    assert c["kind"] == "login_gate"


def test_classify_employer_application_cap():
    c = B.classify_text("we limit the number of applications so our team can review")
    assert c["kind"] == "employer_application_cap"


def test_classify_usage_limit():
    c = B.classify_text("You've hit your session limit, resets 12:40pm")
    assert c["kind"] == "usage_limit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_console_browser_health.py -q
```

Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement classifier**

Create `src/applypilot/fleet/console_browser_health.py`:

```python
"""Deterministic worker-log classification for fleet console health panels."""
from __future__ import annotations


_RULES = [
    ("browser_backend_crashed", "error", ("browser backend", "crashed")),
    ("browser_service_unavailable", "error", ("econnrefused",)),
    ("browser_service_unavailable", "error", ("browser_unavailable",)),
    ("browser_service_unavailable", "error", ("browser service", "not responding")),
    ("browser_server_unavailable", "error", ("browser_server_unavailable",)),
    ("captcha", "warn", ("captcha",)),
    ("captcha", "warn", ("hcaptcha",)),
    ("login_gate", "warn", ("auth_required",)),
    ("login_gate", "warn", ("login page",)),
    ("email_otp", "warn", ("verification code",)),
    ("employer_application_cap", "info", ("limit the number of applications",)),
    ("usage_limit", "warn", ("session limit",)),
    ("usage_limit", "warn", ("usage_limit",)),
    ("timeout", "warn", ("timeout",)),
    ("no_result_line", "warn", ("no_result_line",)),
]


def classify_text(text: str | None) -> dict:
    lower = (text or "").lower()
    for kind, severity, needles in _RULES:
        if all(n in lower for n in needles):
            return {"kind": kind, "severity": severity}
    return {"kind": "unknown", "severity": "info"}


def summarize_worker_logs(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    examples: dict[str, dict] = {}
    for row in rows:
        text = "\n".join(str(row.get(k) or "") for k in ("last_error", "recent_log"))
        cls = classify_text(text)
        kind = cls["kind"]
        if kind == "unknown":
            continue
        counts[kind] = counts.get(kind, 0) + 1
        examples.setdefault(kind, {
            "worker_id": row.get("worker_id"),
            "machine_owner": row.get("machine_owner"),
            "severity": cls["severity"],
        })
    return {"counts": counts, "examples": examples}
```

- [ ] **Step 4: Add browser health to diagnosis**

In `src/applypilot/fleet/console_diagnosis.py`, add:

```python
def browser_health(conn) -> dict:
    from applypilot.fleet.console_browser_health import summarize_worker_logs

    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_id, machine_owner, last_error, recent_log "
            "FROM worker_heartbeat WHERE role='apply' ORDER BY worker_id"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return summarize_worker_logs(rows)
```

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m pytest tests/test_console_browser_health.py tests/test_console_diagnosis.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/applypilot/fleet/console_browser_health.py src/applypilot/fleet/console_diagnosis.py tests/test_console_browser_health.py
git commit -m "Classify browser health for fleet console"
```

---

### Task 6: Diagnosis Endpoint And Recommendations

**Files:**
- Modify: `src/applypilot/fleet/console_diagnosis.py`
- Modify: `src/applypilot/fleet/console_app.py`
- Test: `tests/test_console_diagnosis.py`

- [ ] **Step 1: Add failing recommendation test**

Append to `tests/test_console_diagnosis.py`:

```python
def test_recommendation_for_dedup_blocked_queue(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(conn, url="https://boards.greenhouse.io/acme/jobs/3",
                        dedup_key="acme::pm")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::pm', 'Acme', 'https://already/applied')"
            )
        conn.commit()

        result = console_diagnosis.full_diagnosis(conn)

    rec = result["recommendations"][0]
    assert rec["code"] == "reconcile_dedup_blocked_queue"
    assert rec["action_type"] == "manual_runbook"
    assert "remediator" in rec["command"].lower() or "reconcile" in rec["command"].lower()


def test_recommendation_for_linkedin_canary_exhausted(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key) "
                "VALUES ('https://www.linkedin.com/jobs/view/4','Acme','Lead',"
                "'https://www.linkedin.com/jobs/view/4',8,'ats','queued','li-batch','acme::lead')"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, linkedin_canary_remaining=0 WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.full_diagnosis(conn)

    codes = {r["code"] for r in result["recommendations"]}
    assert "rearm_linkedin_canary" in codes
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py::test_recommendation_for_dedup_blocked_queue tests/test_console_diagnosis.py::test_recommendation_for_linkedin_canary_exhausted -q
```

Expected: FAIL because `full_diagnosis()` does not exist.

- [ ] **Step 3: Implement recommendations**

Add to `src/applypilot/fleet/console_diagnosis.py`:

```python
def recommendations_from(queue: dict, browser: dict) -> list[dict]:
    recs: list[dict] = []
    ats = queue["ats"]
    linkedin = queue["linkedin"]
    if ats["approved"] > 0 and ats["leaseable"] == 0 and ats["dedup_blocked"] == ats["approved"]:
        recs.append({
            "code": "reconcile_dedup_blocked_queue",
            "severity": "warn",
            "lane": "ats",
            "title": "Queued ATS rows are dedup-blocked",
            "reason": f"{ats['dedup_blocked']} approved queued ATS rows are already in applied_set.",
            "action_type": "manual_runbook",
            "command": "Run a read-only queue reconciliation report before mutating any rows.",
        })
    if linkedin["queued"] > 0 and linkedin["canary_exhausted"]:
        recs.append({
            "code": "rearm_linkedin_canary",
            "severity": "info",
            "lane": "linkedin",
            "title": "LinkedIn canary is exhausted",
            "reason": "LinkedIn has queued rows but linkedin_canary_remaining is zero.",
            "action_type": "manual_operator",
            "command": "Use the LinkedIn lane runbook to re-arm a small canary if you want LinkedIn active.",
        })
    if browser["counts"].get("browser_service_unavailable") or browser["counts"].get("browser_backend_crashed"):
        recs.append({
            "code": "restart_browser_backend",
            "severity": "warn",
            "lane": "ats",
            "title": "Browser backend failures detected",
            "reason": "Recent worker logs include browser backend crash or connection-refused failures.",
            "action_type": "manual_machine",
            "command": "Restart the affected machine's browser/apply worker stack, then verify heartbeat.",
        })
    if not recs:
        recs.append({
            "code": "no_immediate_action",
            "severity": "ok",
            "lane": "fleet",
            "title": "No immediate action required",
            "reason": "No high-priority console diagnosis fired.",
            "action_type": "none",
            "command": "",
        })
    return recs


def full_diagnosis(conn) -> dict:
    queue = queue_diagnosis(conn)
    browser = browser_health(conn)
    return {
        "queue": queue,
        "browser": browser,
        "recommendations": recommendations_from(queue, browser),
    }
```

- [ ] **Step 4: Add `/api/diagnosis` route**

In `src/applypilot/fleet/console_app.py`, add read-only route:

```python
if path == "/api/diagnosis":
    from applypilot.fleet import console_diagnosis
    conn = pgqueue.connect()
    try:
        self._send_json(200, console_diagnosis.full_diagnosis(conn))
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    return
```

- [ ] **Step 5: Fold small state into `/api/status`**

In `build_status()`, after existing `gate, queue = _gate_and_queue(conn)`, add:

```python
try:
    from applypilot.fleet import console_diagnosis
    fleet_diagnosis = console_diagnosis.queue_diagnosis(conn)
except Exception:
    try:
        conn.rollback()
    except Exception:
        pass
    fleet_diagnosis = None
```

In the returned dict, add:

```python
"fleet_diagnosis": fleet_diagnosis,
```

- [ ] **Step 6: Run tests**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py tests/test_console_token.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/applypilot/fleet/console_diagnosis.py src/applypilot/fleet/console_app.py tests/test_console_diagnosis.py
git commit -m "Add fleet console diagnosis endpoint"
```

---

### Task 7: Operational Rollups

**Files:**
- Modify: `src/applypilot/fleet/console_diagnosis.py`
- Test: `tests/test_console_diagnosis.py`

- [ ] **Step 1: Write failing operational rollup test**

Append to `tests/test_console_diagnosis.py`:

```python
def test_operational_rollups_include_machines_hosts_forecast_goals_and_workers(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, last_beat, current_agent, current_model) "
                "VALUES "
                "('m4-0','m4','100.69.68.103','apply','idle',now(),'codex','sonnet'), "
                "('m4-score-0','m4','0.0.0.0','compute','idle',now(),NULL,NULL), "
                "('m2-disc-0','m2','100.77.65.8','discovery','idle',now(),NULL,NULL)"
            )
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, "
                "dedup_key, target_host, apply_domain, worker_id, est_cost_usd, updated_at) "
                "VALUES "
                "('https://boards.greenhouse.io/acme/jobs/1','Acme','Engineer',"
                "'https://boards.greenhouse.io/acme/jobs/1/apply',8,'ats','applied','b',"
                "'acme::eng','boards.greenhouse.io','boards.greenhouse.io','m4-0',0.50,now()), "
                "('https://jobs.ashbyhq.com/beta/1','Beta','Analyst',"
                "'https://jobs.ashbyhq.com/beta/1/apply',8,'ats','failed','b',"
                "'beta::analyst','jobs.ashbyhq.com','jobs.ashbyhq.com','m4-0',0.25,now())"
            )
        conn.commit()

        result = console_diagnosis.operational_rollups(conn)

    assert result["machines"]["m4"]["workers"] == 2
    assert result["machines"]["m4"]["roles"]["apply"] == 1
    assert result["host_quality"][0]["host"] in {"boards.greenhouse.io", "jobs.ashbyhq.com"}
    assert result["throughput"]["applied_24h"] == 1
    assert result["daily_goal"]["configured"] is False
    assert result["worker_comparison"][0]["worker_id"] == "m4-0"
    assert result["freshness"]["last_apply_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py::test_operational_rollups_include_machines_hosts_forecast_goals_and_workers -q
```

Expected: FAIL because `operational_rollups()` does not exist.

- [ ] **Step 3: Implement operational rollups**

Add to `src/applypilot/fleet/console_diagnosis.py`:

```python
def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def operational_rollups(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_id, machine_owner, role, state, last_beat, current_agent, current_model "
            "FROM worker_heartbeat ORDER BY machine_owner NULLS LAST, worker_id"
        )
        worker_rows = cur.fetchall()
        cur.execute(
            "SELECT COALESCE(target_host, apply_domain, '(unknown)') AS host, "
            "COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE status='applied') AS applied, "
            "COUNT(*) FILTER (WHERE status='failed') AS failed, "
            "COUNT(*) FILTER (WHERE apply_status='challenge_pending') AS challenges "
            "FROM apply_queue GROUP BY 1 ORDER BY total DESC LIMIT 25"
        )
        host_rows = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE status='applied' AND updated_at > now() - interval '1 hour') AS applied_1h, "
            "COUNT(*) FILTER (WHERE status='applied' AND updated_at > now() - interval '24 hours') AS applied_24h, "
            "MAX(updated_at) FILTER (WHERE status='applied') AS last_apply_at "
            "FROM apply_queue"
        )
        throughput = cur.fetchone() or {}
        cur.execute(
            "SELECT worker_id, COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE status='applied') AS applied, "
            "COUNT(*) FILTER (WHERE status='failed') AS failed, "
            "COUNT(*) FILTER (WHERE status='crash_unconfirmed') AS crash_unconfirmed, "
            "COALESCE(SUM(est_cost_usd),0) AS cost_usd "
            "FROM apply_queue WHERE worker_id IS NOT NULL "
            "GROUP BY worker_id ORDER BY applied DESC, total DESC, worker_id LIMIT 50"
        )
        worker_cmp = cur.fetchall()
    conn.rollback()

    machines: dict[str, dict] = {}
    for row in worker_rows:
        machine = row["machine_owner"] or "(unknown)"
        m = machines.setdefault(machine, {
            "workers": 0,
            "roles": {},
            "last_beat": None,
            "states": {},
        })
        m["workers"] += 1
        m["roles"][row["role"]] = m["roles"].get(row["role"], 0) + 1
        m["states"][row["state"]] = m["states"].get(row["state"], 0) + 1
        if m["last_beat"] is None or row["last_beat"] > m["last_beat"]:
            m["last_beat"] = row["last_beat"]
    for machine in machines.values():
        machine["last_beat"] = _iso(machine["last_beat"])

    applied_1h = int(throughput.get("applied_1h") or 0)
    applied_24h = int(throughput.get("applied_24h") or 0)
    return {
        "machines": machines,
        "host_quality": [{
            "host": r["host"],
            "total": int(r["total"] or 0),
            "applied": int(r["applied"] or 0),
            "failed": int(r["failed"] or 0),
            "challenges": int(r["challenges"] or 0),
        } for r in host_rows],
        "throughput": {
            "applied_1h": applied_1h,
            "applied_24h": applied_24h,
            "estimated_applies_per_hour": applied_1h if applied_1h > 0 else round(applied_24h / 24, 2),
        },
        "daily_goal": {
            "configured": False,
            "target": None,
            "applied_today": applied_24h,
            "remaining": None,
        },
        "worker_comparison": [{
            "worker_id": r["worker_id"],
            "total": int(r["total"] or 0),
            "applied": int(r["applied"] or 0),
            "failed": int(r["failed"] or 0),
            "crash_unconfirmed": int(r["crash_unconfirmed"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
        } for r in worker_cmp],
        "freshness": {
            "last_apply_at": _iso(throughput.get("last_apply_at")),
        },
    }
```

Update `full_diagnosis()`:

```python
def full_diagnosis(conn) -> dict:
    queue = queue_diagnosis(conn)
    browser = browser_health(conn)
    rollups = operational_rollups(conn)
    return {
        "queue": queue,
        "browser": browser,
        "rollups": rollups,
        "recommendations": recommendations_from(queue, browser),
    }
```

- [ ] **Step 4: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/fleet/console_diagnosis.py tests/test_console_diagnosis.py
git commit -m "Add fleet console operational rollups"
```

---

### Task 8: Operator Audit For Existing Console Actions

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `src/applypilot/fleet/console_app.py`
- Create: `tests/test_console_audit.py`

- [ ] **Step 1: Write failing audit tests**

Create `tests/test_console_audit.py`:

```python
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


def test_console_action_audit_records_success_without_secrets(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        ok, msg = console_app.run_action({"action": "pause"})
        assert ok is True
        with conn.cursor() as cur:
            cur.execute("SELECT action, ok, message FROM fleet_console_audit ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()

    assert row["action"] == "pause"
    assert row["ok"] is True
    assert "dsn" not in (row["message"] or "").lower()
    assert "token" not in (row["message"] or "").lower()


def test_console_action_audit_records_unknown_action(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    ok, msg = console_app.run_action({"action": "does_not_exist"})
    assert ok is False
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT action, ok, message FROM fleet_console_audit ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()

    assert row["action"] == "does_not_exist"
    assert row["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_console_audit.py -q
```

Expected: FAIL because `fleet_console_audit` does not exist and `run_action()` does not audit.

- [ ] **Step 3: Add audit table**

Append to `src/applypilot/fleet/schema_v3.sql`:

```sql
-- Console operator action audit. This records only action metadata and scrubbed
-- result messages. It never stores DSNs, tokens, prompts, resumes, or raw logs.
CREATE TABLE IF NOT EXISTS fleet_console_audit (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    actor TEXT,
    lane TEXT,
    target TEXT,
    message TEXT,
    ok BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_console_audit_created ON fleet_console_audit (created_at DESC);
```

- [ ] **Step 4: Include audit table in test truncation**

Modify `tests/conftest.py` `_V3_TABLES` to include:

```python
"fleet_console_audit",
```

- [ ] **Step 5: Implement audit helper**

In `src/applypilot/fleet/console_app.py`, add:

```python
def _audit_action(conn, *, action: str, ok: bool, message: str,
                  lane: str | None = None, target: str | None = None) -> None:
    safe_message = _scrub(message or "")[:500]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_console_audit (action, ok, message, lane, target) "
            "VALUES (%s,%s,%s,%s,%s)",
            (_scrub(action or "unknown")[:120], bool(ok), safe_message, lane, target),
        )
    conn.commit()
```

Modify `run_action()` so unknown action audits too:

```python
if fn is None:
    conn = pgqueue.connect()
    try:
        _audit_action(conn, action=str(action), ok=False, message="unknown action")
    finally:
        conn.close()
    return False, "unknown action"
```

Inside the normal action path, after `result` is converted to `(ok, message)`, call:

```python
ok, message = result if isinstance(result, tuple) else (True, result)
_audit_action(conn, action=action, ok=bool(ok), message=str(message),
              lane=body.get("lane"), target=body.get("url") or body.get("host"))
return ok, message
```

- [ ] **Step 6: Run tests**

Run:

```powershell
python -m pytest tests/test_console_audit.py tests/test_console_token.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/console_app.py tests/conftest.py tests/test_console_audit.py
git commit -m "Audit fleet console actions"
```

---

### Task 9: Dashboard UI Makeover

**Files:**
- Modify: `src/applypilot/fleet/console_app.py`
- Modify: `tests/test_console_challenges_page.py`
- Create: `tests/test_console_operations_page.py`

- [ ] **Step 1: Write failing page smoke tests**

Create `tests/test_console_operations_page.py`:

```python
from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from applypilot.fleet import console_app


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-ops")
    monkeypatch.setattr(console_app, "build_status", lambda: {
        "now": "2026-07-05T00:00:00+00:00",
        "gate": {"paused": False, "should_halt": False, "leasable": 0, "spent_usd": 0, "spend_cap_usd": 0},
        "queue": {"apply": {"queued": 0}},
        "workers": [],
        "recent": [],
        "challenges": 0,
        "linkedin": {"queued": 0, "applied": 0, "canary_enabled": False, "halted": False},
        "doctor": None,
        "discovery": None,
        "deadman_alert": None,
        "deadman_alert_at": None,
        "fleet_diagnosis": {"state": {"code": "idle_no_leasable_jobs", "reason": "No leaseable ATS jobs are available."}},
    })

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_index_contains_operations_sections(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    for text in [
        "Fleet State",
        "Why Not Applying",
        "Agent Routing",
        "Machine Health",
        "Browser Health",
        "Queue Funnel",
        "Safety Rails",
        "Recommended Next Action",
        "Audit Log",
    ]:
        assert text in html

    assert "/api/diagnosis" in html
    assert "/api/agents" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/test_console_operations_page.py -q
```

Expected: FAIL because the page lacks the new sections.

- [ ] **Step 3: Add frontend sections**

In `_INDEX_HTML`, replace the first cards and major sections with code-native sections using these ids:

```html
<section id="fleetState" class="band primary">
  <h2>Fleet State</h2>
  <div id="stateHeadline" class="headline">Loading</div>
  <div id="stateReason" class="sub"></div>
  <div id="nextAction" class="actionline"></div>
</section>

<section id="safetyRails">
  <h2>Safety Rails</h2>
  <div class="metric-grid" id="safetyGrid"></div>
</section>

<section id="whyNotApplying">
  <h2>Why Not Applying</h2>
  <div id="whyBody" class="diagnosis-grid"></div>
</section>

<section id="agentRouting">
  <h2>Agent Routing</h2>
  <div id="agentVerdict" class="sub"></div>
  <table><thead><tr><th>Worker</th><th>Machine</th><th>Agent</th><th>Model</th><th>Chain</th><th>Switch</th></tr></thead>
    <tbody id="agentWorkers"><tr><td colspan="6" class="mut">loading</td></tr></tbody></table>
</section>

<section id="machineHealth">
  <h2>Machine Health</h2>
  <div id="machineMap" class="machine-grid"></div>
</section>

<section id="browserHealth">
  <h2>Browser Health</h2>
  <div id="browserBody" class="diagnosis-grid"></div>
</section>

<section id="queueFunnel">
  <h2>Queue Funnel</h2>
  <div id="funnelBody" class="funnel"></div>
</section>

<section id="auditLog">
  <h2>Audit Log</h2>
  <table><thead><tr><th>Time</th><th>Action</th><th>Result</th><th>Message</th></tr></thead>
    <tbody id="auditRows"><tr><td colspan="4" class="mut">audit endpoint not loaded</td></tr></tbody></table>
</section>
```

Keep the existing Controls, Challenges, Workers, Discovery, Outcomes, Doctor, and LinkedIn sections, but move them below the new overview sections.

- [ ] **Step 4: Add rendering functions**

In `_INDEX_HTML` script, add:

```javascript
async function loadDiagnosis(){
  try{
    const r = await fetch("/api/diagnosis", {cache:"no-store"});
    if(!r.ok) return;
    renderDiagnosis(await r.json());
  }catch(e){}
}

async function loadAgents(){
  try{
    const r = await fetch("/api/agents", {cache:"no-store"});
    if(!r.ok) return;
    renderAgents(await r.json());
  }catch(e){}
}

function renderDiagnosis(d){
  const q = d.queue || {};
  const ats = q.ats || {};
  const li = q.linkedin || {};
  const roll = d.rollups || {};
  const state = q.state || {};
  document.getElementById("stateHeadline").textContent = state.code || "unknown";
  document.getElementById("stateReason").textContent = state.reason || "";
  const recs = d.recommendations || [];
  document.getElementById("nextAction").textContent = recs.length ? recs[0].title + " — " + recs[0].reason : "No recommendation";
  document.getElementById("whyBody").innerHTML = [
    ["ATS queued", ats.queued],
    ["ATS approved", ats.approved],
    ["ATS leaseable", ats.leaseable],
    ["ATS dedup-blocked", ats.dedup_blocked],
    ["LinkedIn queued", li.queued],
    ["LinkedIn leaseable", li.leaseable],
    ["LinkedIn canary exhausted", li.canary_exhausted ? "yes" : "no"]
  ].map(([k,v]) => '<div class="mini"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join("");
  const browser = d.browser || {};
  const counts = browser.counts || {};
  const keys = Object.keys(counts);
  document.getElementById("browserBody").innerHTML = keys.length
    ? keys.map(k => '<div class="mini"><span>'+esc(k)+'</span><b>'+esc(counts[k])+'</b></div>').join("")
    : '<div class="mut">no classified browser failures</div>';
  document.getElementById("funnelBody").innerHTML = [
    ["Queued", ats.queued],
    ["Approved", ats.approved],
    ["Leaseable", ats.leaseable],
    ["Leased", ats.leased],
    ["Applied", ats.applied],
    ["Failed", ats.failed],
    ["Crash unconfirmed", ats.crash_unconfirmed]
  ].map(([k,v]) => '<div class="fstep"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join("");
  const machines = roll.machines || {};
  document.getElementById("machineMap").innerHTML = Object.keys(machines).length
    ? Object.keys(machines).map(k => '<div class="mini"><span>'+esc(k)+'</span><b>'+
      esc(machines[k].workers)+'</b><small class="mut"> workers</small></div>').join("")
    : '<div class="mut">no machine heartbeats</div>';
}

function renderAgents(d){
  const verdict = d.verdict || {};
  document.getElementById("agentVerdict").textContent = (verdict.code || "unknown") + " — " + (verdict.reason || "");
  const rows = d.workers || [];
  const body = document.getElementById("agentWorkers");
  body.innerHTML = rows.length ? rows.map(w =>
    '<tr><td>'+esc(w.worker_id)+'</td><td>'+esc(w.machine_owner||"")+'</td><td>'+
    esc(w.current_agent||"unknown")+'</td><td>'+esc(w.current_model||"unknown")+'</td><td>'+
    esc(w.agent_chain||"")+'</td><td>'+esc(w.last_agent_switch_reason||"")+'</td></tr>'
  ).join("") : '<tr><td colspan="6" class="mut">no apply worker agent telemetry</td></tr>';
}
```

Call:

```javascript
loadDiagnosis();
setInterval(loadDiagnosis, 15000);
loadAgents();
setInterval(loadAgents, 15000);
```

- [ ] **Step 5: Add compact operator CSS**

Add CSS classes:

```css
.band.primary{border-left:4px solid var(--blue)}
.headline{font-size:24px;font-weight:750;margin:6px 0}
.actionline{margin-top:10px;color:var(--fg);font-weight:600}
.metric-grid,.diagnosis-grid,.machine-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.mini{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px}
.mini span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
.mini b{display:block;font-size:20px;margin-top:4px}
.funnel{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.fstep{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px;min-height:70px}
.fstep span{display:block;color:var(--muted);font-size:11px}
.fstep b{font-size:22px}
```

- [ ] **Step 6: Run page tests**

Run:

```powershell
python -m pytest tests/test_console_operations_page.py tests/test_console_challenges_page.py tests/test_console_token.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/applypilot/fleet/console_app.py tests/test_console_operations_page.py tests/test_console_challenges_page.py
git commit -m "Redesign fleet console operations dashboard"
```

---

### Task 10: Final Verification Pass

**Files:**
- No planned source edits unless verification finds a defect.

- [ ] **Step 1: Run targeted backend tests**

Run:

```powershell
python -m pytest tests/test_console_diagnosis.py tests/test_console_agents.py tests/test_console_browser_health.py tests/test_console_audit.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run existing console tests**

Run:

```powershell
python -m pytest tests/test_console_challenges_api.py tests/test_console_challenges_page.py tests/test_console_token.py tests/test_fleet_console_doctor.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run worker/agent tests**

Run:

```powershell
python -m pytest tests/test_agent_budget.py tests/test_apply_worker_switching.py tests/test_fleet_v3_worker.py tests/test_fleet_v3_schema.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Start console for read-only local smoke**

Run:

```powershell
.\run-fleet-console.ps1
```

Expected:

```text
ApplyPilot Fleet Console (LAN-only)
Open this URL on any machine on your LAN:
```

Do not click mutating controls during smoke verification.

- [ ] **Step 5: Verify read endpoints in browser or curl**

Open the LAN URL printed by the script. Verify:

- first viewport shows Fleet State
- Why Not Applying shows queued/approved/leaseable/dedup-blocked
- Agent Routing shows current agent/model or an unknown telemetry empty state
- Browser Health renders classified counts or empty state
- Challenges still render
- LinkedIn still has no write controls

If using PowerShell instead of browser:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/status
Invoke-RestMethod http://127.0.0.1:8787/api/diagnosis
Invoke-RestMethod http://127.0.0.1:8787/api/agents
```

Expected: all return JSON without triggering live fleet mutations.

- [ ] **Step 6: Confirm git state**

Run:

```powershell
git status --short
```

Expected: only intentional files changed, with unrelated pre-existing worktree edits left untouched.

- [ ] **Step 7: Commit verification fixes if any**

If Step 1-6 required fixes, commit them:

```powershell
git add <fixed-files>
git commit -m "Stabilize fleet operations console verification"
```

If no fixes were needed, do not create an empty commit.

---

## Self-Review Notes

- Spec coverage:
  - Fleet state summary: Task 1, Task 6, Task 8.
  - Why-not-applying: Task 1, Task 6, Task 8.
  - Agent/model routing and dynamic switching: Task 2, Task 3, Task 4, Task 8.
  - Machine health: Task 8 renders the section; follow-on data can use existing worker grouping in the same endpoint shape.
  - Browser backend health: Task 5, Task 6, Task 8.
  - Challenge workbench: existing challenge code preserved and covered in Task 8.
  - Queue funnel and safety rails: Task 1, Task 6, Task 8.
  - Recommendations: Task 6.
  - Recent applies, failure clusters, discovery, compute, outcomes, Doctor: existing endpoints remain and are repositioned in Task 8.
  - Audit log: Task 7.
- Forecast, daily goals, worker comparison, freshness: Task 7 produces first-pass read-only calculations and Task 9 renders them.
- No new LinkedIn apply/scrape/resume controls are planned.
- No live SQLite brain access is planned.
- No live fleet worker starts or mutating pipeline commands are part of implementation verification.
