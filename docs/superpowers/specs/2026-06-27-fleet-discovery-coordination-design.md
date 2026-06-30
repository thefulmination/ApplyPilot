# Fleet Discovery Coordination — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the tested fleet v3 foundation — `scheduler.py` (searches → `search_tasks`),
`queue.lease_search`/`complete_search` (board-governed recurring claim), `sync.py` (brain↔PG bridge
patterns). See [`2026-06-26-distributed-residential-fleet-design.md`](2026-06-26-distributed-residential-fleet-design.md).

## 1. Goal & success criteria

Let **many machines run wide-net discovery into the ONE shared brain** — the coordination that removes
the manual "scrape into a silo, merge by hand" problem. Discovery workers are **lean and brain-less**
(they hold Postgres, not the SQLite brain), so they can run on varied/cheap machines on **different
IPs than the apply box** (the safety model: never scrape on the apply account). Postings flow
**worker → PG staging → home → central brain**, where the home box owns the single write via the
existing, battle-tested `jobspy.store_jobspy_results` dedup/insert.

**Decided approach (Option A, fleet-native):** the discovery adapter **imports + calls JobSpy's
scrape/filter helpers** (`scrape_jobs`/`_scrape_with_retry`, `_location_ok`) — it does NOT modify
`jobspy.py` and does NOT write the brain on the worker. It returns posting dicts; the worker ships
them to a PG staging table; the home box reconstructs a DataFrame and runs `store_jobspy_results`
against the brain.

**Done when:** the home scheduler expands a search config into `search_tasks`; a discovery worker
leases a task (board-governed), scrapes via the adapter (stubbed in tests — no real scraping), pushes
the postings to `discovered_postings` (PG), and reschedules the task; the home `pull_discovered`
reconstructs the DataFrame and calls `store_jobspy_results(brain, df, label)` → new jobs land in the
shared brain (deduped), and the staging rows are marked synced (idempotent re-pull is a no-op).
Verified by tests against the disposable Postgres + a temp SQLite brain with `scrape_jobs` and
`store_jobspy_results` stubbed. No real scraping, no real brain.

**Non-goals:** the apply lane (separate, catastrophe-class build); changing `jobspy.py` or any in-flight
file; enrichment (separate); LinkedIn *scraping* policy beyond reusing the existing board governor.

## 2. Flow (reuses the built scheduler + governed search claim)

```
home: scheduler.expand_search_config(searches.yaml) → search_tasks (PG)
                                                          │ lease_search (board-governed, recurring)
                                                          ▼
worker (lean, no brain):  make_search_fn(task) → JobSpy scrape + _location_ok filter → [posting dicts]
                          → queue.push_discovered(PG) → complete_search (reschedule next_due_at)
                                                          │
home: sync.pull_discovered → pd.DataFrame(rows) → store_jobspy_results(brain, df, label) → mark synced
                                                          ▼
                                              shared brain `jobs` (deduped insert)
```

The **scheduler, `lease_search`, `complete_search`, and the `board:<name>` governor/breaker are already
built and tested.** This spec adds: the staging table, the scrape adapter, the worker push, the home
pull, and the entrypoints.

## 3. Components

### 3.1 `discovered_postings` PG staging table (schema_v3.sql addition)
```sql
CREATE TABLE IF NOT EXISTS discovered_postings (
  id             BIGSERIAL PRIMARY KEY,
  task_id        TEXT,                 -- the search_task that produced it
  source_label   TEXT,                 -- store_jobspy_results' label (the query/board)
  posting        JSONB NOT NULL,       -- one JobSpy row as a dict (job_url/title/company/.../job_url_direct)
  worker_id      TEXT,
  discovered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_to_home_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_discovered_unsynced ON discovered_postings (discovered_at)
  WHERE synced_to_home_at IS NULL;
```
Added to `conftest._V3_TABLES` for test truncation.

### 3.2 `fleet/discovery_adapter.py` — scrape wrapper (no brain write)
`make_search_fn(*, default_sites=None, results_per_site=50, hours_old=72, proxy=None) -> Callable[[dict], list[dict]]`.
The returned `search_fn(task)`:
1. Maps the `search_task` (`query`, `board`, `location`, `params` JSONB) to JobSpy kwargs (`search_term`,
   `site_name=[board]` or `params['sites']`, `location`, `results_wanted`, `hours_old`,
   `description_format='markdown'`, `is_remote` from params, proxies from `parse_proxy`).
2. Calls `jobspy._scrape_with_retry(kwargs)` → DataFrame (imported from `applypilot.discovery.jobspy`;
   `scrape_jobs` is stubbed in tests).
3. Applies `jobspy._location_ok` filtering (reusing the accept/reject location config).
4. Returns `df.to_dict("records")` — a list of posting dicts (the columns `store_jobspy_results` reads).
On a scrape exception returns `[]` (the worker treats an empty/raised scrape as a board-block → governor;
see §3.4). NO DB write here.

### 3.3 `queue.push_discovered(conn, *, task_id, source_label, worker_id, postings) -> int`
Bulk-insert the posting dicts into `discovered_postings` (one row each, `posting` as JSONB). Returns the
count. Idempotency is not needed at push (each scrape run stages fresh rows; the home dedups at insert).

### 3.4 `worker._tick_discovery` extension (worker.py — mine)
After `search_fn(task)` returns postings: `queue.push_discovered(conn, task_id=task["task_id"],
source_label=task.get("query") or task["board"], worker_id=self.worker_id, postings=postings)`, THEN
`complete_search(...)` with `result_count=len(postings)`. A scrape that raises/returns empty under a
block sets `error="blocked"` (already wired) → the board governor records it. (Today `_tick_discovery`
calls search_fn + complete_search but does NOT stage postings — this adds the push.)

### 3.5 `sync.pull_discovered(*, sqlite_conn=None, pg_conn=None, batch=500) -> int`
Home-side: select unsynced `discovered_postings` (oldest first, `LIMIT batch`), group by `source_label`,
reconstruct `pd.DataFrame([r["posting"] for r in group])`, call
`jobspy.store_jobspy_results(brain_conn, df, source_label)` (the existing dedup/insert into the shared
brain), then `UPDATE discovered_postings SET synced_to_home_at=now()` for the pulled ids. Idempotent:
a re-pull skips already-synced rows; `store_jobspy_results` itself dedups by url so a replay is a no-op.
Returns the count of postings ingested. Mirrors `sync`'s write-home-then-mark-synced contract.

### 3.6 Entrypoints
- Worker: extend the existing worker runner so a `role='discovery'` worker uses
  `make_search_fn(...)` as its `search_fn` (a `applypilot-fleet-discovery` console script, or the
  compute worker main generalized — reuse `WorkerLoop(role='discovery', search_fn=...)`).
- Home: `applypilot-fleet-discovery-home` — `expand` (scheduler.expand_search_config from a searches
  config) + `pull` (sync.pull_discovered). Reuses the built scheduler.

## 4. Safety / honesty constraints
- **Different IPs from apply.** Discovery workers are lean + brain-less and meant to run on machines
  separate from the apply box (never scrape on the apply account — the account-safety rule). The fleet
  doesn't enforce the operator's IP topology, but the lean/no-brain design makes the separation natural.
- **Board-governed.** Scrape rate + breaker per `board:<name>` already exist (`lease_search` gates on
  the board scope); a scrape block trips the board breaker, not the apply lanes.
- **Brain stays the source of truth.** Workers never hold the brain; the home box owns the single write
  via `store_jobspy_results`. Postings are staged in PG, never written to a worker-local brain.
- **No jobspy.py change.** The adapter imports + calls jobspy helpers; the file is untouched.

## 5. Testing (disposable PG + temp SQLite, all scraping/brain stubbed)
- **Adapter:** `make_search_fn` with a stubbed `_scrape_with_retry` returning a small DataFrame →
  asserts the kwargs mapping (board→site_name, location, is_remote), the `_location_ok` filter applied,
  and `df.to_dict('records')` shape; a scrape exception → `[]`.
- **push_discovered:** inserts N rows into `discovered_postings` with the JSONB posting + task/worker.
- **_tick_discovery:** with a fake `search_fn` returning 2 postings, run one tick → 2 staging rows +
  the search task rescheduled (reuse the built search lease in a disposable-PG test).
- **pull_discovered:** seed `discovered_postings`, stub `jobspy.store_jobspy_results` to record the
  DataFrame it received → assert it got the postings (right rows, right label) and the rows are marked
  synced; a re-pull is a no-op (0).
- **End-to-end (disposable PG + temp brain, scrape+store stubbed):** expand a 1-search config →
  lease+tick a discovery worker (stub scrape) → staging rows → pull → the stubbed `store_jobspy_results`
  is called with the postings; staging marked synced.

## 6. Decided
- Option A (wrap JobSpy library/helpers + PG staging + home `store_jobspy_results`); lean brain-less
  workers; postings worker→PG→home→brain; reuse scheduler + governed search claim. **Decided.**
- Apply lane is OUT (separate catastrophe-class build, scoped next with the approval gate + canary).
