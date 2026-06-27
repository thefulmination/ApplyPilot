# Fleet Compute Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the tested fleet v3 compute lane to the real `score_job`/`audit_job`, so owner-controlled workers (local + cloud VMs) score the brain's backlog across multiple LLM providers, with results syncing back as advisory only — under the existing cost cap.

**Architecture:** A pure adapter (`compute_adapters.py`) turns a `compute_queue` payload into a call to the real scorer/auditor and back into the advisory result shape `sync.pull_compute_results` already reads. Shared context (resume/preference/KG/search-config) is served as versioned broker assets and cached per worker. The worker loop and queues already exist; this plan adds the adapter, multi-provider behavior (heterogeneous + failover + opt-in ensemble), a `task`-dispatch tweak, two entrypoints, and the minimal upstream touch-points.

**Tech Stack:** Python 3.11 (`.conda-env`), psycopg3, the disposable `applypilot-pgtest` Postgres (via the `fleet_db` conftest fixture), pytest. LLM layer = `src/applypilot/llm.py` (DeepSeek/Gemini/OpenAI/local).

## Global Constraints

- Run tests with `.conda-env/python.exe -m pytest <path> -q` from the repo root (`C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot`).
- PG-backed tests use the `fleet_db` fixture in `tests/conftest.py` (applies `ensure_schema_v3`, truncates between tests). New v3 tables/columns must be added to `schema_v3.sql`; new truncated tables must be added to `conftest._V3_TABLES`.
- **Commit only specific paths — NEVER `git add -A`.** The user's 7 dirty files (`run-applypilot.ps1`, `discovery/jobspy.py`, `pipeline.py`, `scoring/cover_letter.py`, `scoring/tailor.py`, `tests/test_discovery_scheduler.py`, `tests/test_generation_workers.py`) and sibling-session files must stay untouched.
- Allowed-to-modify upstream files (named touch-points only): `src/applypilot/scoring/scorer.py` (add an optional `provider` param to `score_job`). Everything else new lives under `src/applypilot/fleet/` and `tests/`.
- Advisory rule: compute results land in `research_fit_score` / `research_decision` only — NEVER `fit_score` / `audit_score`. `pull_compute_results` already enforces this; do not change it.
- Compute is IP-free: no browser, no site traffic in this lane. (Enrich is explicitly out of scope.)

## File Structure

- Create `src/applypilot/fleet/compute_adapters.py` — `ComputeContext`, `make_score_fn`, `make_audit_fn`, failover + ensemble. Pure wiring; no DB.
- Create `src/applypilot/fleet/compute_context.py` — publish/load the versioned shared-context assets (PG `fleet_assets`).
- Create `src/applypilot/fleet/compute_worker_main.py` — `build_compute_loop(env)` + `main()` (`applypilot-fleet-compute`).
- Create `src/applypilot/fleet/compute_home_main.py` — `push_backlog()` / `pull_results()` + `main()` (`applypilot-fleet-compute-home`).
- Modify `src/applypilot/fleet/queue.py` — `write_compute_result(..., provider=None)`.
- Modify `src/applypilot/fleet/worker.py` — `WorkerLoop` accepts `compute_fns` and `_tick_compute` dispatches by `job['task']`.
- Modify `src/applypilot/fleet/schema_v3.sql` — `ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS provider TEXT;`.
- Modify `src/applypilot/scoring/scorer.py` — `score_job(..., provider=None)`.
- Modify `src/applypilot/fleet/sync.py` — `push_compute_eligible` includes `full_description` in the payload.
- Tests: `tests/test_fleet_compute_adapters.py`, `tests/test_fleet_compute_context.py`, `tests/test_fleet_compute_worker.py`, `tests/test_fleet_compute_home.py`, and additions to `tests/test_fleet_v3_governor_queue.py` (provider column).

---

### Task 1: `score_job` optional provider override (upstream touch-point)

**Files:**
- Modify: `src/applypilot/scoring/scorer.py` (`score_job`, ~line 150 and ~line 197)
- Test: `tests/test_score_job_provider.py`

**Interfaces:**
- Produces: `scorer.score_job(resume_text, job, preference_profile=None, knowledge_graph_prompt=None, provider=None) -> dict`. When `provider` is set, the call routes to `get_client(stage="score", provider_override=provider)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_score_job_provider.py
from applypilot import scoring
from applypilot.scoring import scorer


def test_score_job_forwards_provider_override(monkeypatch):
    seen = {}

    class FakeClient:
        model = "gemini-2.0-flash"
        provider_name = "gemini"
        def chat(self, *a, **k):
            return '{"score": 8, "keywords": "ops", "reasoning": "fit"}'

    def fake_get_client(model_override=None, stage=None, provider_override=None):
        seen["stage"] = stage
        seen["provider_override"] = provider_override
        return FakeClient()

    monkeypatch.setattr(scorer, "get_client", fake_get_client)
    job = {"title": "Chief of Staff", "site": "Acme", "location": "Remote", "full_description": "ops"}
    out = scorer.score_job("RESUME", job, provider="gemini")
    assert seen["stage"] == "score" and seen["provider_override"] == "gemini"
    assert out["score"] == 8
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `.conda-env/python.exe -m pytest tests/test_score_job_provider.py -q`
Expected: FAIL (`score_job() got an unexpected keyword argument 'provider'`).

- [ ] **Step 3: Implement**

In `score_job`'s signature add `provider: str | None = None`. Change the client construction line from `client = get_client(stage="score")` to:

```python
        client = get_client(stage="score", provider_override=provider)
```

- [ ] **Step 4: Run it, expect PASS**

Run: `.conda-env/python.exe -m pytest tests/test_score_job_provider.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/scoring/scorer.py tests/test_score_job_provider.py
git commit -m "feat(scorer): optional provider override on score_job"
```

---

### Task 2: `ComputeContext` + `make_score_fn` (core wiring + cost capture)

**Files:**
- Create: `src/applypilot/fleet/compute_adapters.py`
- Test: `tests/test_fleet_compute_adapters.py`

**Interfaces:**
- Consumes: `scorer.score_job(..., provider=...)`; `llm.get_client`, `llm._estimate_cost`.
- Produces:
  - `ComputeContext(resume_text: str, preference_profile: dict | None, kg_prompt: str | None, search_cfg: dict | None, providers: list[str], fallback: list[str], ensemble: bool)`
  - `make_score_fn(ctx) -> Callable[[dict], tuple[dict, float]]` returning `(result, cost_usd)` where `result = {"task": "score", "research_fit_score": int|None, "research_decision": None, "keywords": str, "reasoning": str, "model": str, "provider": str, "status": "done"|"failed"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_compute_adapters.py
from applypilot.fleet import compute_adapters as ca


def _ctx(**kw):
    base = dict(resume_text="RESUME", preference_profile=None, kg_prompt=None,
                search_cfg=None, providers=["deepseek"], fallback=[], ensemble=False)
    base.update(kw); return ca.ComputeContext(**base)


def test_score_fn_maps_payload_and_captures_cost(monkeypatch):
    calls = {}
    def fake_score_job(resume, job, preference_profile=None, knowledge_graph_prompt=None, provider=None):
        calls["job"] = job; calls["provider"] = provider
        return {"score": 9, "keywords": "ops", "reasoning": "strong", "model": "deepseek-v4-flash", "provider": "deepseek"}
    class FakeClient:
        model = "deepseek-v4-flash"; last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
    monkeypatch.setattr(ca, "score_job", fake_score_job)
    monkeypatch.setattr(ca, "get_client", lambda **k: FakeClient())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.0004)

    payload = {"url": "u1", "company": "Acme", "title": "Chief of Staff",
               "application_url": "https://x", "full_description": "ops role"}
    score_fn = ca.make_score_fn(_ctx())
    result, cost = score_fn(payload)
    assert calls["job"]["title"] == "Chief of Staff" and calls["job"]["site"] == "Acme"
    assert calls["job"]["full_description"] == "ops role" and calls["provider"] == "deepseek"
    assert result["research_fit_score"] == 9 and result["status"] == "done"
    assert result["provider"] == "deepseek" and result["model"] == "deepseek-v4-flash"
    assert cost == 0.0004


def test_score_fn_maps_llm_error_to_failed(monkeypatch):
    monkeypatch.setattr(ca, "score_job", lambda *a, **k: {"score": 0, "keywords": "", "reasoning": "x", "error": "429"})
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "m", "last_usage": None})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: None)
    score_fn = ca.make_score_fn(_ctx())
    result, cost = score_fn({"url": "u", "company": "C", "title": "T", "full_description": "d"})
    assert result["status"] == "failed" and result["research_fit_score"] is None
    assert cost == 0.0
```

- [ ] **Step 2: Run it, expect FAIL** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_adapters.py -q` (ModuleNotFound).

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/compute_adapters.py
"""Pure wiring between the fleet compute_queue and the real scorer/auditor.

A compute job payload (url/company/title/application_url/full_description) is mapped
to a score_job/audit_job call and back into the advisory result shape that
sync.pull_compute_results reads (research_fit_score / research_decision). No DB here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from applypilot.llm import get_client, _estimate_cost as estimate_cost
from applypilot.scoring.scorer import score_job
from applypilot.scoring.audit import audit_job


@dataclass
class ComputeContext:
    resume_text: str
    preference_profile: dict | None = None
    kg_prompt: str | None = None
    search_cfg: dict | None = None
    providers: list[str] = field(default_factory=list)  # ordered; providers[0] is primary
    fallback: list[str] = field(default_factory=list)    # tried in order on an error result
    ensemble: bool = False


def _job_from_payload(payload: dict) -> dict:
    return {
        "title": payload.get("title") or "",
        "site": payload.get("company") or payload.get("site") or "",
        "location": payload.get("location") or "N/A",
        "full_description": payload.get("full_description") or "",
        "fit_score": payload.get("fit_score"),
    }


def _score_once(ctx: ComputeContext, job: dict, provider: str | None) -> tuple[dict, float]:
    raw = score_job(ctx.resume_text, job, ctx.preference_profile, ctx.kg_prompt, provider=provider)
    client = get_client(stage="score", provider_override=provider)
    cost = estimate_cost(getattr(client, "model", None), getattr(client, "last_usage", None)) or 0.0
    return raw, float(cost)


def make_score_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    primary = ctx.providers[0] if ctx.providers else None

    def score_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        raw, cost = _score_once(ctx, job, primary)
        provider = raw.get("provider") or primary
        if raw.get("error") or int(raw.get("score") or 0) <= 0:
            return ({"task": "score", "research_fit_score": None, "research_decision": None,
                     "keywords": "", "reasoning": raw.get("reasoning") or raw.get("error") or "",
                     "model": raw.get("model"), "provider": provider, "status": "failed"}, cost)
        return ({"task": "score", "research_fit_score": int(raw["score"]), "research_decision": None,
                 "keywords": raw.get("keywords", ""), "reasoning": raw.get("reasoning", ""),
                 "model": raw.get("model"), "provider": provider, "status": "done"}, cost)

    return score_fn
```

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_adapters.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_adapters.py tests/test_fleet_compute_adapters.py
git commit -m "feat(fleet): compute score adapter (payload->score_job->advisory + cost)"
```

---

### Task 3: `make_audit_fn`

**Files:**
- Modify: `src/applypilot/fleet/compute_adapters.py`
- Test: `tests/test_fleet_compute_adapters.py`

**Interfaces:**
- Produces: `make_audit_fn(ctx) -> Callable[[dict], tuple[dict, float]]`; result `{"task": "audit", "research_fit_score": None, "research_decision": <audit_label>, "audit_score": float, "flags": list, "reason": str, "status": "done"}`, cost always `0.0` (deterministic, no LLM).

- [ ] **Step 1: Write the failing test** (audit_job is deterministic — call it for real)

```python
def test_audit_fn_maps_scoreaudit_to_decision():
    payload = {"url": "u", "company": "Acme", "title": "Chief of Staff",
               "full_description": "operations leadership", "fit_score": 8}
    audit_fn = ca.make_audit_fn(_ctx())
    result, cost = audit_fn(payload)
    assert result["task"] == "audit" and result["status"] == "done"
    assert isinstance(result["research_decision"], str) and result["research_fit_score"] is None
    assert "audit_score" in result and isinstance(result["flags"], list)
    assert cost == 0.0
```

- [ ] **Step 2: Run it, expect FAIL** (`make_audit_fn` undefined).

- [ ] **Step 3: Implement** (append to `compute_adapters.py`)

```python
def make_audit_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    def audit_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        a = audit_job(job, ctx.search_cfg)
        return ({"task": "audit", "research_fit_score": None, "research_decision": a.audit_label,
                 "audit_score": a.audit_score, "role_fit_score": a.role_fit_score,
                 "flags": list(a.flags), "reason": a.reason, "status": "done"}, 0.0)
    return audit_fn
```

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_adapters.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_adapters.py tests/test_fleet_compute_adapters.py
git commit -m "feat(fleet): compute audit adapter (audit_job->research_decision)"
```

---

### Task 4: Cross-provider failover in the score adapter

**Files:**
- Modify: `src/applypilot/fleet/compute_adapters.py` (`make_score_fn`)
- Test: `tests/test_fleet_compute_adapters.py`

**Interfaces:**
- Produces: when the primary provider returns an error/zero result, `score_fn` retries the same job on each provider in `ctx.fallback` (in order) and returns the first successful result; the result's `provider` reflects which one succeeded.

- [ ] **Step 1: Write the failing test**

```python
def test_score_fn_fails_over_to_next_provider(monkeypatch):
    def fake_score_job(resume, job, pref=None, kg=None, provider=None):
        if provider == "deepseek":
            return {"score": 0, "error": "429", "reasoning": "rate limited"}
        return {"score": 7, "keywords": "k", "reasoning": "ok", "model": "gemini-2.0-flash", "provider": "gemini"}
    monkeypatch.setattr(ca, "score_job", fake_score_job)
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "m", "last_usage": None})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.0)
    score_fn = ca.make_score_fn(_ctx(providers=["deepseek"], fallback=["gemini"]))
    result, _ = score_fn({"url": "u", "company": "C", "title": "T", "full_description": "d"})
    assert result["status"] == "done" and result["research_fit_score"] == 7
    assert result["provider"] == "gemini"
```

- [ ] **Step 2: Run it, expect FAIL** (no failover yet — returns `failed`).

- [ ] **Step 3: Implement** (replace `make_score_fn`'s body)

```python
def make_score_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    primary = ctx.providers[0] if ctx.providers else None
    chain = [primary] + list(ctx.fallback)

    def _build(raw, provider, cost, status):
        ok = status == "done"
        return ({"task": "score",
                 "research_fit_score": int(raw["score"]) if ok else None,
                 "research_decision": None,
                 "keywords": raw.get("keywords", "") if ok else "",
                 "reasoning": raw.get("reasoning") or raw.get("error") or "",
                 "model": raw.get("model"), "provider": provider, "status": status}, cost)

    def score_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        total_cost = 0.0
        last = None
        for provider in chain:
            raw, cost = _score_once(ctx, job, provider)
            total_cost += cost
            prov = raw.get("provider") or provider
            if not raw.get("error") and int(raw.get("score") or 0) > 0:
                return _build(raw, prov, total_cost, "done")
            last = (raw, prov)
        raw, prov = last
        return _build(raw, prov, total_cost, "failed")

    return score_fn
```

- [ ] **Step 4: Run it, expect PASS** (and the Task 2 error test still passes — no fallback configured → single attempt → failed). Run the whole file.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_adapters.py tests/test_fleet_compute_adapters.py
git commit -m "feat(fleet): cross-provider failover in the score adapter"
```

---

### Task 5: Opt-in ensemble (A/B compare)

**Files:**
- Modify: `src/applypilot/fleet/compute_adapters.py`
- Test: `tests/test_fleet_compute_adapters.py`

**Interfaces:**
- Produces: when `ctx.ensemble` is True and `len(ctx.providers) >= 2`, `score_fn` scores the job on every provider in `ctx.providers`, and the result adds `"ensemble": [{"provider", "score"}...]`, `"agreement": float` (1.0 = identical scores; lower = more spread), and `research_fit_score` = rounded mean of the successful scores. Cost = sum across providers.

- [ ] **Step 1: Write the failing test**

```python
def test_ensemble_scores_all_providers_and_aggregates(monkeypatch):
    def fake_score_job(resume, job, pref=None, kg=None, provider=None):
        return {"score": {"deepseek": 8, "gemini": 6}[provider], "keywords": "k",
                "reasoning": "r", "model": provider, "provider": provider}
    monkeypatch.setattr(ca, "score_job", fake_score_job)
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "m", "last_usage": None})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.001)
    score_fn = ca.make_score_fn(_ctx(providers=["deepseek", "gemini"], ensemble=True))
    result, cost = score_fn({"url": "u", "company": "C", "title": "T", "full_description": "d"})
    assert result["research_fit_score"] == 7  # round(mean(8,6))
    assert {e["provider"] for e in result["ensemble"]} == {"deepseek", "gemini"}
    assert 0.0 <= result["agreement"] <= 1.0
    assert round(cost, 4) == 0.002
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement** — in `score_fn`, before the failover loop, branch on ensemble:

```python
    def score_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        if ctx.ensemble and len(ctx.providers) >= 2:
            members, total_cost, scores = [], 0.0, []
            for provider in ctx.providers:
                raw, cost = _score_once(ctx, job, provider)
                total_cost += cost
                s = int(raw.get("score") or 0)
                if not raw.get("error") and s > 0:
                    members.append({"provider": raw.get("provider") or provider, "score": s})
                    scores.append(s)
            if not scores:
                return _build({"reasoning": "ensemble: all providers failed"}, ctx.providers[0], total_cost, "failed")
            mean = sum(scores) / len(scores)
            spread = (max(scores) - min(scores)) / 9.0  # 1-10 scale span
            res = {"task": "score", "research_fit_score": round(mean), "research_decision": None,
                   "keywords": "", "reasoning": "ensemble", "model": None,
                   "provider": "+".join(m["provider"] for m in members),
                   "ensemble": members, "agreement": round(1.0 - spread, 3), "status": "done"}
            return res, total_cost
        # ... (the failover loop from Task 4 follows unchanged)
```

(Keep the Task 4 failover loop as the non-ensemble path below this branch.)

- [ ] **Step 4: Run it, expect PASS** (run the whole adapters test file).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_adapters.py tests/test_fleet_compute_adapters.py
git commit -m "feat(fleet): opt-in ensemble scoring with agreement metric"
```

---

### Task 6: `write_compute_result` provider column + `WorkerLoop` task dispatch

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql` (after the `llm_usage` table)
- Modify: `src/applypilot/fleet/queue.py` (`write_compute_result`)
- Modify: `src/applypilot/fleet/worker.py` (`WorkerLoop.__init__`, `_tick_compute`)
- Test: `tests/test_fleet_compute_worker.py`

**Interfaces:**
- Consumes: `make_score_fn`, `make_audit_fn`.
- Produces:
  - `queue.write_compute_result(conn, worker_id, url, *, result, status="done", cost_usd=0, model=None, provider=None, task=None, machine_owner=None, tokens_in=None, tokens_out=None)` — records `provider` in `llm_usage`.
  - `WorkerLoop(..., compute_fns: dict[str, Callable] | None = None)`; `_tick_compute` selects `compute_fns[job["task"]]` (falling back to `score_fn` for `task == "score"` when only `score_fn` was given). The result's `status`/`provider` are forwarded to `write_compute_result`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_compute_worker.py
from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def _factory(dsn):
    return lambda: pgqueue.connect(dsn)


def test_compute_worker_routes_audit_task_and_records_provider(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c-audit", "task": "audit",
                                        "payload": {"title": "Chief of Staff", "company": "Acme",
                                                    "full_description": "ops", "fit_score": 8}}])
        queue.push_compute_jobs(conn, [{"url": "c-score", "task": "score",
                                        "payload": {"title": "COS", "company": "Acme", "full_description": "ops"}}])

    fns = {
        "audit": lambda payload: ({"task": "audit", "research_decision": "qualified", "status": "done"}, 0.0),
        "score": lambda payload: ({"task": "score", "research_fit_score": 9, "model": "deepseek-v4-flash",
                                   "provider": "deepseek", "status": "done"}, 0.0003),
    }
    loop = WorkerLoop(_factory(fleet_db), "w-c", home_ip="1.1.1.1", role="compute", compute_fns=fns)
    a1 = loop.run_once(); a2 = loop.run_once()
    assert {a1["action"], a2["action"]} == {"compute_done"}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, result FROM compute_queue WHERE url='c-audit'")
        r = cur.fetchone(); assert r["status"] == "done" and r["result"]["research_decision"] == "qualified"
        cur.execute("SELECT provider, model FROM llm_usage WHERE task='score'")
        u = cur.fetchone(); assert u["provider"] == "deepseek" and u["model"] == "deepseek-v4-flash"
```

- [ ] **Step 2: Run it, expect FAIL** (`compute_fns` unknown / `provider` column missing).

- [ ] **Step 3: Implement**

In `schema_v3.sql`, after the `llm_usage` `CREATE TABLE`:

```sql
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS provider TEXT;
```

In `queue.write_compute_result`, add `provider=None` to the signature and include it in the `llm_usage` INSERT:

```python
def write_compute_result(conn, worker_id, url, *, result, status="done", cost_usd=0,
                         model=None, provider=None, task=None, machine_owner=None,
                         tokens_in=None, tokens_out=None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE compute_queue SET status=%s, result=%s, est_cost_usd=COALESCE(%s,0), updated_at=now() "
            "WHERE url=%s AND lease_owner=%s",
            (status, json.dumps(result) if result is not None else None, cost_usd, url, worker_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        cur.execute(
            "INSERT INTO llm_usage (worker_id, machine_owner, task, model, provider, tokens_in, tokens_out, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (worker_id, machine_owner, task, model, provider, tokens_in, tokens_out, cost_usd),
        )
    conn.commit()
    return True
```

In `worker.WorkerLoop.__init__`, add `compute_fns: dict | None = None` and store `self.compute_fns = compute_fns or ({"score": score_fn} if score_fn else {})`. Replace `_tick_compute` body's scoring section:

```python
    def _tick_compute(self, conn) -> dict:
        job = queue.lease_compute(conn, self.worker_id)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        task = job.get("task") or "score"
        fn = self.compute_fns.get(task)
        if fn is None:
            raise RuntimeError(f"compute role has no handler for task {task!r}")
        self._beat(conn, state="computing", current_job=job["url"])
        result, cost = fn(job.get("payload") or {"url": job["url"]})
        queue.write_compute_result(
            conn, self.worker_id, job["url"], result=result, status=result.get("status", "done"),
            cost_usd=cost or 0, model=result.get("model"), provider=result.get("provider"),
            task=task, machine_owner=self.machine_owner,
        )
        self._beat(conn, state="idle")
        return {"action": "compute_done", "url": job["url"], "task": task, "cost_usd": cost or 0}
```

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_worker.py -q`. Then run `tests/test_fleet_v3_worker.py` to confirm the existing compute test still passes (the `score_fn`→`compute_fns` shim keeps it green).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/queue.py src/applypilot/fleet/worker.py tests/test_fleet_compute_worker.py
git commit -m "feat(fleet): compute task-dispatch + provider in the usage ledger"
```

---

### Task 7: Versioned shared-context assets

**Files:**
- Create: `src/applypilot/fleet/compute_context.py`
- Test: `tests/test_fleet_compute_context.py`

**Interfaces:**
- Consumes: `pgqueue.put_asset(conn, name, data: bytes)`, `pgqueue.get_asset(conn, name) -> bytes | None`; `ComputeContext`.
- Produces:
  - `publish_context(conn, *, resume_text, preference_profile, kg_prompt, search_cfg, version: str) -> None` — writes assets `ctx:resume`, `ctx:preference`, `ctx:kg_prompt`, `ctx:search_cfg`, and `ctx:version`.
  - `load_context(conn, *, providers, fallback=(), ensemble=False) -> tuple[ComputeContext, str]` — fetches the assets, returns `(ComputeContext, version)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fleet_compute_context.py
import json
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc


def test_publish_then_load_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        cc.publish_context(conn, resume_text="RESUME", preference_profile={"weights": 1},
                           kg_prompt="KG", search_cfg={"score_audit": {}}, version="v1")
        ctx, version = cc.load_context(conn, providers=["deepseek"], fallback=["gemini"], ensemble=True)
    assert version == "v1"
    assert ctx.resume_text == "RESUME" and ctx.kg_prompt == "KG"
    assert ctx.preference_profile == {"weights": 1} and ctx.search_cfg == {"score_audit": {}}
    assert ctx.providers == ["deepseek"] and ctx.fallback == ["gemini"] and ctx.ensemble is True


def test_load_missing_context_returns_empty_version(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        ctx, version = cc.load_context(conn, providers=["deepseek"])
    assert version == "" and ctx.resume_text == ""
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/compute_context.py
"""Versioned shared-context assets (resume / preference / KG prompt / search config)
served through the broker's fleet_assets blob store. Workers fetch once and re-fetch
on a version change; the brain never lands on a worker disk persistently."""
from __future__ import annotations

import json

from applypilot.apply import pgqueue
from applypilot.fleet.compute_adapters import ComputeContext

_RESUME, _PREF, _KG, _CFG, _VER = "ctx:resume", "ctx:preference", "ctx:kg_prompt", "ctx:search_cfg", "ctx:version"


def _b(s: str | None) -> bytes:
    return (s or "").encode("utf-8")


def publish_context(conn, *, resume_text, preference_profile, kg_prompt, search_cfg, version) -> None:
    pgqueue.put_asset(conn, _RESUME, _b(resume_text))
    pgqueue.put_asset(conn, _PREF, _b(json.dumps(preference_profile or {})))
    pgqueue.put_asset(conn, _KG, _b(kg_prompt))
    pgqueue.put_asset(conn, _CFG, _b(json.dumps(search_cfg or {})))
    pgqueue.put_asset(conn, _VER, _b(version))


def _txt(conn, name) -> str:
    raw = pgqueue.get_asset(conn, name)
    return raw.decode("utf-8") if raw else ""


def load_context(conn, *, providers, fallback=(), ensemble=False) -> tuple[ComputeContext, str]:
    version = _txt(conn, _VER)
    pref = _txt(conn, _PREF); cfg = _txt(conn, _CFG)
    ctx = ComputeContext(
        resume_text=_txt(conn, _RESUME),
        preference_profile=json.loads(pref) if pref else None,
        kg_prompt=_txt(conn, _KG) or None,
        search_cfg=json.loads(cfg) if cfg else None,
        providers=list(providers), fallback=list(fallback), ensemble=bool(ensemble),
    )
    return ctx, version
```

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_context.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_context.py tests/test_fleet_compute_context.py
git commit -m "feat(fleet): versioned shared-context assets for compute workers"
```

---

### Task 8: Worker entrypoint `applypilot-fleet-compute`

**Files:**
- Create: `src/applypilot/fleet/compute_worker_main.py`
- Modify: `pyproject.toml` (console_scripts) OR `setup.cfg`/`setup.py` — whichever the repo uses
- Test: `tests/test_fleet_compute_worker.py`

**Interfaces:**
- Consumes: `compute_context.load_context`, `make_score_fn`/`make_audit_fn`, `WorkerLoop`.
- Produces: `build_compute_loop(conn, *, dsn, worker_id, home_ip, providers, fallback, ensemble, machine_owner=None) -> WorkerLoop` and `main()`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_compute_loop_wires_both_handlers(fleet_db):
    from applypilot.fleet import compute_context as cc
    from applypilot.fleet import compute_worker_main as cwm
    from applypilot.apply import pgqueue
    with pgqueue.connect(fleet_db) as conn:
        cc.publish_context(conn, resume_text="R", preference_profile={}, kg_prompt="KG",
                           search_cfg={}, version="v1")
    with pgqueue.connect(fleet_db) as conn:
        loop = cwm.build_compute_loop(conn, dsn=fleet_db, worker_id="w1", home_ip="1.1.1.1",
                                      providers=["deepseek"], fallback=[], ensemble=False)
    assert set(loop.compute_fns) == {"score", "audit"} and loop.role == "compute"
```

- [ ] **Step 2: Run it, expect FAIL.**

- [ ] **Step 3: Implement**

```python
# src/applypilot/fleet/compute_worker_main.py
"""applypilot-fleet-compute: a compute worker (score + audit) for owner-controlled
machines. Reads PG DSN + LLM key/provider from the local env, loads the shared
context, and runs the WorkerLoop. Compute is IP-free (no browser, no site traffic)."""
from __future__ import annotations

import argparse
import os

from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet.compute_adapters import make_audit_fn, make_score_fn
from applypilot.fleet.worker import WorkerLoop


def build_compute_loop(conn, *, dsn, worker_id, home_ip, providers, fallback, ensemble,
                       machine_owner=None) -> WorkerLoop:
    ctx, _version = cc.load_context(conn, providers=providers, fallback=fallback, ensemble=ensemble)
    fns = {"score": make_score_fn(ctx), "audit": make_audit_fn(ctx)}
    return WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="compute",
                      compute_fns=fns, machine_owner=machine_owner)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--home-ip", default=os.environ.get("FLEET_HOME_IP", "0.0.0.0"))
    p.add_argument("--providers", default=os.environ.get("LLM_SCORE_PROVIDER", "deepseek"))
    p.add_argument("--fallback", default=os.environ.get("LLM_SCORE_FALLBACK", ""))
    p.add_argument("--ensemble", action="store_true", default=bool(os.environ.get("FLEET_ENSEMBLE")))
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER"))
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    providers = [s for s in args.providers.split(",") if s]
    fallback = [s for s in args.fallback.split(",") if s]
    with pgqueue.connect(args.dsn) as conn:
        loop = build_compute_loop(conn, dsn=args.dsn, worker_id=args.worker_id, home_ip=args.home_ip,
                                  providers=providers, fallback=fallback, ensemble=args.ensemble,
                                  machine_owner=args.machine_owner)
    loop.run_forever()
    return 0
```

Register the console script (match the repo's existing entrypoint style — check `pyproject.toml [project.scripts]` for the pattern already used by `applypilot`): add
`applypilot-fleet-compute = "applypilot.fleet.compute_worker_main:main"`.

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_worker.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_worker_main.py pyproject.toml tests/test_fleet_compute_worker.py
git commit -m "feat(fleet): applypilot-fleet-compute worker entrypoint"
```

---

### Task 9: Home driver `applypilot-fleet-compute-home` (+ payload `full_description`)

**Files:**
- Create: `src/applypilot/fleet/compute_home_main.py`
- Modify: `src/applypilot/fleet/sync.py` (`push_compute_eligible` payload includes `full_description`)
- Modify: `pyproject.toml`
- Test: `tests/test_fleet_compute_home.py`

**Interfaces:**
- Consumes: `sync.push_compute_eligible`, `sync.pull_compute_results`.
- Produces: `push_backlog(*, sqlite_conn, pg_conn, task, score_floor, limit) -> int`, `pull_results(*, sqlite_conn, pg_conn) -> int`, and `main()`.

- [ ] **Step 1: Write the failing test** (mirror `tests/test_fleet_v3_sync.py`'s temp-SQLite pattern)

```python
# tests/test_fleet_compute_home.py
import sqlite3
from applypilot.apply import pgqueue
from applypilot.fleet import compute_home_main as chm

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT);"""


def test_push_backlog_includes_full_description(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, full_description) "
               "VALUES ('u1','Acme','COS','https://x',8.0,'the full JD')"); sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7, limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT payload FROM compute_queue WHERE url='u1'")
            assert cur.fetchone()["payload"]["full_description"] == "the full JD"
```

- [ ] **Step 2: Run it, expect FAIL** (no `compute_home_main`; payload lacks `full_description`).

- [ ] **Step 3: Implement**

In `sync.py`, the `_PUSH_COMPUTE_SELECT` already selects `url, company, title, application_url`. Add `full_description` to that SELECT and to the payload dict in `push_compute_eligible`:

```python
# _PUSH_COMPUTE_SELECT: add full_description to the column list
#   SELECT url, company, title, application_url, full_description, CAST(...) AS score
# push_compute_eligible payload:
                "payload": {
                    "url": r["url"], "company": r["company"], "title": r["title"],
                    "application_url": r["application_url"], "full_description": r["full_description"],
                },
```

Create the driver:

```python
# src/applypilot/fleet/compute_home_main.py
"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse

from applypilot.fleet import sync


def push_backlog(*, sqlite_conn=None, pg_conn=None, task="score", score_floor=7, limit=None) -> int:
    return sync.push_compute_eligible(sqlite_conn=sqlite_conn, pg_conn=pg_conn,
                                      task=task, score_floor=score_floor, limit=limit)


def pull_results(*, sqlite_conn=None, pg_conn=None) -> int:
    return sync.pull_compute_results(sqlite_conn=sqlite_conn, pg_conn=pg_conn)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)
    if args.cmd == "push":
        print("pushed", push_backlog(task=args.task, score_floor=args.score_floor, limit=args.limit))
    else:
        print("pulled", pull_results())
    return 0
```

Register `applypilot-fleet-compute-home = "applypilot.fleet.compute_home_main:main"` in `pyproject.toml`.

- [ ] **Step 4: Run it, expect PASS** — `.conda-env/python.exe -m pytest tests/test_fleet_compute_home.py -q`. Then run `tests/test_fleet_v3_sync.py` to confirm the `full_description` SELECT change didn't break the compute-push test (its temp DDL must include `full_description` — update that one test's DDL if needed, it is the build's own test file).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/compute_home_main.py src/applypilot/fleet/sync.py pyproject.toml tests/test_fleet_compute_home.py tests/test_fleet_v3_sync.py
git commit -m "feat(fleet): compute-home push/pull driver + full_description payload"
```

---

### Task 10: End-to-end (stubbed LLM, no spend) + opt-in live smoke

**Files:**
- Test: `tests/test_fleet_compute_e2e.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test** — full loop with a stubbed `score_job`

```python
# tests/test_fleet_compute_e2e.py
import sqlite3
import pytest
from applypilot.apply import pgqueue
from applypilot.fleet import compute_adapters as ca, compute_context as cc, sync
from applypilot.fleet.worker import WorkerLoop

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT);"""


def test_compute_lane_end_to_end_advisory(fleet_db, tmp_path, monkeypatch):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, fit_score, full_description) "
               "VALUES ('u1','Acme','Chief of Staff','https://x',8.0,8,'operations leadership')"); sq.commit()

    monkeypatch.setattr(ca, "score_job", lambda *a, **k: {"score": 9, "keywords": "ops",
                        "reasoning": "strong", "model": "deepseek-v4-flash", "provider": "deepseek"})
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "deepseek-v4-flash",
                        "last_usage": {"prompt_tokens": 50, "completion_tokens": 10}})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.0002)

    with pgqueue.connect(fleet_db) as pg:
        cc.publish_context(pg, resume_text="R", preference_profile={}, kg_prompt="KG",
                           search_cfg={}, version="v1")
        sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7)
        ctx, _ = cc.load_context(pg, providers=["deepseek"])

    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w-e2e", home_ip="1.1.1.1", role="compute",
                      compute_fns={"score": ca.make_score_fn(ctx)})
    assert loop.run_once()["action"] == "compute_done"

    with pgqueue.connect(fleet_db) as pg:
        n = sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg)
    assert n == 1
    row = sq.execute("SELECT research_fit_score, fit_score, audit_score FROM jobs WHERE url='u1'").fetchone()
    assert row["research_fit_score"] == 9          # advisory written
    assert row["fit_score"] == 8 and row["audit_score"] == 8.0   # NEVER auto-promoted

    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT provider, cost_usd FROM llm_usage WHERE worker_id='w-e2e'")
        u = cur.fetchone(); assert u["provider"] == "deepseek" and float(u["cost_usd"]) == 0.0002


@pytest.mark.skipif("not __import__('os').environ.get('DEEPSEEK_API_KEY')",
                    reason="live smoke needs a real key")
def test_live_smoke_scores_one_real_job():
    from applypilot.fleet.compute_adapters import ComputeContext, make_score_fn
    fn = make_score_fn(ComputeContext(resume_text="Operations leader, 9 yrs.", providers=["deepseek"]))
    result, cost = fn({"title": "Chief of Staff", "company": "Acme", "full_description": "Lead ops."})
    assert result["status"] in {"done", "failed"} and cost >= 0
```

- [ ] **Step 2: Run it, expect FAIL** (then iterate until the wiring passes).

- [ ] **Step 3: Implement** — no new code; fix any wiring mismatch surfaced by the test.

- [ ] **Step 4: Run the full fleet suite**

Run: `.conda-env/python.exe -m pytest tests/test_fleet_v3_*.py tests/test_fleet_compute_*.py tests/test_fleet_pgqueue.py tests/test_score_job_provider.py -q`
Expected: all pass (110 prior + the new compute tests). The live smoke is skipped without a key.

- [ ] **Step 5: Commit & push**

```bash
git add tests/test_fleet_compute_e2e.py
git commit -m "test(fleet): compute lane end-to-end advisory + live smoke (gated)"
git push private applypilot-hardening-and-brainstorm-integration
```

---

## Self-Review

**Spec coverage:** §3 architecture → Tasks 2/3/6/10; §4.1 adapter → Tasks 2–5; §4.2 versioned assets → Task 7; §4.3 worker runner → Task 8; §4.4 home driver → Task 9; §4.5 touch-points → Tasks 1 & 6 (`score_job` provider, `_tick_compute` dispatch, `provider` field, cost-not-from-brain → Task 2 reads `client.last_usage`/`estimate_cost`); §5 multi-provider → Tasks 4 (failover) & 5 (ensemble) & 6 (recording); §6 cost → Tasks 2 & 6 (`cost_usd` to `llm_usage`, cap already gates `lease_compute`); §7 error/recovery → Task 2 error mapping (reclaim/quarantine already built); §8 testing → every task + Task 10. No gaps.

**Placeholder scan:** every code step contains real code; commands are exact. The only "match the repo" note is the console-script registration in Tasks 8/9 — the engineer must read `pyproject.toml [project.scripts]` and follow the existing `applypilot = ...` pattern.

**Type consistency:** `ComputeContext` fields are identical in Tasks 2/3/7/8/10; `make_score_fn`/`make_audit_fn` return `(result_dict, cost_float)` everywhere; result dicts always carry `status`; `write_compute_result(..., provider=...)` matches its caller in `_tick_compute`; `score_job(..., provider=None)` (Task 1) matches the adapter call (Task 2).
