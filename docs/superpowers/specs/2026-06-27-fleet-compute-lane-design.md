# Fleet Compute Lane — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the tested fleet v3 foundation (`src/applypilot/fleet/`, 110 PG tests). See
[`2026-06-26-distributed-residential-fleet-design.md`](2026-06-26-distributed-residential-fleet-design.md)
and [`2026-06-27-fleet-v3-build-report.md`](2026-06-27-fleet-v3-build-report.md).

## 1. Goal & success criteria

Take the v3 **compute lane** from a tested scaffold to actually doing work: distribute the
brain's LLM **scoring** (and the deterministic **audit** re-rank) across machines the owner
controls — local boxes plus cheap ephemeral cloud VMs — each running a compute worker that
leases jobs from Postgres, calls the **real** `scorer.score_job` / `audit.audit_job`, and whose
results sync back to the brain as **advisory** (`research_fit_score` / `research_decision`),
governed by the existing cost cap.

**Done when:** one local worker + one cloud-VM worker score a batch end-to-end → results land in
the brain as advisory (never auto-promoted to `fit_score`/`audit_score`) → the `llm_usage` ledger
reflects real spend → the daily/total cost cap halts leasing when hit. Verified by an automated
end-to-end test (stubbed LLM, no spend) plus one opt-in live smoke test.

**Non-goals:** enrichment (it drives a browser to job sites → IP-sensitive → belongs in the
residential/discovery lane), the apply lane, the Gmail OTP relay, the Helper app, and any
friend-machine deployment of compute (the code supports it; deployment is deferred).

## 2. Why compute is IP-free (and the deployment consequence)

`score_job` is one DeepSeek/Gemini/OpenAI API call (no site visit); `audit_job` is deterministic
KG/pattern matching (no network). Neither touches a target site, so compute carries **no captcha
or IP-ban risk** and does **not** need residential/friend machines. It runs on owner-controlled
infrastructure (local + cloud VMs) with the owner's key(s). Friends are reserved for the apply
lane (residential IPs). v1 talks to Postgres **directly** (Topology A, trusted machines); the
broker-RPC path (already built) is the drop-in friend-safe generalization, deferred.

## 3. Architecture (reuses the built foundation)

```
SQLite brain ──push_compute_eligible(score|audit)──► Postgres compute_queue
                                                         │ lease_compute (cost-capped)
                                                         ▼
                                            Compute Worker (WorkerLoop role=compute)
                                              compute_fns = {score: score_fn, audit: audit_fn}
                                              score_fn → scorer.score_job(...)  (multi-provider)
                                              write_compute_result(result, cost, model, provider, tokens)
                                                         │  + llm_usage ledger (cost cap)
SQLite brain ◄──pull_compute_results (advisory only)─────┘  (reclaim_compute requeues crashes)
```

**Already exists & tested:** `compute_queue`, `lease_compute`, `write_compute_result`,
`_cost_cap_exceeded`, `llm_usage`, `reclaim_compute`, poison-quarantine, and
`sync.push_compute_eligible` / `sync.pull_compute_results` (advisory-only, never promotes a score).

## 4. New components

### 4.1 `fleet/compute_adapters.py` — the wiring (pure, unit-testable)
- `make_score_fn(ctx) -> score_fn(payload) -> (result, cost_usd)`:
  builds the `job` dict `score_job` wants (`title`, `site`, `location`, `full_description`) from
  the `compute_queue` payload; calls `score_job(ctx.resume, job, ctx.preference, ctx.kg_prompt,
  provider=...)`; captures token usage + cost (from the client / `_estimate_cost`); returns
  `({"research_fit_score": score, "research_decision": None, "keywords", "reasoning", "model",
  "provider"}, cost_usd)`. The result shape is exactly what `pull_compute_results` reads.
- `make_audit_fn(ctx) -> audit_fn(payload) -> (result, 0.0)`: wraps `audit_job(job, ctx.search_cfg)`;
  maps the `ScoreAudit` to `{"research_decision": <verdict/rank>, ...}`; cost 0 (deterministic).
- Error mapping: `score_job` returns an error dict (`score=0, error=...`) on LLM failure → the
  adapter returns a `failed` compute result (status `failed`), never advisory-promoted.

### 4.2 Shared-context delivery (versioned assets)
`score_job` needs `resume_text` + `preference_profile` + `kg_prompt`; `audit_job` needs
`search_cfg` (+ KG). These are identical for every job, so they are served as **broker assets**
(reuse `fleet_assets` + `put_asset`/`get_asset`/`broker.fetch_assets`): `resume.txt`,
`preference_profile.json`, `kg_prompt.txt`, `search_cfg.json`, each tagged with a **content
version**. The worker fetches them once at startup, caches in memory, and **re-fetches on a
version bump** (polled via `get_config`/an asset-version field). The per-job `full_description`
rides in the `compute_queue` payload (`push_compute_eligible` is extended to include it). This
keeps the brain off the worker disk; the KG is consumed, never built, by the fleet.

### 4.3 Compute worker runner + key plumbing
A thin entrypoint `applypilot-fleet-compute` (console script / `python -m`):
read PG DSN + the LLM key(s) (`DEEPSEEK_API_KEY`/`GEMINI_API_KEY`/`OPENAI_API_KEY`) +
`LLM_SCORE_PROVIDER` (+ optional `LLM_SCORE_FALLBACK`) from local env; load/ensure the worker row
(capability `{can_compute: true}`, plus its provider tag); fetch the context assets; build
`WorkerLoop(role='compute', compute_fns={'score': score_fn, 'audit': audit_fn}, ...)`; run
`run_forever`, heartbeating, honoring `get_config` (paused/version) + the cost cap.

### 4.4 Home driver
`applypilot-fleet-compute-home`: `push` (fill `compute_queue` from the backlog for score/audit,
honoring a score floor + limit pushed into SQL) and `pull` (advisory results → brain). One-shot or
loop. Reuses `sync.push_compute_eligible` / `sync.pull_compute_results`.

### 4.5 Small upstream + worker touch-points
- `scorer.score_job(..., provider: str | None = None)` — optional provider override so the adapter
  can re-invoke on a chosen provider (failover/ensemble). Defaults to current behavior; benefits the
  live tool too.
- `worker._tick_compute` — dispatch by `job['task']` to the matching entry in `compute_fns`
  (currently always calls a single `score_fn`). `write_compute_result` gains a `provider` field.
- **Cost capture must not depend on the brain.** The adapter reads the cost from the client's
  `last_usage` + `_estimate_cost` directly and passes it to `write_compute_result` (→ fleet
  `llm_usage` in Postgres). It must NOT rely on the live tool's brain-side `record_llm_usage`
  (SQLite) firing — a VM worker has no brain. The plan confirms `score_job`/the client expose usage
  without a brain write, or estimates from token counts × the provider price table.

## 5. Multi-provider scoring

The provider layer (`llm.py`) already supports **DeepSeek / Gemini / OpenAI / local**, selected by
env (`LLM_PROVIDER`, per-stage `LLM_SCORE_PROVIDER`/`LLM_SCORE_MODEL`), with per-model cost
estimation. The fleet builds three capabilities on top:

- **Heterogeneous workers + recording** (default): each worker scores with its own
  `LLM_SCORE_PROVIDER` + key, summing separate provider rate limits to clear the backlog faster.
  Each result records `provider` + `model`; a home-side report shows the provider mix and
  per-provider score distribution.
- **Cross-provider failover**: when `score_job` returns an error result, the adapter retries the
  job on the next provider in `LLM_SCORE_FALLBACK` (comma-list) before marking it `failed`.
- **Ensemble / A-B compare (opt-in, sampled)**: a per-batch flag scores a job on N providers and
  the result carries each provider's score plus an `agreement`/`spread` field; the aggregate is
  computed at pull time. OFF by default (doubles cost); intended for a sample or top-N, under the
  same cost cap. The ensemble per-provider scores and `agreement`/`spread` are preserved in the PG
  `compute_queue.result` JSONB (inspectable there); promoting them to a brain-side column or report
  is a follow-up (tracked in `.superpowers/sdd/compute-lane-followups.md`).

## 6. Cost governance

`fleet_config.cost_cap_daily_usd` / `cost_cap_total_usd` + `_cost_cap_exceeded` already gate
`lease_compute`. The adapter reports real `cost_usd` per job → `llm_usage` ledger → cap enforced.
Ensemble multiplies cost by the provider count, so it is sampled and cost-capped. Set a
conservative cap for the first run; the watchdog (separate spec) can flip `paused` on a breach.

## 7. Error handling & recovery

LLM failure → `failed` result (not advisory). Crashed worker → `reclaim_compute` requeues the lease
after its TTL. A job that repeatedly crashes workers → poison-quarantine pulls it. All three paths
exist and are tested in the foundation; this spec only adds the adapter's error→`failed` mapping
and the failover chain.

## 8. Testing

- **Adapter unit tests** (fake `score_job`/`audit_job`): payload→job mapping; result shape
  (`research_fit_score`/`research_decision`); cost capture; error→`failed` mapping; failover
  switches provider on an error result; ensemble produces the per-provider set + agreement.
- **End-to-end** (disposable Postgres + temp SQLite brain, **stubbed LLM client** — no spend):
  push jobs → `WorkerLoop.run_once` with the real adapter → assert `compute_queue` `done`,
  `llm_usage` row, and `pull_compute_results` writes `research_*` (and never `fit_score`/`audit_score`).
- **Context/versioning**: asset fetch + cache + re-fetch on version bump.
- **Opt-in live smoke test** (gated on a real key env var): score one real job on each configured
  provider to confirm the live path + cost estimation.

## 9. Owner-run (not code)
Rotate + set the LLM key(s); choose/spin cloud VMs; run the home `push`/`pull`; set the cost cap;
choose the provider mix per worker.

## 10. Decided questions
- Deployment: owner machines + cloud VMs, owner key(s); v1 direct Postgres. **Decided.**
- Tasks: score + audit; enrich deferred to the residential lane. **Decided.**
- KG: versioned shared-context asset, consumed not built. **Decided.**
- Providers: DeepSeek/Gemini/OpenAI (no new provider now); heterogeneous + failover + opt-in
  ensemble. **Decided.**
