# Fleet Discovery Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Lean, brain-less discovery workers scrape via imported JobSpy helpers (jobspy.py untouched), stage postings to a Postgres table, and the home box ingests them into the shared brain via the existing `store_jobspy_results` dedup — reusing the built scheduler + board-governed `lease_search`/`complete_search`.

**Architecture:** worker `lease_search` → `make_search_fn` (JobSpy scrape + `_location_ok` filter) → `queue.push_discovered` (PG `discovered_postings`) → `complete_search`. Home `sync.pull_discovered` → `pd.DataFrame(rows)` → `jobspy.store_jobspy_results(brain, df, label)` → mark synced.

**Tech Stack:** Python 3.11 (`.conda-env`), psycopg3, pandas 2.3.3, pytest with the disposable `applypilot-pgtest` Postgres (`fleet_db` fixture). Reuses `discovery/jobspy.py` helpers (imported, never modified).

## Global Constraints

- Run tests with `.conda-env/python.exe -m pytest <path> -q` from the repo root. PG-backed tests use the `fleet_db` fixture.
- **Commit only the specific paths named per task — NEVER `git add -A`.** The user's 7 dirty files (`run-applypilot.ps1`, `discovery/jobspy.py`, `pipeline.py`, `scoring/cover_letter.py`, `scoring/tailor.py`, `tests/test_discovery_scheduler.py`, `tests/test_generation_workers.py`) and sibling-session files stay untouched. **`discovery/jobspy.py` is READ/IMPORT ONLY — never modify it.**
- New v3 tables go in `schema_v3.sql` AND `conftest._V3_TABLES`.
- All scraping + the brain write are STUBBED in tests (no real JobSpy call, no real brain): monkeypatch `_scrape_with_retry` / `store_jobspy_results`.
- The brain is plain SQLite; the staging is Postgres.

## File Structure

- Modify `src/applypilot/fleet/schema_v3.sql` — add `discovered_postings`.
- Modify `tests/conftest.py` — add `"discovered_postings"` to `_V3_TABLES`.
- Modify `src/applypilot/fleet/queue.py` — add `push_discovered`.
- Create `src/applypilot/fleet/discovery_adapter.py` — `make_search_fn`.
- Modify `src/applypilot/fleet/worker.py` — `_tick_discovery` stages postings.
- Modify `src/applypilot/fleet/sync.py` — add `pull_discovered`.
- Create `src/applypilot/fleet/discovery_main.py` — worker + home entrypoints.
- Modify `pyproject.toml` — two console scripts.
- Tests: `tests/test_fleet_discovery.py` (schema/push/pull/worker), `tests/test_discovery_adapter.py`, `tests/test_fleet_discovery_e2e.py`.

---

### Task 1: `discovered_postings` table + `queue.push_discovered`

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`, `tests/conftest.py`, `src/applypilot/fleet/queue.py`
- Test: `tests/test_fleet_discovery.py`

**Interfaces:**
- Produces: `queue.push_discovered(conn, *, task_id, source_label, worker_id, postings, commit=True) -> int` — bulk-inserts posting dicts into `discovered_postings` (`posting` as JSONB); returns the count.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_discovery.py
from applypilot.apply import pgqueue
from applypilot.fleet import queue


def test_push_discovered_stages_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        n = queue.push_discovered(conn, task_id="t1", source_label="chief of staff", worker_id="w1",
                                  postings=[{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
        assert n == 2
        with conn.cursor() as cur:
            cur.execute("SELECT task_id, source_label, worker_id, posting, synced_to_home_at "
                        "FROM discovered_postings ORDER BY posting->>'job_url'")
            rows = cur.fetchall()
        assert [r["posting"]["job_url"] for r in rows] == ["u1", "u2"]
        assert rows[0]["task_id"] == "t1" and rows[0]["source_label"] == "chief of staff"
        assert rows[0]["synced_to_home_at"] is None
```

- [ ] **Step 2: Run it, expect FAIL** — `.conda-env/python.exe -m pytest tests/test_fleet_discovery.py -q`.

- [ ] **Step 3: Implement**

In `schema_v3.sql` (after the `compute_queue` block):
```sql
-- discovered_postings: raw JobSpy postings staged by lean discovery workers (no local brain).
-- The home box ingests these into the shared SQLite brain via store_jobspy_results (one write path).
CREATE TABLE IF NOT EXISTS discovered_postings (
    id                BIGSERIAL PRIMARY KEY,
    task_id           TEXT,
    source_label      TEXT,
    posting           JSONB NOT NULL,
    worker_id         TEXT,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_to_home_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_discovered_unsynced ON discovered_postings (discovered_at)
    WHERE synced_to_home_at IS NULL;
```

In `conftest.py`, add `"discovered_postings"` to `_V3_TABLES`.

In `queue.py`:
```python
def push_discovered(conn, *, task_id, source_label, worker_id, postings, commit=True) -> int:
    """Stage raw JobSpy postings (dicts) from a discovery worker into discovered_postings.
    The home box later ingests them into the brain (sync.pull_discovered)."""
    n = 0
    with conn.cursor() as cur:
        for p in postings:
            cur.execute(
                "INSERT INTO discovered_postings (task_id, source_label, posting, worker_id) "
                "VALUES (%s,%s,%s,%s)",
                (task_id, source_label, json.dumps(p, default=str), worker_id),
            )
            n += 1
    if commit:
        conn.commit()
    return n
```
(`json` is already imported at the top of `queue.py`.)

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql tests/conftest.py src/applypilot/fleet/queue.py tests/test_fleet_discovery.py
git commit -m "feat(fleet): discovered_postings staging table + queue.push_discovered"
```

---

### Task 2: `discovery_adapter.make_search_fn`

**Files:**
- Create: `src/applypilot/fleet/discovery_adapter.py`
- Test: `tests/test_discovery_adapter.py`

**Interfaces:**
- Produces: `make_search_fn(*, results_per_site=50, hours_old=72, proxy=None, search_cfg=None) -> Callable[[dict], list[dict]]`. The returned `search_fn(task)` scrapes via JobSpy (imported `_scrape_with_retry`), filters by `_location_ok`, and returns `df.to_dict("records")`; a scrape exception → `[]`. NO brain write.

- [ ] **Step 1: Write the failing test** (stub the scrape; no real JobSpy)

```python
# tests/test_discovery_adapter.py
import pandas as pd
from applypilot.fleet import discovery_adapter as da


def test_search_fn_maps_kwargs_and_returns_records(monkeypatch):
    seen = {}
    def fake_scrape(kwargs, **k):
        seen["kwargs"] = kwargs
        return pd.DataFrame([{"job_url": "u1", "title": "COS", "location": "Remote"},
                             {"job_url": "u2", "title": "PM", "location": "Remote"}])
    monkeypatch.setattr(da, "_scrape_with_retry", fake_scrape)
    monkeypatch.setattr(da, "_location_ok", lambda loc, a, r: True)  # accept all
    fn = da.make_search_fn(results_per_site=25, hours_old=48)
    out = fn({"task_id": "t1", "query": "chief of staff", "board": "indeed",
              "location": "Remote", "params": {"remote": True}})
    assert [p["job_url"] for p in out] == ["u1", "u2"]
    assert seen["kwargs"]["search_term"] == "chief of staff"
    assert seen["kwargs"]["site_name"] == ["indeed"] and seen["kwargs"]["location"] == "Remote"
    assert seen["kwargs"]["results_wanted"] == 25 and seen["kwargs"]["hours_old"] == 48
    assert seen["kwargs"].get("is_remote") is True


def test_search_fn_returns_empty_on_scrape_error(monkeypatch):
    def boom(kwargs, **k): raise RuntimeError("blocked")
    monkeypatch.setattr(da, "_scrape_with_retry", boom)
    fn = da.make_search_fn()
    assert fn({"task_id": "t", "query": "q", "board": "indeed", "location": "NYC", "params": {}}) == []
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/discovery_adapter.py
"""Lean discovery scrape adapter: wraps JobSpy's scrape+filter (imported from
discovery.jobspy, which is NEVER modified) and returns posting dicts. No brain write
-- the worker stages the postings to Postgres; the home box ingests them."""
from __future__ import annotations

from typing import Callable

from applypilot.discovery.jobspy import (
    _scrape_with_retry, _location_ok, _load_location_config, parse_proxy,
)


def make_search_fn(*, results_per_site=50, hours_old=72, proxy=None, search_cfg=None) -> Callable[[dict], list[dict]]:
    accept, reject = _load_location_config(search_cfg or {})
    proxy_config = parse_proxy(proxy) if proxy else None

    def search_fn(task: dict) -> list[dict]:
        params = task.get("params") or {}
        sites = params.get("sites") or [task["board"]]
        kwargs = {
            "site_name": sites, "search_term": task["query"], "location": task.get("location") or "",
            "results_wanted": results_per_site, "hours_old": hours_old,
            "description_format": "markdown", "country_indeed": "usa", "verbose": 1,
        }
        if params.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs)
        except Exception:
            return []  # a scrape block -> empty; the worker records a board-block outcome
        if df is None or len(df) == 0:
            return []
        df = df[df.apply(lambda row: _location_ok(row.get("location"), accept, reject), axis=1)]
        return df.to_dict("records")

    return search_fn
```

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/discovery_adapter.py tests/test_discovery_adapter.py
git commit -m "feat(fleet): discovery scrape adapter (wraps JobSpy, no brain write)"
```

---

### Task 3: `worker._tick_discovery` stages postings

**Files:**
- Modify: `src/applypilot/fleet/worker.py`
- Test: `tests/test_fleet_discovery.py`

**Interfaces:**
- Consumes: `queue.push_discovered`.
- Produces: `_tick_discovery` now stages the returned postings via `push_discovered` before `complete_search` (only when the scrape didn't error). The return dict gains `"staged": <count>`.

- [ ] **Step 1: Write the failing test** (seed a search task, run one tick with a fake search_fn)

```python
def test_worker_discovery_stages_postings(fleet_db):
    from applypilot.fleet.worker import WorkerLoop
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO search_tasks (task_id, query, board, location, cadence_seconds) "
                    "VALUES ('t1','chief of staff','indeed','Remote',3600)")
        conn.commit()
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w-disc", home_ip="1.1.1.1", role="discovery",
                      search_fn=lambda task: [{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
    res = loop.run_once()
    assert res["action"] == "search_done" and res["staged"] == 2
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM discovered_postings WHERE task_id='t1'")
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT status, next_due_at > now() AS future FROM search_tasks WHERE task_id='t1'")
        r = cur.fetchone(); assert r["status"] == "queued" and r["future"] is True  # rescheduled
```

- [ ] **Step 2: Run it, expect FAIL** (no `staged` key / no staging).

- [ ] **Step 3: Implement** — in `worker._tick_discovery`, between `postings = self.search_fn(task)` (its try/except) and `complete_search`, add the staging:

```python
        staged = 0
        if not error and postings:
            staged = queue.push_discovered(
                conn, task_id=task["task_id"],
                source_label=task.get("query") or task.get("board"),
                worker_id=self.worker_id, postings=postings, commit=False,
            )
        queue.complete_search(
            conn, self.worker_id, task["task_id"],
            result_count=len(postings), board=task.get("board"), error=error,
        )
        self._beat(conn, state="idle")
        return {"action": "search_done", "task_id": task["task_id"],
                "result_count": len(postings), "staged": staged, "error": error}
```
(Keep the existing lease/error-handling above unchanged; only insert the staging + add `staged` to the return. `push_discovered(commit=False)` so it shares `complete_search`'s transaction — verify `complete_search` commits at the end; if it manages its own commit, call `push_discovered` with `commit=False` then let `complete_search` commit, OR push with `commit=True` before completing. Pick whichever keeps both writes durable; the test asserts both landed.)

- [ ] **Step 4: Run it, expect PASS.** Then run `tests/test_fleet_v3_worker.py` to confirm the apply/compute ticks are unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/worker.py tests/test_fleet_discovery.py
git commit -m "feat(fleet): discovery worker stages scraped postings to PG"
```

---

### Task 4: `sync.pull_discovered`

**Files:**
- Modify: `src/applypilot/fleet/sync.py`
- Test: `tests/test_fleet_discovery.py`

**Interfaces:**
- Produces: `sync.pull_discovered(*, sqlite_conn=None, pg_conn=None, batch=500) -> int` — reads unsynced `discovered_postings`, groups by `source_label`, reconstructs `pd.DataFrame(rows)`, calls `jobspy.store_jobspy_results(brain, df, label)`, marks the rows synced. Returns postings ingested. Idempotent re-pull.

- [ ] **Step 1: Write the failing test** (stub `store_jobspy_results`; record what it received)

```python
def test_pull_discovered_ingests_and_marks_synced(fleet_db, monkeypatch):
    import applypilot.fleet.sync as sync_mod
    captured = {}
    def fake_store(conn, df, source_label):
        captured["urls"] = list(df["job_url"]); captured["label"] = source_label
        return (len(df), 0)
    monkeypatch.setattr(sync_mod, "store_jobspy_results", fake_store)
    with pgqueue.connect(fleet_db) as conn:
        queue.push_discovered(conn, task_id="t1", source_label="cos", worker_id="w1",
                              postings=[{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
    with pgqueue.connect(fleet_db) as pg:
        n = sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg)  # brain conn unused by the stub
    assert n == 2 and captured["urls"] == ["u1", "u2"] and captured["label"] == "cos"
    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM discovered_postings WHERE synced_to_home_at IS NULL")
        assert cur.fetchone()["n"] == 0
    # re-pull is a no-op
    with pgqueue.connect(fleet_db) as pg:
        assert sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg) == 0
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement** — in `sync.py`, add the import `from applypilot.discovery.jobspy import store_jobspy_results` and `import pandas as pd` (at module top), and:

```python
def pull_discovered(*, sqlite_conn=None, pg_conn=None, batch=500) -> int:
    """Ingest staged discovery postings into the shared brain via store_jobspy_results.
    Group unsynced rows by source_label, rebuild a DataFrame per group, dedup-insert,
    then mark synced. Idempotent: synced rows are skipped; store_jobspy_results dedups by url."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    n = 0
    try:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT id, source_label, posting FROM discovered_postings "
                "WHERE synced_to_home_at IS NULL ORDER BY discovered_at LIMIT %s",
                (batch,),
            )
            rows = cur.fetchall()
        if not rows:
            return 0
        by_label: dict[str, list] = {}
        ids: list[int] = []
        for r in rows:
            by_label.setdefault(r["source_label"] or "", []).append(r["posting"])
            ids.append(r["id"])
        for label, postings in by_label.items():
            store_jobspy_results(sq, pd.DataFrame(postings), label)
            n += len(postings)
        with pg.cursor() as cur:
            cur.execute("UPDATE discovered_postings SET synced_to_home_at = now() WHERE id = ANY(%s)", (ids,))
        pg.commit()
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()
```

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/sync.py tests/test_fleet_discovery.py
git commit -m "feat(fleet): sync.pull_discovered (PG staging -> brain via store_jobspy_results)"
```

---

### Task 5: Entrypoints (`applypilot-fleet-discovery` + `-home`)

**Files:**
- Create: `src/applypilot/fleet/discovery_main.py`
- Modify: `pyproject.toml`
- Test: `tests/test_fleet_discovery.py`

**Interfaces:**
- Produces: `build_discovery_loop(*, dsn, worker_id, home_ip, results_per_site, hours_old, proxy, search_cfg=None) -> WorkerLoop` (a `role='discovery'` loop with `search_fn = make_search_fn(...)`); `expand_searches(conn, config)` (delegates to `scheduler.expand_search_config`); `pull(*, sqlite_conn, pg_conn)` (delegates to `sync.pull_discovered`); `main_worker(argv)` / `main_home(argv)`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_discovery_loop_wires_search_fn(fleet_db):
    from applypilot.fleet import discovery_main as dm
    loop = dm.build_discovery_loop(dsn=fleet_db, worker_id="w1", home_ip="1.1.1.1",
                                   results_per_site=25, hours_old=48, proxy=None)
    assert loop.role == "discovery" and loop.search_fn is not None
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement** `discovery_main.py` (mirror `compute_worker_main`/`compute_home_main`): `build_discovery_loop` builds `WorkerLoop(conn_factory=lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="discovery", search_fn=make_search_fn(results_per_site=..., hours_old=..., proxy=..., search_cfg=search_cfg))`; `main_worker` reads `--dsn`/`FLEET_PG_DSN` + flags and `run_forever`; `main_home` has `expand` (load a searches config + `scheduler.expand_search_config`) and `pull` (`sync.pull_discovered`) subcommands. Register in `pyproject.toml [project.scripts]`: `applypilot-fleet-discovery = "applypilot.fleet.discovery_main:main_worker"` and `applypilot-fleet-discovery-home = "applypilot.fleet.discovery_main:main_home"`.

- [ ] **Step 4: Run it, expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/discovery_main.py pyproject.toml tests/test_fleet_discovery.py
git commit -m "feat(fleet): discovery worker + home entrypoints"
```

---

### Task 6: End-to-end + full suite

**Files:**
- Test: `tests/test_fleet_discovery_e2e.py`

- [ ] **Step 1: Write the failing test** — expand a 1-search config → run a discovery tick (stub scrape) → staging rows → pull (stub store) → assert the stub got the postings + staging marked synced:

```python
# tests/test_fleet_discovery_e2e.py
from applypilot.apply import pgqueue
from applypilot.fleet import scheduler, sync as sync_mod, discovery_adapter as da
from applypilot.fleet.worker import WorkerLoop
import pandas as pd


def test_discovery_end_to_end(fleet_db, monkeypatch):
    monkeypatch.setattr(da, "_scrape_with_retry",
                        lambda kwargs, **k: pd.DataFrame([{"job_url": "u1", "title": "COS", "location": "Remote"}]))
    monkeypatch.setattr(da, "_location_ok", lambda loc, a, r: True)
    captured = {}
    monkeypatch.setattr(sync_mod, "store_jobspy_results",
                        lambda conn, df, label: (captured.setdefault("urls", list(df["job_url"])), (len(df), 0))[1])
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, {"searches": [{"query": "chief of staff", "boards": ["indeed"],
                                                            "locations": ["Remote"]}]})
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="discovery",
                      search_fn=da.make_search_fn())
    assert loop.run_once()["action"] == "search_done"
    with pgqueue.connect(fleet_db) as pg:
        assert sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg) == 1
    assert captured["urls"] == ["u1"]
```

- [ ] **Step 2: Run it; iterate until green.**

- [ ] **Step 3: Implement** — no new code; fix any wiring mismatch.

- [ ] **Step 4: Run the full fleet suite**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py tests/test_build_score_prompt_text.py -q`
Expected: all pass (prior 139 + the new discovery tests).

- [ ] **Step 5: Commit & push**

```bash
git add tests/test_fleet_discovery_e2e.py
git commit -m "test(fleet): discovery coordination end-to-end"
git push private applypilot-hardening-and-brainstorm-integration
```

---

## Self-Review

**Spec coverage:** §3.1 staging table → Task 1; §3.2 adapter → Task 2; §3.3 push_discovered → Task 1; §3.4 worker staging → Task 3; §3.5 pull_discovered → Task 4; §3.6 entrypoints → Task 5; §5 testing → every task + Task 6. No gaps.

**Placeholder scan:** none — every step has complete code, except Task 5's `main_worker`/`main_home` bodies which follow the established `compute_worker_main`/`compute_home_main` argparse pattern (the implementer mirrors those existing files); `build_discovery_loop` (the tested unit) is fully specified.

**Type consistency:** `push_discovered(conn, *, task_id, source_label, worker_id, postings, commit)` identical in Tasks 1 & 3; `make_search_fn(...) -> search_fn(task) -> list[dict]` consumed by Task 3/5/6; `pull_discovered(*, sqlite_conn, pg_conn, batch)` in Tasks 4 & 6; postings are JobSpy row dicts with a `job_url` key throughout.

**One integration note:** Task 3 must keep `push_discovered` and `complete_search` durable in the same tick — confirm `complete_search`'s commit semantics and choose the `commit=` flag so both writes land (the test asserts both).
