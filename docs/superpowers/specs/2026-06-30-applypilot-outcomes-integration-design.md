# ApplyPilot Outcomes Integration — Design

- **Date:** 2026-06-30
- **Status:** Draft (approved in brainstorm; pending written-spec review)
- **Owner:** Jonathan
- **Builds on:** the Outcomes Tracker (`email_events` table + `outcome_scan` / `outcome_timeline` / `lane_insights` / `outcome_dashboard`), spec `2026-06-29-applypilot-outcomes-tracker-design.md`.

---

## 1. Goal

Make the `email_events` outcome data **referenceable from five places**, so the
reasons / titles / timestamps / timelines we extract from email can be consumed
elsewhere. Every integration is **additive and read-only** over `email_events`,
with one exception (#4) that writes a *downstream copy* to Postgres.

**Two of the five (#3 learning-loop, #5 scoring signal) are built INERT:** they
compute and expose their data but **write nothing into `applications`, `jobs`,
the scores, or the apply gate**. They are parked for later activation.

## 2. Hard safety invariant

This feature leaves **byte-unchanged**: the apply gate, the `applications` /
`application_events` tables, `jobs.apply_status`, and every job score
(`fit_score`, `audit_score`, `COALESCE(audit_score, fit_score)`).

The only persisted writes this feature makes are:
- `email_events` (already exists; written by the existing `outcome_scan`).
- #4's **new** Postgres table `inbox_outcomes` (a downstream copy; never written back into the home brain's tracker/scores).

Everything else is computed on demand (pure functions, reports, exports) or read.

## 3. Source of truth

`email_events` (in the SQLite brain, `%LOCALAPPDATA%\ApplyPilot\applypilot.db`)
remains the single source. The per-application *timeline view* used throughout is
`outcome_dashboard.build_application_rows` (which composes `outcome_timeline` +
`lane_insights.derive_segments`). All five integrations read from these.

## 4. Modules

### #1 — External export (read)
- New command `applypilot outcomes-export [--out DIR]`.
- Writes a timestamped folder under the existing `application_exports/`:
  - `email_events.jsonl` — raw evidence rows (one per email).
  - `outcome_timelines.jsonl` — the assembled per-application view (the dashboard
    rows: company, title, applied_at, current_stage, outcome, first/decision/silent
    days, segments, events) **plus the #3 `implied_status` field** (§#3).
- New module `outcome_export.py`: pure assembly of the JSONL payloads from a
  read-only connection; reuses `build_application_rows`. Writes only to the export
  folder, never the DB.

### #2 — Research-tree read helper (read)
- In `brainDb.ts` (repo: `New project 9`), add read-only helpers:
  - `readEmailEvents(): EmailEventRow[]` — raw rows.
  - `readOutcomeTimelines(): OutcomeTimelineRow[]` — the per-application view,
    mirroring the Python `build_application_rows` shape so TS analysis/scoring can
    reference outcomes consistently.
- Read-only via `node:sqlite` against the same brain. No DDL, no writes (Python
  owns the schema, per the unified-brain rule).

### #3 — Learning-loop feed (INERT — overrides nothing)
- New **pure** module `outcome_implied.py`: `implied_status(timeline) -> dict | None`.
  - Maps confident terminal/advancing outcomes only: `offer → "offer"`,
    `interview → "recruiter_screen"`, `rejected → "rejected"`. `acknowledged` and
    `other` map to **None** (display-only; never implied).
  - Carries the precedence/recency metadata so a *consumer* can apply no-downgrade
    rules: returns `{implied_status, source_message_id, occurred_at, confidence}`
    plus the inputs needed to compare against an application's `last_status_at`.
  - PURE: no DB, no I/O. It only *describes* what a promotion would be.
- Surfaced two ways, **neither of which mutates the tracker**:
  - In the export (`outcome_timelines.jsonl` gains an `implied_status` field).
  - `applypilot outcomes-promote` — a **preview-only** command (always dry-run)
    that prints, per application, the current tracker status vs the implied status
    and whether a promotion *would* advance / be skipped (recency + no-downgrade).
    **It has NO `--apply` / no write path to `applications`/`application_events`/`jobs`.**
- **Parked for later:** when desired, a future one-flag change can route the
  preview's decisions through the existing `record_application`. That activation is
  explicitly OUT of scope here.

### #4 — Fleet Postgres sync (downstream copy)
- New thin PG table `inbox_outcomes` (added via the fleet schema, the fleet's
  own migration path):
  `job_url (PK), company, title, current_stage, outcome, reason, occurred_at,
   first_response_days, decision_days, synced_at`. **No raw email bodies / snippets**
  (privacy + size).
- **Home → PG push** (home stays authoritative), added to the existing fleet sync
  module: read the per-application view from the brain, upsert the thin summary into
  `inbox_outcomes`. Idempotent on `job_url`.
- Surfaced **read-only** in the fleet console (a new panel/section). No worker logic
  depends on it; it is visibility only.

### #5 — Advisory scoring signal (INERT — never in the gate)
- New **pure** module `outcome_lane_signal.py`: given a candidate job's segments
  and the corpus lane stats (from `lane_insights.compute_lane_insights`), return an
  advisory annotation per job: `{lane_dimension, lane_value, response_rate, ci_low,
  ci_high, flag}` (warm / cold / none / insufficient). PURE; reuses `lane_insights`.
- Surfaced read-only via:
  - `applypilot outcomes-lanes` — a Rich report of warm/cold lanes vs baseline.
  - The dashboard (already shows lane insights) + an `outcome_signal` field in the
    export.
- **Never** folded into `audit_score`/`fit_score`/`COALESCE(...)`/the apply gate.
  Thin-data-honest (Wilson + sample floor → "insufficient" until enough outcomes).

## 5. Components / files

**Python tool (`New project/ApplyPilot`):**
- Create: `src/applypilot/outcome_export.py` (#1, #3 field, #5 field).
- Create: `src/applypilot/outcome_implied.py` (#3, pure).
- Create: `src/applypilot/outcome_lane_signal.py` (#5, pure).
- Modify: `src/applypilot/cli.py` — add `outcomes-export`, `outcomes-promote`
  (preview-only), `outcomes-lanes`.
- Modify: fleet schema + `src/applypilot/fleet/sync.py` — `inbox_outcomes` table +
  `push_inbox_outcomes`; `src/applypilot/fleet/console_app.py` — read-only panel.
- Tests: `tests/test_outcome_export.py`, `tests/test_outcome_implied.py`,
  `tests/test_outcome_lane_signal.py`, `tests/test_inbox_outcomes_sync.py`,
  CLI tests.

**Research tree (`New project 9`):**
- Modify: `src/applypilot/brainDb.ts` — `readEmailEvents`, `readOutcomeTimelines`.
- Test: a brainDb test seeding `email_events` and asserting the read shape.

## 6. Data flow

```
email_events (brain, read-only everywhere below)
   ├─ outcome_export → email_events.jsonl + outcome_timelines.jsonl (+implied_status +outcome_signal)
   ├─ brainDb.ts readEmailEvents / readOutcomeTimelines        → TS analysis
   ├─ outcome_implied.implied_status (pure)  → export field + outcomes-promote PREVIEW (no writes)
   ├─ outcome_lane_signal (pure)             → outcomes-lanes report + dashboard + export field
   └─ fleet sync push_inbox_outcomes         → PG inbox_outcomes (copy) → fleet console (read-only)
```

## 7. Error handling

- Exports: per-row try/except; a bad row is logged and skipped, never aborts the file.
- `outcomes-promote` preview and `outcomes-lanes`: read-only; on empty data they
  print "nothing to show" rather than erroring.
- Fleet push: best-effort, idempotent; a PG failure logs and is retried next sync,
  never corrupts the brain (push only reads the brain).
- All new SQLite reads use a read-only / shared connection; no new writers to the brain.

## 8. Testing

- **Pure functions** (`outcome_implied`, `outcome_lane_signal`): unit tests incl. the
  precedence/recency cases (old email doesn't imply a downgrade; ack/other → None;
  offer wins) and thin-data guards.
- **Export**: assert the two JSONL files exist with the expected keys (incl.
  `implied_status`, `outcome_signal`); round-trip parse.
- **Fleet sync**: `push_inbox_outcomes` against a temp PG (or a fake cursor) — thin
  columns only, idempotent on `job_url`, no bodies.
- **brainDb.ts**: seed `email_events`, assert `readOutcomeTimelines` shape matches
  the Python view.
- **Safety test**: a test asserting `outcome_implied` and `outcome_lane_signal`
  import no tracker/score mutators and expose no write path (they are pure); and
  that `outcomes-promote` has no `--apply`/write branch.

## 9. Out of scope (parked)

- **Actual promotion** of implied statuses into `applications` (a future flag over
  `record_application`).
- **Folding** the lane signal into scoring / the apply gate (explicitly excluded by
  the safety invariant).
- Reconciling the applied-count denominator (299 brain vs 295 tracker vs fleet) —
  tracked separately.

## 10. Suggested build phases (for the plan)

1. **Reads/exports**: `outcome_export.py` + `outcomes-export`; `outcome_implied.py`
   (+ export field + `outcomes-promote` preview); `outcome_lane_signal.py`
   (+ `outcomes-lanes` + export field). (Python, additive, no DB writes.)
2. **Research tree**: `brainDb.ts` read helpers + test. (TS repo.)
3. **Fleet**: `inbox_outcomes` table + `push_inbox_outcomes` + console panel.
