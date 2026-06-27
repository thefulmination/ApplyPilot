# Fleet Frontier Quality Lane — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the shipped **compute lane** (`src/applypilot/fleet/compute_adapters.py`,
`scoring/scorer.py`, 123 PG tests) — reuses its scorer + the multi-provider/failover idea.
Grounding for Flavor B: memory `applypilot-subscription-scoring` (the ToS verdicts).
See [`2026-06-27-fleet-compute-lane-design.md`](2026-06-27-fleet-compute-lane-design.md).

## 1. Goal & success criteria

Put **frontier-model judgment (GPT‑5.5 / Opus class) only where it changes the decision** — the
small *contested* subset of jobs — without a metered frontier bill on the whole corpus and without
making the user's personal subscriptions load‑bearing.

A **hybrid**: the cheap metered API (DeepSeek, the shipped compute lane) scores the full corpus; a
**frontier second pass** re‑scores only the contested subset and **flags where the frontier judge
disagrees with the cheap one** for the owner's eye. The frontier backend is a config choice on one
interface:
- **Flavor A (default, ToS‑clean):** a metered frontier API model (e.g. `gpt-5.5` / `claude-opus`
  via API key). Runs anywhere; zero subscription exposure.
- **Flavor B (default‑OFF toggle):** the frontier pass uses the user's **own Codex/ChatGPT
  subscription** via `codex exec`, on the user's logged‑in home box, paid from sunk quota — with
  automatic failover to Flavor A.

**Architecture decision (why home‑side, not the fleet queue):** the contested subset is *small* and
Flavor B is *serial + home‑box only* (one login). The distributed compute_queue is keyed by `url`
(a job already bulk‑scored can't be cleanly re‑queued), and parallel fan‑out is pointless for a
small set and outright harmful for a subscription login. So the frontier lane is a **standalone
home‑side pass** — select → score → record — that reuses the scorer but needs **no new queue, no
worker/lease changes**.

**Done when:** `applypilot-fleet-frontier` selects the contested subset from the brain, scores each
with the chosen frontier backend (A or B, B failing over to A), records the frontier score +
agreement‑vs‑cheap in a self‑contained `frontier_scores` brain side‑table (advisory — the 60‑col
`jobs` table is NOT migrated), and prints a **disagreement report** (jobs where the frontier and
cheap judges diverge). Verified by tests with stubbed backends (no spend); one opt‑in live smoke
each for A (metered key) and B (`codex exec`).

**Non‑goals:** replacing the cheap API for bulk (it isn't — DeepSeek does the whole 77k for ~$10–30);
Claude‑subscription (a later optional toggle, OFF) and Gemini (login path removed 2026‑06‑18,
excluded); pooling anyone else's subscription (a hard ToS line); any change to the shipped compute
lane or its queue/worker.

## 2. Flow

```
Brain (SQLite jobs)  ── cheap research_fit_score already written by the compute lane (BULK) ──┐
        │                                                                                      │
        ▼  select_contested(band)                                                              │
  contested subset ── frontier_pass ──► frontier backend per job (serial, gap-jitter):         │
        │                                 Flavor A: scorer.score_job(provider=gpt-5.5|opus)     │
        │                                 Flavor B: cli_providers.score_via_codex(codex exec)   │
        │                                 (B -> A failover on SubscriptionUnavailable)          │
        ▼                                                                                       │
  frontier_scores side-table (url, cheap_score, frontier_score, provider, agreement, scored_at) ◄┘
        │
        ▼  disagreement_report(max_agreement)   ──►  owner-review queue (low-agreement jobs)
```

Advisory throughout. The frontier score lands in `frontier_scores`, never in `jobs.fit_score` /
`jobs.audit_score` — a flaky frontier call can't corrupt the brain.

## 3. Components

### 3.1 `fleet/frontier_select.py` — contested-subset selector
`select_contested(sqlite_conn, *, band=200, mode="band", lo=7.0, hi=8.5, hours=24, urls=None)
-> list[dict]` (each `{url, company, title, full_description, cheap_score}`). Modes:
- **`band` (default):** jobs whose **cheap score** (`COALESCE(research_fit_score, fit_score)`) is in
  `[lo, hi]` (default **[7.0, 8.5]** — the "maybe" zone a better judge resolves), `duplicate_of_url
  IS NULL`, **not already frontier‑scored** (no row in `frontier_scores`), ordered by cheap_score
  desc, `LIMIT band`.
- **`new`:** jobs discovered within the last `hours` (the daily trickle), same exclusions.
- **`urls`:** an explicit url list (e.g. a pairwise‑adjudication candidate set).
`lo`/`hi`/`band` are arguments, not hard‑coded.

### 3.2 `fleet/cli_providers.py` — the subscription‑CLI backend (Flavor B)
`score_via_codex(prompt, *, schema_path, timeout_s=120, retries=2) -> dict`: runs
`codex exec --output-schema <schema_path> -o <tmp.json> "<prompt>"`, reads + JSON‑parses `<tmp.json>`,
returns `{"score": int, "reasoning": str, ...}`. On a malformed object → bounded retry with a
"return ONLY the JSON object" reinforcement; on non‑zero exit / auth / quota / parse‑exhaustion →
raise `SubscriptionUnavailable`. Uses `--output-schema`/`-o` (the single‑object form) — **never**
`--json` (a JSONL event stream). A `claude -p --output-format json --json-schema … → .structured_output`
variant is **stubbed** for a later optional toggle (Claude stays OFF per the ToS findings).
This module shells out only; it holds no token (the CLI uses the local login).

### 3.3 `fleet/frontier_pass.py` — the orchestrator (home‑side, reuses the scorer)
`run_frontier_pass(*, sqlite_conn, provider, mode, band, lo, hi, hours, urls, resume_text,
preference_profile, kg_prompt, subscription_enabled=False) -> dict`:
1. `select_contested(...)`.
2. For each job, build the score prompt (the same job dict `score_job` consumes) and score it with
   the backend: a **metered‑API provider** → `scorer.score_job(resume, job, preference, kg,
   provider=<frontier>)`; **`codex-subscription`** → `cli_providers.score_via_codex(...)` — but only
   if `subscription_enabled` (else raise; default off). On `SubscriptionUnavailable`, **fail over**
   to the configured Flavor‑A metered model so the opinion still arrives (and record which backend
   actually produced it).
3. Compute `agreement = round(1 - abs(frontier_score - cheap_score)/9.0, 3)` (1.0 identical, lower =
   more divergence).
4. Upsert a `frontier_scores` row. Serial with gap‑jitter between calls (reuse the apply‑lane jitter)
   — no parallelism on a subscription login.
Returns `{scored, failed_over, disagreements}`.

### 3.4 `frontier_scores` brain side‑table (no `jobs` migration)
```sql
CREATE TABLE IF NOT EXISTS frontier_scores (
  url            TEXT PRIMARY KEY,
  cheap_score    REAL,
  frontier_score REAL,
  frontier_decision TEXT,
  provider       TEXT,       -- the backend that actually produced it (post-failover)
  agreement      REAL,       -- 1.0 = identical; low = divergent
  reasoning      TEXT,
  scored_at      TEXT
);
```
Self‑contained + advisory; the pairwise review tool / a future jobs‑column promotion can read it
later. Created by `frontier_pass` on first run (idempotent).

### 3.5 Disagreement report
`disagreement_report(sqlite_conn, *, max_agreement=0.8) -> list[dict]` — `frontier_scores` rows with
`agreement < max_agreement`, ordered by agreement asc: `{url, company, title, cheap_score,
frontier_score, agreement, provider}`. This is the **owner‑review queue**: "the model you trust most
disagreed with the cheap one here." It never auto‑acts.

### 3.6 CLI: `applypilot-fleet-frontier`
`--mode band|new|urls`, `--band N`, `--lo`/`--hi`, `--provider <model|codex-subscription>`,
`--enable-subscription` (required to use `codex-subscription`; default off), `--report`
(print the disagreement queue). Loads the resume/preference/KG context locally (the same inputs the
compute lane serves as assets; here read from the brain/owner config since the pass is home‑side).

## 4. Safety / honesty constraints (from the subscription research)

- **Advisory only.** Frontier scores live in `frontier_scores`; `jobs.fit_score`/`audit_score` are
  never touched.
- **Own accounts only.** Flavor B uses the user's own ChatGPT login on the user's own box; no token
  is ever read or distributed. Pooling a friend's subscription is account‑sharing (ToS violation +
  ban vector) and is structurally impossible here.
- **Home box only.** Flavor B runs only where `codex` is logged in (the home box) — it's a local
  subprocess, never a cloud worker.
- **Default OFF + explicit opt‑in.** `codex-subscription` requires `--enable-subscription`; the
  default backend is a metered API model (Flavor A), which is ToS‑clean.
- **ToS reality, stated.** Flavor A is clean. Flavor B (programmatic `codex exec` on a personal
  plan) is a documented **gray zone** (OpenAI's automation clause) — bounded to a small subset,
  default‑off, owner‑run; the spec does not claim it is blessed.
- **Cost truth.** Bulk stays on the cheap API; this is a *quality* lane on a small subset, not a
  cost optimization.

## 5. Testing
- **Selector:** temp SQLite brain → `band` returns only in‑band, non‑dup, not‑yet‑frontier jobs,
  ordered; `new`/`urls` modes select correctly; already‑scored urls excluded.
- **CLI backend (stubbed subprocess):** `score_via_codex` parses a valid schema object; retries then
  raises `SubscriptionUnavailable` on malformed; raises on non‑zero exit. The `--output-schema`/`-o`
  argv is asserted (not `--json`).
- **Orchestrator:** with a stubbed `score_job`, a `band` pass writes `frontier_scores` rows with a
  correct `agreement`; with a stubbed `score_via_codex` that raises `SubscriptionUnavailable`, the
  pass **fails over** to the stubbed metered model and records `provider` = the metered one.
- **Guardrail:** `provider='codex-subscription'` without `--enable-subscription` raises before any
  subprocess; advisory‑only — the `jobs` table is never written.
- **Report:** seeded mixed agreement → only `agreement < max` returned, ordered.
- **Opt‑in live smokes:** A (one real metered frontier call, key‑gated); B (one real `codex exec`,
  gated on `codex login status` reporting ChatGPT).

## 6. Decided vs open
- Home‑side pass (not the fleet queue); cheap API for bulk + a frontier pass on the contested subset;
  Flavor A default, Flavor B default‑off explicit opt‑in; frontier data in a self‑contained
  `frontier_scores` side‑table. **Decided.**
- Backends: metered API (A) + Codex subscription (B). Claude subscription = later optional, OFF;
  Gemini = excluded. **Decided.**
- **Open for review:** (1) the default contested band `[7.0, 8.5]` on `COALESCE(research_fit_score,
  fit_score)` — confirm the field + window; (2) whether `disagreement_report` should also write a
  flag the pairwise review tool reads, or stay a standalone report (default: standalone). Defaults
  chosen; confirm at review.
