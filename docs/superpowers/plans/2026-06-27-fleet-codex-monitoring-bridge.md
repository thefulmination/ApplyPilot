# Fleet Codex Monitoring Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local FastMCP stdio server (`applypilot-fleet-codex-bridge`) that exposes exactly 8 tools (5 read + 3 bounded-action) so the owner can watch and minimally steer the fleet from Codex.

**Architecture:** One new module `src/applypilot/fleet/codex_bridge.py` holding a module-scope `FastMCP` app, a `_with_conn` connection-per-call helper, and 8 `@mcp.tool()`-decorated plain sync functions. Read tools reuse `heartbeat.dashboard_snapshot` / `monitor.build_health_report` plus a couple of direct read-only queries; action tools delegate to the existing `monitor.MonitorActions`. A small foundation change adds a `manual=` one-shot path to `heartbeat.quarantine_job` (and `MonitorActions.quarantine` uses it) so deliberate quarantines never pollute `crash_count`.

**Tech Stack:** Python 3.11+, the official `mcp` SDK (`mcp.server.fastmcp.FastMCP`) over stdio, psycopg 3 against the v3 coordination Postgres, tested against the disposable `applypilot-pgtest` Postgres via the `fleet_db` fixture (`tests/conftest.py`), same as the compute/discovery/watchdog lanes.

## Global Constraints

- **Repo / cwd:** `C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`. Run python as `.conda-env/python.exe`. Test command: `.conda-env/python.exe -m pytest <file> -q`.
- **New dependency:** `mcp>=1.28,<2` in `pyproject.toml` (cap below a FastMCP-3.0 reorg). Install into the editable env: `.conda-env/python.exe -m pip install "mcp>=1.28,<2"`. No new test dependency — the registry test uses the **sync** `mcp._tool_manager.list_tools()` path (NOT `await mcp.list_tools()`), so pytest-asyncio is not required.
- **The safety gate is the registry test** (Task 6): the live `@mcp.tool()` set must be EXACTLY the 8 names `{fleet_status, health_report, recent_results, challenges, caps, restart_worker, pause_scope, quarantine_job}` — no denied op, no generic SQL tool. This is a hard merge gate.
- **No DB access at import or in `main()`** before `mcp.run()`. All DB access happens inside a tool via `_with_conn`. Importing the module with `FLEET_PG_DSN` unset must NOT raise.
- **DSN discipline:** `_with_conn` reads `os.environ["FLEET_PG_DSN"]` ITSELF (returns a structured error if missing/empty) and passes it positionally to `connect(dsn)`. Do NOT call `pgqueue.connect()` with no arg — its `get_dsn()` falls back to `DATABASE_URL`, which is set on the home box (Railway), and would silently hit the wrong DB.
- **Connection:** use `from applypilot.apply.pgqueue import connect` (it sets `row_factory=dict_row`, which every read primitive relies on with `row["col"]` indexing). Import only the `connect` symbol — NOT the whole `pgqueue` module (which also binds `set_paused`/`set_spend_cap`). `_with_conn` rolls back + closes in `finally` and never commits (action primitives commit themselves).
- **Uniform return type:** EVERY tool returns `dict[str, Any]` so the `{"error": …}` sentinel never collides with a narrow `-> str`/`-> list` annotation, and FastMCP serializes datetime/Decimal cleanly. Tools that conceptually return text/lists wrap them: `health_report → {"report": str}`, `recent_results → {"results": [...]}`, `challenges → {"challenges": [...]}`.
- **Tools are plain sync functions**; `@mcp.tool()` returns the function unchanged, so unit tests import and call the module-level functions directly. Every tool param and the return is type-annotated (FastMCP infers the JSON-Schema from annotations).
- **Commit discipline:** `git add <exact paths>` ONLY — NEVER `git add -A`. The 7 user-dirty files (`run-applypilot.ps1`, `src/applypilot/discovery/jobspy.py`, `src/applypilot/pipeline.py`, `src/applypilot/scoring/cover_letter.py`, `src/applypilot/scoring/tailor.py`, `tests/test_discovery_scheduler.py`, `tests/test_generation_workers.py`) + untracked user files stay untouched. Do NOT push (the orchestrator pushes after the final whole-branch review).

## File Structure

- Create `src/applypilot/fleet/codex_bridge.py` — the FastMCP app, `_with_conn`, the 8 tools, `main()`.
- Modify `src/applypilot/fleet/heartbeat.py` — add `manual: bool = False` to `quarantine_job`.
- Modify `src/applypilot/fleet/monitor.py` — `MonitorActions.quarantine` passes `manual=True`.
- Modify `pyproject.toml` — `mcp>=1.28,<2` dep + `applypilot-fleet-codex-bridge` script.
- Create `tests/test_codex_bridge.py` — the bridge's read/action/registry/e2e tests.
- Modify `tests/test_fleet_monitor.py` — the `manual=` quarantine tests (foundation change consumer).
- Modify `docs/...` (a short README/runbook) — the Codex config snippet (Task 7).

---

### Task 1: Dependency + module scaffold + `_with_conn` + `main`

**Files:**
- Modify: `pyproject.toml`
- Create: `src/applypilot/fleet/codex_bridge.py`
- Test: `tests/test_codex_bridge.py`

**Interfaces:**
- Produces: the module `applypilot.fleet.codex_bridge` with `mcp` (a `FastMCP`), `_with_conn(fn)` (reads `FLEET_PG_DSN`, connects via `connect`, runs `fn(conn)`, returns its result or `{"error": …}`, rolls back + closes in `finally`), and `main()` (calls `mcp.run()`). Later tasks register tools on `mcp` and reuse `_with_conn`.

- [ ] **Step 1: Install the dependency.**

Run: `.conda-env/python.exe -m pip install "mcp>=1.28,<2"`
Then confirm: `.conda-env/python.exe -c "from mcp.server.fastmcp import FastMCP; print('mcp ok')"` → `mcp ok`

- [ ] **Step 2: Add the dependency + entrypoint to `pyproject.toml`.**

In `[project] dependencies`, add `"mcp>=1.28,<2"` alongside the existing deps. In `[project.scripts]`, add (alongside the existing `applypilot-fleet-*` scripts, removing/reordering none):

```toml
applypilot-fleet-codex-bridge = "applypilot.fleet.codex_bridge:main"
```

- [ ] **Step 3: Write the failing test**

```python
# tests/test_codex_bridge.py
import os
import importlib
import pytest
from applypilot.apply import pgqueue


def test_module_imports_without_dsn(monkeypatch):
    # No DB access at import time: importing with FLEET_PG_DSN unset must not raise.
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    mod = importlib.import_module("applypilot.fleet.codex_bridge")
    importlib.reload(mod)
    assert hasattr(mod, "mcp") and hasattr(mod, "main") and hasattr(mod, "_with_conn")


def test_with_conn_errors_when_dsn_unset(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert "error" in out and "FLEET_PG_DSN" in out["error"]


def test_with_conn_errors_on_unreachable_db(monkeypatch):
    # A syntactically-valid but dead DSN returns a structured error, not a raise.
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=1")
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert "error" in out


def test_with_conn_runs_fn_and_closes(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert out == {"ok": True}
```

- [ ] **Step 4: Run it, expect FAIL** (`ModuleNotFoundError: applypilot.fleet.codex_bridge`).

Run: `.conda-env/python.exe -m pytest tests/test_codex_bridge.py -q`

- [ ] **Step 5: Implement** `src/applypilot/fleet/codex_bridge.py`

```python
"""Codex monitoring bridge (spec 2026-06-27) -- a local FastMCP stdio server that
surfaces the fleet's read-only telemetry and three bounded-safe actions into Codex.

SAFETY: the guarantee is the tool REGISTRY -- exactly 8 functions are @mcp.tool().
Action tools delegate only to monitor.MonitorActions (whose surface is restart /
quarantine / pause, no apply/unpause/cap/challenge-resolve). We import only the
`connect` symbol from apply.pgqueue (not the module) so set_paused/set_spend_cap are
not even bound here. No DB access happens at import or in main(); every tool goes
through _with_conn, which reads FLEET_PG_DSN itself and rolls back + closes per call.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from applypilot.apply.pgqueue import connect
from applypilot.fleet import heartbeat, monitor

mcp = FastMCP("applypilot-fleet")


def _with_conn(fn: Callable[[Any], dict]) -> dict:
    """Open a short-lived dict_row connection from FLEET_PG_DSN, run fn(conn), and
    return its dict result (or a structured {"error": ...}). Rolls back (read-only
    discipline; no-op after an action's own commit) and closes in finally. Reads the
    DSN directly -- never connect() with no arg (that falls back to DATABASE_URL)."""
    dsn = os.environ.get("FLEET_PG_DSN")
    if not dsn:
        return {"error": "FLEET_PG_DSN is not set; set it in the Codex MCP env block"}
    try:
        conn = connect(dsn)
    except Exception as e:  # RuntimeError (no DSN) / OperationalError (dead DB) / etc.
        return {"error": f"could not connect: {e}"}
    try:
        return fn(conn)
    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def main() -> int:  # pragma: no cover - stdio server loop, not unit-testable
    """Entry point: run the FastMCP server over stdio. No DB access here."""
    mcp.run()
    return 0
```

- [ ] **Step 6: Run it, expect PASS** (4 passed).

Run: `.conda-env/python.exe -m pytest tests/test_codex_bridge.py -q`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/applypilot/fleet/codex_bridge.py tests/test_codex_bridge.py
git commit -m "feat(fleet): Codex bridge scaffold — FastMCP app, _with_conn, entrypoint"
```

---

### Task 2: Read tools — `fleet_status`, `caps`, `health_report`

**Files:**
- Modify: `src/applypilot/fleet/codex_bridge.py`
- Test: `tests/test_codex_bridge.py`

**Interfaces:**
- Consumes: `heartbeat.dashboard_snapshot(conn) -> dict`, `monitor.build_health_report(snapshot, *, captcha_threshold=0.4, cost_cap_total=None) -> str`, `_with_conn`.
- Produces: tools `fleet_status() -> dict[str, Any]`, `caps() -> dict[str, Any]`, `health_report() -> dict[str, Any]` registered on `mcp`.

- [ ] **Step 1: Write the failing test**

```python
def _seed_caps(conn):
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=FALSE, cost_cap_daily_usd=10, cost_cap_total_usd=100 WHERE id=1")
        cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (3.0, now())")
    conn.commit()


def test_fleet_status_returns_snapshot(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    out = codex_bridge.fleet_status()
    # dashboard_snapshot keys
    for k in ("machines", "governor", "queue_depth", "captcha_backlog", "quarantine", "spend_today"):
        assert k in out


def test_caps_returns_caps_and_spend(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn:
        _seed_caps(conn)
    out = codex_bridge.caps()
    assert out["paused"] is False
    assert float(out["cost_cap_daily_usd"]) == 10.0
    assert float(out["cost_cap_total_usd"]) == 100.0
    assert float(out["spend_today"]) == 3.0
    assert float(out["spend_total"]) == 3.0


def test_health_report_returns_text(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn:
        _seed_caps(conn)
    out = codex_bridge.health_report()
    assert "report" in out and "NEEDS YOUR DECISION" in out["report"]
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: fleet_status`).

- [ ] **Step 3: Implement** — append to `codex_bridge.py`:

```python
@mcp.tool()
def fleet_status() -> dict[str, Any]:
    """Fleet health rollup: machines, breaker states, queue depths, captcha backlog,
    quarantine count, 24h spend."""
    return _with_conn(lambda conn: heartbeat.dashboard_snapshot(conn))


def _read_caps(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT paused, cost_cap_daily_usd, cost_cap_total_usd FROM fleet_config WHERE id=1")
        cfg = cur.fetchone() or {}
        cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= now() - interval '24 hours'")
        spend_today = float(cur.fetchone()["s"])
        cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage")
        spend_total = float(cur.fetchone()["s"])
    return {
        "paused": cfg.get("paused"),
        "cost_cap_daily_usd": float(cfg.get("cost_cap_daily_usd") or 0),
        "cost_cap_total_usd": float(cfg.get("cost_cap_total_usd") or 0),
        "spend_today": spend_today,
        "spend_total": spend_total,
    }


@mcp.tool()
def caps() -> dict[str, Any]:
    """Cost caps + spend: paused flag, daily/total caps, 24h spend, all-time spend."""
    return _with_conn(_read_caps)


@mcp.tool()
def health_report() -> dict[str, Any]:
    """The text health report (incl. a NEEDS YOUR DECISION anomaly section). The 24h
    spend is compared against the DAILY cap (apples-to-apples)."""
    def _build(conn):
        snap = heartbeat.dashboard_snapshot(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT cost_cap_daily_usd FROM fleet_config WHERE id=1")
            row = cur.fetchone()
        daily = float((row or {}).get("cost_cap_daily_usd") or 0)
        text = monitor.build_health_report(snap, captcha_threshold=0.4, cost_cap_total=daily)
        return {"report": text}
    return _with_conn(_build)
```

- [ ] **Step 4: Run it, expect PASS** (7 passed total).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/codex_bridge.py tests/test_codex_bridge.py
git commit -m "feat(fleet): Codex bridge read tools — fleet_status, caps, health_report"
```

---

### Task 3: Read tools — `recent_results`, `challenges`

**Files:**
- Modify: `src/applypilot/fleet/codex_bridge.py`
- Test: `tests/test_codex_bridge.py`

**Interfaces:**
- Consumes: `_with_conn`.
- Produces: tools `recent_results(limit: int = 20) -> dict[str, Any]` (key `"results"` → merged chronological list of normalized rows) and `challenges() -> dict[str, Any]` (key `"challenges"` → open auth_challenge rows).
- Normalized row: `{"lane": "apply"|"compute", "url": str, "status": str, "finished_at": <updated_at iso str>, "detail": {...}}` where apply `detail = {"company", "title", "apply_error"}` and compute `detail = {"task", "cost"}`.

- [ ] **Step 1: Write the failing test**

```python
def test_recent_results_merges_lanes_newest_first(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # an apply terminal row (older) and a compute terminal row (newer)
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, company, title, apply_error, updated_at) "
                    "VALUES ('a1','http://x','5','failed','Acme','COS','form_error', now() - interval '2 min')")
        cur.execute("INSERT INTO compute_queue (url, task, status, est_cost_usd, updated_at) "
                    "VALUES ('c1','score','done', 0.01, now())")
        conn.commit()
    out = codex_bridge.recent_results(limit=10)
    rows = out["results"]
    assert [r["lane"] for r in rows] == ["compute", "apply"]   # newest-first
    apply_row = next(r for r in rows if r["lane"] == "apply")
    assert apply_row["url"] == "a1" and apply_row["status"] == "failed"
    assert apply_row["detail"]["apply_error"] == "form_error"
    compute_row = next(r for r in rows if r["lane"] == "compute")
    assert compute_row["detail"]["task"] == "score"


def test_recent_results_caps_limit(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        for i in range(5):
            cur.execute("INSERT INTO compute_queue (url, task, status, updated_at) "
                        "VALUES (%s,'score','done', now())", (f"c{i}",))
        conn.commit()
    out = codex_bridge.recent_results(limit=3)
    assert len(out["results"]) == 3


def test_challenges_only_unresolved(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO auth_challenge (url, worker_id, kind, route) VALUES ('u1','w','captcha','offsite')")
        cur.execute("INSERT INTO auth_challenge (url, worker_id, kind, route, resolved_at) "
                    "VALUES ('u2','w','captcha','offsite', now())")
        conn.commit()
    out = codex_bridge.challenges()
    urls = [c["url"] for c in out["challenges"]]
    assert "u1" in urls and "u2" not in urls
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: recent_results`).

- [ ] **Step 3: Implement** — append to `codex_bridge.py`:

```python
def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _recent_results(conn, limit: int) -> dict[str, Any]:
    n = max(1, min(int(limit), 100))
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, status, updated_at, company, title, apply_error FROM apply_queue "
            "WHERE status IN ('applied','failed','blocked','crash_unconfirmed') "
            "ORDER BY updated_at DESC LIMIT %s", (n,))
        for r in cur.fetchall():
            rows.append({"lane": "apply", "url": r["url"], "status": r["status"],
                         "finished_at": _iso(r["updated_at"]),
                         "detail": {"company": r["company"], "title": r["title"],
                                    "apply_error": r["apply_error"]}})
        cur.execute(
            "SELECT url, status, updated_at, task, est_cost_usd FROM compute_queue "
            "WHERE status IN ('done','failed','quarantined') "
            "ORDER BY updated_at DESC LIMIT %s", (n,))
        for r in cur.fetchall():
            rows.append({"lane": "compute", "url": r["url"], "status": r["status"],
                         "finished_at": _iso(r["updated_at"]),
                         "detail": {"task": r["task"], "cost": float(r["est_cost_usd"] or 0)}})
    rows.sort(key=lambda x: x["finished_at"] or "", reverse=True)
    return {"results": rows[:n]}


@mcp.tool()
def recent_results(limit: int = 20) -> dict[str, Any]:
    """The most recent terminal fleet events (apply + compute) merged newest-first,
    each row carrying a lane-specific structured detail dict. limit capped at 100."""
    return _with_conn(lambda conn: _recent_results(conn, limit))


def _challenges(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, worker_id, machine_owner, kind, route, raised_at "
                    "FROM auth_challenge WHERE resolved_at IS NULL ORDER BY raised_at DESC")
        out = [{**r, "raised_at": _iso(r["raised_at"])} for r in cur.fetchall()]
    return {"challenges": out}


@mcp.tool()
def challenges() -> dict[str, Any]:
    """Open (unresolved) auth challenges — the captcha backlog detail."""
    return _with_conn(_challenges)
```

> NOTE: confirm `auth_challenge` has a `raised_at` column (the dashboard_snapshot counts it via `resolved_at IS NULL`). If the timestamp column is named differently, match it in the SELECT + test.

- [ ] **Step 4: Run it, expect PASS** (10 passed total).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/codex_bridge.py tests/test_codex_bridge.py
git commit -m "feat(fleet): Codex bridge read tools — recent_results (merged) + challenges"
```

---

### Task 4: Foundation change — `manual=` one-shot quarantine

**Files:**
- Modify: `src/applypilot/fleet/heartbeat.py` (`quarantine_job`)
- Modify: `src/applypilot/fleet/monitor.py` (`MonitorActions.quarantine`)
- Test: `tests/test_fleet_monitor.py`

**Interfaces:**
- Produces: `heartbeat.quarantine_job(conn, url, *, worker, reason, threshold=3, commit=True, manual=False) -> bool`. With `manual=True`: pull the job immediately (set `quarantined_at=now()`), tag `reason` with a `manual:` prefix, do NOT increment `crash_count`; return True only on the newly-quarantined transition, False if already quarantined. `MonitorActions.quarantine(url, *, worker, reason) -> bool` now passes `manual=True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_monitor.py (add)
from applypilot.fleet import heartbeat


def test_quarantine_manual_one_shot_no_crash_count(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        newly = heartbeat.quarantine_job(conn, "j1", worker="w", reason="bad", manual=True)
        assert newly is True
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count, quarantined_at, reason FROM poison_jobs WHERE url='j1'")
            row = cur.fetchone()
        assert row["crash_count"] == 0          # one-shot did NOT bump the strike counter
        assert row["quarantined_at"] is not None  # pulled immediately
        assert row["reason"].startswith("manual:")
        # second manual call: already quarantined -> False, still no crash_count bump
        again = heartbeat.quarantine_job(conn, "j1", worker="w", reason="bad", manual=True)
        assert again is False
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count FROM poison_jobs WHERE url='j1'")
            assert cur.fetchone()["crash_count"] == 0


def test_quarantine_default_still_strikes(fleet_db):
    # Regression: the default (manual=False) crash-strike path is unchanged.
    with pgqueue.connect(fleet_db) as conn:
        assert heartbeat.quarantine_job(conn, "j2", worker="w", reason="crash", threshold=3) is False  # strike 1
        assert heartbeat.quarantine_job(conn, "j2", worker="w", reason="crash", threshold=3) is False  # strike 2
        assert heartbeat.quarantine_job(conn, "j2", worker="w", reason="crash", threshold=3) is True   # strike 3 -> pulled
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count FROM poison_jobs WHERE url='j2'")
            assert cur.fetchone()["crash_count"] == 3


def test_monitor_actions_quarantine_uses_manual(fleet_db):
    from applypilot.fleet import monitor
    with pgqueue.connect(fleet_db) as conn:
        newly = monitor.MonitorActions(conn).quarantine("j3", worker="w", reason="owner")
        assert newly is True  # single call pulls the job (manual one-shot)
        with conn.cursor() as cur:
            cur.execute("SELECT crash_count, quarantined_at FROM poison_jobs WHERE url='j3'")
            row = cur.fetchone()
        assert row["crash_count"] == 0 and row["quarantined_at"] is not None
```

- [ ] **Step 2: Run it, expect FAIL** (`TypeError: quarantine_job() got an unexpected keyword argument 'manual'`).

- [ ] **Step 3: Implement** — in `heartbeat.py`, change the `quarantine_job` signature and add the `manual` branch at the top of the function body:

```python
def quarantine_job(conn, url, *, worker, reason, threshold=3, commit=True, manual=False) -> bool:
    """Bump ``poison_jobs.crash_count`` for ``url`` (creating the row on first
    strike). Once the count reaches ``threshold`` and the job is not already
    quarantined, stamp ``quarantined_at`` + ``reason``. Returns True ONLY on the
    transition that newly quarantines the job (idempotent thereafter).

    ``manual=True`` is a DELIBERATE one-shot (owner / monitor / Codex bridge): pull the
    job immediately WITHOUT accumulating ``crash_count`` (so manual quarantines never
    pollute real crash signal), tagging the reason with a ``manual:`` prefix. Returns
    True only on the newly-quarantined transition, False if already quarantined."""
    if manual:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO poison_jobs (url, crash_count, last_worker, reason, quarantined_at)
                   VALUES (%s, 0, %s, %s, now())
                   ON CONFLICT (url) DO UPDATE SET
                       last_worker    = EXCLUDED.last_worker,
                       reason         = EXCLUDED.reason,
                       quarantined_at = now()
                   WHERE poison_jobs.quarantined_at IS NULL""",
                (url, worker, f"manual:{reason}"),
            )
            newly = cur.rowcount > 0
        if commit:
            conn.commit()
        return newly
    with conn.cursor() as cur:
        # ... existing crash-strike body unchanged ...
```

(Keep the existing crash-strike body exactly as-is below the `manual` branch.)

In `monitor.py`, change `MonitorActions.quarantine`:

```python
    def quarantine(self, url: str, *, worker: str, reason: str) -> bool:
        """Quarantine a poison job (deliberate one-shot: pulls immediately, does not
        pollute crash_count). Stops the job being re-leased."""
        return heartbeat.quarantine_job(self._conn, url, worker=worker, reason=reason, manual=True)
```

- [ ] **Step 4: Run it, expect PASS.** Then run the watchdog suite to confirm the automatic (crash-strike) quarantine path is unaffected.

Run: `.conda-env/python.exe -m pytest tests/test_fleet_monitor.py tests/test_fleet_watchdog.py -q`
Expected: all pass (the watchdog's `_handle_stuck` calls `quarantine_job` with `manual=False` default → crash-strike behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/heartbeat.py src/applypilot/fleet/monitor.py tests/test_fleet_monitor.py
git commit -m "feat(fleet): manual one-shot quarantine (no crash_count pollution) for monitor/bridge"
```

---

### Task 5: Action tools — `restart_worker`, `pause_scope`, `quarantine_job`

**Files:**
- Modify: `src/applypilot/fleet/codex_bridge.py`
- Test: `tests/test_codex_bridge.py`

**Interfaces:**
- Consumes: `monitor.MonitorActions(conn)` (`.restart_worker(worker_id) -> int`, `.pause_scope(scope_key) -> None`, `.quarantine(url, *, worker, reason) -> bool`), `_with_conn`, stdlib `logging`.
- Produces: tools `restart_worker(worker_id: str) -> dict[str, Any]`, `pause_scope(scope_key: str) -> dict[str, Any]`, `quarantine_job(url: str, worker: str, reason: str) -> dict[str, Any]`. Each logs the action (audit trail, spec §7) and returns a small result dict.

- [ ] **Step 1: Write the failing test**

```python
def test_restart_worker_enqueues_command(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    out = codex_bridge.restart_worker("wA")
    assert out["action"] == "restart" and out["worker_id"] == "wA"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT command FROM remote_commands WHERE worker_id='wA'")
        assert cur.fetchone()["command"] == "restart"


def test_pause_scope_pauses(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, min_gap_seconds) VALUES ('host:z.com',5)")
        conn.commit()
    out = codex_bridge.pause_scope("host:z.com")
    assert out["action"] == "pause" and out["scope_key"] == "host:z.com"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key='host:z.com'")
        assert cur.fetchone()["breaker_state"] == "paused"


def test_quarantine_job_one_shot(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    out = codex_bridge.quarantine_job("jX", "wA", "owner-pulled")
    assert out["action"] == "quarantine" and out["url"] == "jX" and out["newly_quarantined"] is True
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT quarantined_at, crash_count FROM poison_jobs WHERE url='jX'")
        row = cur.fetchone()
        assert row["quarantined_at"] is not None and row["crash_count"] == 0
```

- [ ] **Step 2: Run it, expect FAIL** (`AttributeError: restart_worker`).

- [ ] **Step 3: Implement** — append to `codex_bridge.py` (add `import logging` + `logger` near the top imports):

```python
import logging
logger = logging.getLogger("applypilot.fleet.codex_bridge")


@mcp.tool()
def restart_worker(worker_id: str) -> dict[str, Any]:
    """Enqueue a 'restart' command for a worker (conservative — only slows the fleet)."""
    def _do(conn):
        command_id = monitor.MonitorActions(conn).restart_worker(worker_id)
        logger.info("bridge action: restart_worker worker_id=%s command_id=%s", worker_id, command_id)
        return {"action": "restart", "worker_id": worker_id, "command_id": command_id}
    return _with_conn(_do)


@mcp.tool()
def pause_scope(scope_key: str) -> dict[str, Any]:
    """Pause a host/board scope. Does NOT unpause (resume is owner-only, absent here)."""
    def _do(conn):
        monitor.MonitorActions(conn).pause_scope(scope_key)
        logger.info("bridge action: pause_scope scope_key=%s", scope_key)
        return {"action": "pause", "scope_key": scope_key}
    return _with_conn(_do)


@mcp.tool()
def quarantine_job(url: str, worker: str, reason: str) -> dict[str, Any]:
    """Manually quarantine a job (one-shot: pulls it now, does not pollute crash_count)."""
    def _do(conn):
        newly = monitor.MonitorActions(conn).quarantine(url, worker=worker, reason=reason)
        logger.info("bridge action: quarantine_job url=%s worker=%s newly=%s", url, worker, newly)
        return {"action": "quarantine", "url": url, "newly_quarantined": newly}
    return _with_conn(_do)
```

- [ ] **Step 4: Run it, expect PASS** (13 passed total in `tests/test_codex_bridge.py`).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/codex_bridge.py tests/test_codex_bridge.py
git commit -m "feat(fleet): Codex bridge action tools — restart/pause/quarantine (audited)"
```

---

### Task 6: Registry / safety gate (exactly 8 tools, no denied op)

**Files:**
- Test: `tests/test_codex_bridge.py`

**Interfaces:**
- Consumes: the registered `mcp` app from Tasks 1-5.

- [ ] **Step 1: Write the test (the hard safety gate)**

```python
def test_registry_is_exactly_the_eight_tools():
    from applypilot.fleet import codex_bridge
    names = {t.name for t in codex_bridge.mcp._tool_manager.list_tools()}  # sync, no await
    assert names == {
        "fleet_status", "health_report", "recent_results", "challenges", "caps",
        "restart_worker", "pause_scope", "quarantine_job",
    }


def test_no_denied_op_is_registered():
    from applypilot.fleet import codex_bridge
    names = {t.name for t in codex_bridge.mcp._tool_manager.list_tools()}
    for denied in ("apply", "approve", "resolve_challenge", "set_cost_cap", "unpause",
                   "resume_scope", "set_paused", "query", "execute", "sql"):
        assert denied not in names
```

- [ ] **Step 2: Run it.** Expected: PASS (the 8 tools registered in Tasks 1-5 — no production change needed; if it fails, a tool name drifted or an extra tool was registered, which is a real defect to fix).

Run: `.conda-env/python.exe -m pytest tests/test_codex_bridge.py -q`
Expected: 15 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_bridge.py
git commit -m "test(fleet): Codex bridge registry safety gate — exactly 8 tools, no denied op"
```

---

### Task 7: End-to-end + full suite + Codex config runbook

**Files:**
- Test: `tests/test_codex_bridge.py`
- Create: `docs/fleet-codex-bridge-runbook.md`

**Interfaces:**
- Consumes everything above.

- [ ] **Step 1: Write the e2e test** — exercise a read tool and an action tool through one seeded PG, proving the whole surface composes.

```python
def test_bridge_end_to_end(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO compute_queue (url, task, status, updated_at) VALUES ('c1','score','done', now())")
        cur.execute("INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) VALUES ('w1','compute','idle', now())")
        conn.commit()
    # read: status + recent_results render
    assert "machines" in codex_bridge.fleet_status()
    assert codex_bridge.recent_results()["results"][0]["url"] == "c1"
    # act: pause a scope, see it reflected
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, min_gap_seconds) VALUES ('host:e2e',5)")
        conn.commit()
    assert codex_bridge.pause_scope("host:e2e")["action"] == "pause"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key='host:e2e'")
        assert cur.fetchone()["breaker_state"] == "paused"
```

- [ ] **Step 2: Run it; iterate until green.**

Run: `.conda-env/python.exe -m pytest tests/test_codex_bridge.py -q` (expected: 16 passed)

- [ ] **Step 3: Run the FULL fleet suite (the gate).**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_codex_bridge.py tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py -q`
Expected: all pass (prior watchdog-era 166 + the new bridge/monitor tests). Capture exact counts.

- [ ] **Step 4: Write the Codex runbook** `docs/fleet-codex-bridge-runbook.md` with the Windows-correct config (absolute interpreter, `cwd`, `enabled`, env subtable) and the troubleshooting notes:

```markdown
# Codex Fleet Bridge — runbook

Run on the home box (Postgres + Codex local). Codex CLI is the verified-reliable path;
Codex desktop additionally needs `cwd` + `enabled` (openai/codex #14449).

Add to `~/.codex/config.toml`:

​```toml
[mcp_servers.applypilot-fleet]
command = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot\\.conda-env\\python.exe"
args = ["-m", "applypilot.fleet.codex_bridge"]
cwd = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot"
enabled = true

[mcp_servers.applypilot-fleet.env]
FLEET_PG_DSN = "postgresql://…?connect_timeout=5"
​```

- If every tool returns "FLEET_PG_DSN is not set", the env block did not inject — confirm it's a
  SUBTABLE (`[mcp_servers.applypilot-fleet.env]`), not an inline `env = {…}`.
- Use the ABSOLUTE `.conda-env\python.exe` — a GUI-launched Codex won't inherit the conda PATH and a
  bare `python` will ImportError on `applypilot`/`mcp`.
- Codex defaults: 10s startup, 60s per-tool. If the PG is remote, bump `tool_timeout_sec`.
- Leave the action tools (restart_worker / pause_scope / quarantine_job) on Codex's per-call approval.
- Tools: fleet_status, health_report, recent_results, challenges, caps (read); restart_worker,
  pause_scope, quarantine_job (action, audited via the bridge's logger).
```

- [ ] **Step 5: Commit** (do NOT push)

```bash
git add tests/test_codex_bridge.py docs/fleet-codex-bridge-runbook.md
git commit -m "test(fleet): Codex bridge e2e + runbook"
```

---

## Self-Review

**Spec coverage:**
- §3 architecture (FastMCP app, `_with_conn`, no-DB-at-import, connect-only import, DSN-direct) → Task 1. §3.1 read tools (`fleet_status`/`caps`/`health_report`) → Task 2; (`recent_results`/`challenges`) → Task 3. §3.1.1 normalized merged feed with structured `detail` → Task 3. §3.1 action tools → Task 5. §3.1 manual one-shot quarantine + §7 mitigation → Task 4 (foundation) + Task 5 (audit log). §3.2 / §5 registry safety gate → Task 6. §4 error handling (structured errors, RuntimeError catch) → Task 1 `_with_conn` + tested across tasks. §5 testing (read/action/registry/DSN-discipline/e2e) → Tasks 1-7. §6 owner-run config → Task 7 runbook. §8 decisions all reflected (uniform dict return, sync registry path, manual quarantine, no rate cap).
- The spec's `health_report`/`recent_results`/`challenges` return types are tightened to a uniform `dict[str, Any]` (wrapping text/lists) so the `{"error": …}` sentinel never collides with a narrow annotation — a sound refinement of the §3.1 table, consistent with the §3.1 datetime note ("annotate `dict[str,Any]`/leave permissive"). The spec table should be read with this wrapping.

**Placeholder scan:** none — every code step has complete code. Two `NOTE:` callouts (Task 3 `auth_challenge.raised_at` column name; the foundation crash-strike body "unchanged below") instruct the implementer to confirm an exact column / preserve existing code; these are verification instructions, not placeholders.

**Type consistency:** `_with_conn(fn)` returns `dict` and is used identically in Tasks 1-5. Every tool returns `dict[str, Any]`; read-list/text tools wrap under `results`/`challenges`/`report` keys (consistent between impl and tests). `quarantine_job(conn, url, *, worker, reason, threshold=3, commit=True, manual=False)` is the single signature used in Task 4 impl + Task 4 tests + Task 5's `MonitorActions.quarantine` delegation. The 8 tool names in Task 6's registry assertion exactly match the 8 `@mcp.tool()` function names defined in Tasks 2/3/5.

**One integration note:** Task 4 changes `MonitorActions.quarantine` semantics (a single call now pulls the job, where before it took 3 strikes). The watchdog's automatic quarantine calls `heartbeat.quarantine_job` DIRECTLY with the default `manual=False`, so it is unaffected — Task 4 Step 4 runs `tests/test_fleet_watchdog.py` to confirm. The only consumer of `MonitorActions.quarantine` is the bridge (and any future Layer-B monitor), so the semantic change is contained.
