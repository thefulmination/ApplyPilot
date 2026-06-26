# Design Spec: Unify ApplyPilot Around the SQLite Brain (Single Source of Truth)

**Status:** Approved-in-principle (owner approved seam + cadence + pairwise + KG handling); pending spec review before implementation.
**Date:** 2026-06-25
**Scope:** Make the Python brain (`applypilot.db`) the one canonical record per job. The TypeScript research/scoring tree (`New project 9`) reads jobs *from* it and writes scores + labels *back* to it, so research improvements reach the live applier and the hand-exported catalog (provenance gap) is retired.


## Owner decisions baked in
- **Integration seam: Option A — TS reads/writes the brain DIRECTLY via Node's built-in `node:sqlite`.** Enabled by: Node v24 (built-in `node:sqlite`, **no native dep**, no new `package.json` entry → no conflict with the active Codex session), the authoritative DB being local (not OneDrive), and serial access (owner kills the other process) hardened by WAL + `busy_timeout`. (Option B export/import kept as a documented fallback; Option C local service deferred.)
- **Sync cadence:** one-command wrapper (the scorer reads the brain and writes `research_scores` back directly — no separate import step).
- **Pairwise labels:** include now (`research_pairwise_labels` table built this pass).
- **KG scoring:** unify per §5 (build from brain, version + store the KG, scores tagged with `kg_version`).
- **Supabase (audit §9.2):** stays the cross-device label COLLECTION point; the brain imports from it ONE-WAY. Brain authoritative for everything except label intake.
- **Resume (audit §9.3):** KG master resume and apply resume are intentionally DIFFERENT; spec documents the divergence, no forced sync.

## 0. Guiding principles (non-negotiable)
1. **`fit_score` / `audit_score` stay authoritative for applying.** Research scores are *advisory*; they only reach the apply gate when the owner explicitly promotes a job/batch. The fleet's default behavior never changes silently.
2. **Additive, never destructive.** New tables/columns only — matches the brain's `ALTER TABLE ADD`-only discipline (`ensure_columns()` in `database.py`).
3. **Serial access, hardened.** No *simultaneous* writers: the owner kills the other process before a cross-language run, AND TS opens the DB in WAL mode with a `busy_timeout` (~5s) so accidental overlap waits instead of erroring. TS targets the **authoritative LOCAL DB** via the launcher env, never the OneDrive backup copy — **and never the `~/.applypilot` stub (see §9.1 hard guard: require `APPLYPILOT_DB_PATH` + rowcount floor).**
4. **Python owns the schema.** Python `init_db()` creates/migrates ALL tables (incl. the new `research_*`). TS only **reads `jobs`** and **writes `research_*`** rows — it never issues DDL or touches live scoring columns.
5. **Don't disrupt the active Codex session.** TS additions are a new DB-access module + one branch in `loadMergedApplyPilotCatalog` (`catalogInputs.ts:81`). `node:sqlite` is built-in, so **no dependency/`package.json` change**. No restructuring.
6. **Every stage independently shippable and reversible**, gated by a flag/env var; the old JSON catalog flow keeps working until explicitly retired.
7. **Retain dead/stale jobs for training** — the research read source must NOT liveness-filter.

## 1. Target data model (additions to the brain)
All additions land in `New project/ApplyPilot/src/applypilot/database.py` via the `_ALL_COLUMNS` registry + a new `ensure_research_tables()`, auto-migrated on `init_db()`. **Python creates these; TS only INSERTs into them.**

### 1a. `research_scores` (one row per job×model×run) — this IS the KG-based scoring output
Keyed to `jobs.url`. Columns: `id` PK, `job_url` FK→jobs.url, `item_id`, `provider`, `model`, `research_fit_score` REAL, `research_decision`, `confidence`, `reason` (the one-sentence why), `positive_signals_json`, `gaps_json`, `evidence_node_ids_json`, `score_source` (tier1/adjudicated), `raw_fit_score` REAL, `kg_version` (FK→research_kg_artifacts), `scored_at`, `ingested_at`.
Indexes on `job_url`, `model`, `scored_at`. `UNIQUE(job_url, provider, model, scored_at)` for idempotent re-writes.

### 1b. Advisory columns on `jobs` (denormalized "current best research opinion")
`research_fit_score` REAL, `research_decision`, `research_model`, `research_scored_at`, `research_opt_in` INTEGER DEFAULT 0.
**Critical:** never folded into `COALESCE(audit_score, fit_score)` automatically — consumed only per §4.

### 1c. `research_labels` (absorb the JSONL label stores)
Preserves the append-only `ReviewLabelEvent` shape: `id` PK, `job_url` FK, `item_id`, `source_project_id`, `decision`, `rating`, `reason`/`cleaned_reason`, `tags_json`, `method`, `fit_map_feedback_json`, `review_queue_json`, `item_status_at_review`, `created_at`, and `raw_event_json` (lossless full-event escape hatch).

### 1d. `research_pairwise_labels` (INCLUDED NOW)
Absorbs `PairwiseComparisonEvent` (`applypilot-pairwise-annotation.jsonl`): `id` PK, `left_job_url` FK, `right_job_url` FK, `left_item_id`, `right_item_id`, `winner` (left/right/tie), `method`, `source_project_id`, `created_at`, `raw_event_json`. Indexes on both job-url columns.

### 1e. KG provenance + artifact (§5)
- `research_kg_runs`: `kg_version` PK, `built_at`, `resume_path`, `resume_sha`, `n_label_events`, `n_capabilities`, `compact_kg_path`, `source` (brain/json).
- `research_kg_artifacts`: `kg_version` PK, `compact_kg_json` BLOB, `built_at`, `input_label_count`, `inputs_sha`. Lets any `research_scores.kg_version` resolve to the exact graph that produced it.

### Rationale capture (already covered, both sides)
- **Research:** `research_scores.reason` + `positive_signals_json` + `gaps_json`.
- **Live (already in `jobs`):** `score_reasoning`, `fit_verdict`, `audit_reason`, `fit_diagnosis`/`fit_diagnosis_json`, `recommended_action`. The unification preserves both; no new rationale field needed.

## 2. Integration seam — Option A (direct `node:sqlite`)
TS opens the authoritative local `applypilot.db` in-process via Node's built-in `node:sqlite` (Node v24 — no native module, no `package.json` dep). A small `src/applypilot/brainDb.ts` module:
- Resolves the authoritative DB path (mirror Python: `APPLYPILOT_DB_PATH` else `APP_DIR/applypilot.db`; never the OneDrive copy).
- Opens with `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`.
- Exposes `readJobs(filter)` (SELECT from `jobs`) and `writeResearchScores(rows)` / `writeLabels(rows)` / `writePairwise(rows)` (INSERT/UPSERT into the Python-created `research_*` tables only).
- Schema-guard: on open, assert the `research_*` tables exist (i.e. Python `init_db()` has run); if not, fail fast with a clear "run `applypilot init-db` first" message — never create tables from TS.

**Concurrency posture:** owner kills the other process for a cross-language run; WAL + `busy_timeout` covers accidental overlap. `node:sqlite` emits an ExperimentalWarning in v24 (cosmetic; suppressable). If the warning is unwanted, `better-sqlite3` is the drop-in alternative at the cost of a native build — not recommended while the Codex session is active.

(Fallback **Option B**: Python `export-jobs` + `import-research-*` CLIs syncing via JSONL — zero shared-file access ever; revert to this only if direct access proves troublesome. **Option C**: a localhost DB-owner service — natural evolution once research + applying run continuously.)

## 3. Read path (TS reads jobs FROM the brain)
1. TS `brainDb.readJobs()` SELECTs jobs straight from the brain (no liveness filter — retain-for-training), mapping brain columns → `ApplyPilotJobMetadata` (title, company, location, salary, lane, experienceFitScore, importedScore, content, **jobUrl**).
2. **Single swap point:** `loadMergedApplyPilotCatalog(paths)` (`catalogInputs.ts:81`) gains a brain-backed branch gated by `APPLYPILOT_CATALOG_SOURCE=brain|json` (default `json`). When `brain`, it builds `ReviewCatalogState` from `brainDb.readJobs()` instead of the static JSON paths. Return type unchanged → scorer, KG build, review server untouched.
3. **ID mapping (load-bearing):** brain key = `jobs.url`; TS key = `item.id`. Carry `url` onto the catalog item (`jobUrl`) and use it as the join key both directions so writeback (§4) resolves `itemId → job_url`.

Outcome: the catalog is read live from the brain; the hand-merged `applypilot-fitmap-jobs-queue-latest.json` becomes legacy fallback only — provenance gap closed.

## 4. Write-back path (scores + labels → brain; advisory consumption)
- **Scores:** the scorer keeps writing `data/review/applypilot-{model}-kg-scores.jsonl` (calibration/eval artifact) AND `brainDb.writeResearchScores()` upserts into `research_scores` (§1a) + refreshes the advisory columns (§1b). Idempotent via the unique key.
- **Labels:** the review server keeps appending JSONL AND `brainDb.writeLabels()`/`writePairwise()` upsert into `research_labels`/`research_pairwise_labels` by event `id` (lossless `raw_event_json`). (Direct write keeps the brain current; JSONL stays as the human-facing log until Option C.)
- **Live consumption (opt-in, advisory):**
  - Default: fleet apply gate unchanged — `fleet_sync.py` PUSH selects on `COALESCE(audit_score, fit_score) >= ?`; `research_fit_score` invisible.
  - Promotion (owner-driven): emit `decisions.jsonl` from promoted research scores → existing `applypilot import-decisions` sets the gate **only for explicitly promoted rows**. (Future: flagged `OR (research_opt_in=1 AND research_fit_score >= ?)` clause — deferred.)

## 5. Knowledge-graph scoring (unified)
The KG is the evidence base the research scorer reasons over; `research_scores` is its output. The live Python applier scorer does NOT use the KG (preference-profile based) — KG scoring is research-side, feeding the applier advisorily via §4.
1. **KG built from the brain:** the KG build reads labels directly from `research_labels` (via `brainDb`) rather than scattered JSONL → reproducible from the single source.
2. **KG versioned + stored:** each build records `research_kg_runs` + stores the compact KG blob in `research_kg_artifacts` (§1e).
3. **Scores tagged with `kg_version`:** full lineage resume + brain-labels → KG vN → scores(vN) → advisory applier signal. KG-quality/regression instruments also run off brain-sourced inputs.

## 6. One-command sync wrapper (decided)
A single command (`npm run applypilot:sync-brain` or a flag on `model:score`) that, with `APPLYPILOT_CATALOG_SOURCE=brain`: reads jobs from the brain → runs `model:score` → writes `research_scores` (+ advisory cols) straight back via `brainDb`. No export/import steps. Manual trigger now; wrap in a scheduled task later. (A separate `import-labels` one-shot backfills the historical JSONL into the brain once.)

## 7. Rollout (staged, each shippable + reversible)
- **Stage 0 — Schema (Python only, additive).** Add §1 tables/columns + `ensure_research_tables()`. Invisible via `init_db()`; reversible. No TS/Codex impact.
- **Stage 1 — TS brain-DB layer + backfill.** Build `brainDb.ts` (`node:sqlite`, WAL, busy_timeout, schema-guard) with `readJobs` + `writeResearch*`. One-time backfill of existing `data/review/applypilot-*-kg-scores.jsonl` / `-label-events.jsonl` / pairwise into the `research_*` tables; verify counts. Reversible (truncate tables).
- **Stage 2 — Read adapter (TS, flag OFF).** Add the `APPLYPILOT_CATALOG_SOURCE` branch in `loadMergedApplyPilotCatalog` reading via `brainDb`. Default `json`. A/B the brain catalog vs the hand-merged one. Coordinate with Codex before touching `catalogInputs.ts`.
- **Stage 3 — Cut read path over.** Flip default to `brain`; wire write-back into `model:score`; retire the hand-merged latest. Provenance gap closed. Reversible via env var.
- **Stage 4 — Opt-in advisory gate.** Wire research→`import-decisions` promotion. Optionally add the flagged `research_opt_in` clause.
- **Stage 5 (later) — Option C service** if continuous research+apply justifies it.

### Two-repo / Codex coordination
- Stage 0 in `New project/ApplyPilot` (PyPI tool; `.conda-env` runtime authoritative, `.venv` stale; APP_DIR invocation gotcha). Must land + verify (tables created) before the TS layer reads/writes them.
- Stage 1–3 in `New project 9`: only edits are new `brainDb.ts` + one branch at the top of `loadMergedApplyPilotCatalog`, plus the write-back call in `applypilotModelScore.ts`. `node:sqlite` is built-in → no dependency change. Check handoff notes + `git status`/pull before editing; do not refactor; never `git add -A`.

## 8. Remaining risks / open questions
1. **Serial-access discipline.** WAL + `busy_timeout` make light overlap safe, but a long TS write while a Python writer holds the lock could still time out. Mitigation: keep TS writes batched in short transactions; kill the review server before a big sync. Acceptable given the low write rate.
2. **Authoritative DB path.** Confirm the exact resolved path the owner runs with (`APPLYPILOT_DB_PATH` vs `APP_DIR/applypilot.db`) and that `brainDb.ts` resolves the same one — never the OneDrive backup.
3. **`node:sqlite` maturity.** Experimental-but-usable in v24 (emits a warning). If undesirable, switch to `better-sqlite3` (native build). Decide if the warning is acceptable.
4. **Score scale.** Brain `fit_score` documented INTEGER 0–10; TS `research_fit_score` is REAL 0–10. Store raw REAL in `research_scores`; rescale only at the `import-decisions` promotion boundary.
5. **Preferred model for §1b denorm columns** when multiple models scored a job: deterministic rule (latest `scored_at`, or a configured preferred model).

## Key files referenced
- Brain schema/migrations: `New project/ApplyPilot/src/applypilot/database.py` (`_ALL_COLUMNS`, `ensure_*_tables`); DB path: `config.py` (`DB_PATH`, `APP_DIR`)
- Live rationale columns (already present): `jobs.score_reasoning` / `fit_verdict` / `audit_reason` / `fit_diagnosis` / `recommended_action`
- Promotion path: `import_decisions.py`; gate `COALESCE(audit_score, fit_score)` in `apply/fleet_sync.py`
- TS read seam: `New project 9/src/applypilot/catalogInputs.ts:81` (`loadMergedApplyPilotCatalog`); fallback `src/review/catalog.ts:52`
- TS score writer: `src/cli/applypilotModelScore.ts:159`; TS label writer: `src/review/labelStore.ts:66`
- NEW: `New project 9/src/applypilot/brainDb.ts` (`node:sqlite` access layer)

## 9. Audit reconciliations (2026-06-25 cross-project sweep)
A filesystem-wide audit of every ApplyPilot store (all `New project*` dirs + off-OneDrive) surfaced 5 gaps. Resolutions (owner-decided where noted):

### 9.1 Three DBs — hard guard against the stub (was framed as "two DBs")
There are THREE `applypilot.db`: the **945 MB canonical** at `%LOCALAPPDATA%\ApplyPilot\applypilot.db`; a **143 KB / 13-job STUB** at `~/.applypilot/applypilot.db` (config DEFAULT when `APPLYPILOT_DB_PATH` is unset — `config.py:10,19`); and the **OneDrive backup** at `New project/ApplyPilot/.applypilot/applypilot.db`. Canonicality is established ONLY by `run-applypilot.ps1:19` setting `APPLYPILOT_DB_PATH` to the 945 MB path.
**Resolution:** `brainDb.ts` MUST NOT use `config.py`'s bare default. It (a) requires `APPLYPILOT_DB_PATH` to be set (sourced from the launcher env), and (b) fails fast with a sanity check — refuse to open a `jobs` table below a floor (e.g. < 1,000 rows) so it can never silently bind to the 13-row stub or the backup. Never fall back to `~/.applypilot`. (Upgrades old open-question #2 from "confirm the path" to a hard guard.)

### 9.2 Supabase — collector, one-way import into the brain [OWNER DECISION]
New project 9 syncs labels bidirectionally with a Supabase cloud Postgres (`public.label_events`, `public.source_items`) via `src/sync/supabaseClient.ts` (`review:sync` / `applypilot:sync`). **Decision: Supabase stays the cross-device label COLLECTION point; the brain imports ONE-WAY.** `research_labels` (§1c) is a sink fed by BOTH the JSONL logs AND Supabase `label_events`, deduped by event `id`. The brain is authoritative for everything EXCEPT label intake and NEVER pushes labels back to Supabase. `labelMerge` keeps running Supabase-side for cross-device collection; the brain import is strictly downstream. Conflict precedence: latest `created_at` wins; `raw_event_json` preserved losslessly. This removes the "three label homes" ambiguity: Supabase = intake, brain = truth, JSONL = human-facing log.

### 9.3 Resume divergence — intentional [OWNER DECISION]
The KG is built from `MasterResume\StalloneJonathan7.docx` (fuller ~20k-char master; `applypilotKnowledgeGraph.ts:28`). The live applier + fleet use a different, newer `~/.applypilot/resume.pdf`. **Decision: keep them separate intentionally** — the master feeds the KG/scoring; the tailored pdf is what's actually sent. `research_kg_runs.resume_path/resume_sha` records the KG master. The spec explicitly documents that **research advisory scores are computed against the master resume, NOT the apply resume** — treat this as a known, accepted gap when promoting scores to the apply gate (§4). No forced sync.

### 9.4 inbox_events — named, not re-homed
Gmail application-outcome events live in `applypilot.db :: inbox_events` (keyed by `job_url` + `received_at`), written by the read-only Gmail OAuth scanner. They ALREADY ride in the brain (consistent with single-source). Named here so they are NOT re-homed. FUTURE (out of scope for v1): surface `inbox_events` + the fleet's `apply_status` into an outcome-calibration loop alongside research scores — the strongest ground-truth signal.

### 9.5 fleet_assets — third resume copy
Railway Postgres `fleet_assets` holds a BYTEA copy of `profile.json` + `resume.pdf` for the offsite fleet — a third resume copy. It must stay consistent with the canonical APPLY resume (`~/.applypilot/resume.pdf` per §9.3), NOT the KG master. apply_queue mechanics stay downstream of the gate and out of scope; `apply_status`/`applied_at` noted as a future calibration input (§9.4).

### 9.6 Path resolution — the launcher is authoritative
ALL `APPLYPILOT_*_PATH` overrides (`APPLYPILOT_DB_PATH`, `APPLYPILOT_DIR`, `APPLYPILOT_SEARCH_CONFIG_PATH=searches_tuned.yaml`) come from `run-applypilot.ps1`, not `config.py` defaults. Note: `searches_tuned.yaml` IS used under the launcher (supersedes the old "silently ignored" assumption). Any cross-language tooling (`brainDb.ts`, the sync wrapper) MUST source the launcher's environment, never reimplement `config.py` defaults.

### 9.7 Explicitly out of scope (no job/label/score data)
New project 2 (onetab/bookmarks), New project 6 (music-lab SQLite), Claude_KG (empty), optquant (options pricing), and New project 3/4/5/7/8/10 (unrelated projects). Confirmed to contain no ApplyPilot job/label/score records — not part of the unification.
