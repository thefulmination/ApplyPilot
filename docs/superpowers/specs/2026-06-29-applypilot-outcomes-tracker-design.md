# ApplyPilot Outcomes Tracker — Design

- **Date:** 2026-06-29
- **Status:** Draft (approved in brainstorm; pending written-spec review)
- **Owner:** Jonathan
- **Topic:** Email-driven, full-timeline application-outcome tracker with lane insights

---

## 1. Motivation

ApplyPilot can apply to jobs, but the feedback loop is thin. The existing
`applypilot scan-gmail` (`gmail_outcomes.py`) reads the inbox with a **keyword
heuristic** classifier and can tag an outcome (offer / interview / rejected /
acknowledged) and fuzzy-match it to a job. It cannot:

1. **Read** an email — so it cannot extract a free-text **reason** ("role was put
   on hold", "went with someone with more buy-side experience") and only weakly
   guesses the **job title**.
2. Present a **browsable, per-application record** — outcome, reason, title, the
   email's timestamp, and the **applied → decision latency**.
3. Show the **journey** — first reply, screen, assessment, interview, decision —
   each with its own timestamp.
4. Tell you **which kinds of jobs actually respond** so you can lean into the
   lanes that convert.

This design adds a richer, email-driven outcome tracker that does all four,
built **on top of** ApplyPilot's existing data — not a separate tool.

## 2. Verified data facts (ground truth, queried 2026-06-29)

- The **home SQLite brain** (`%LOCALAPPDATA%\ApplyPilot\applypilot.db`, ~1.0 GB)
  is the canonical store. The fleet Postgres (`applypilot_fleet`) is only a thin
  apply/coordination queue — **no `jobs` table, no descriptions** there. All new
  state in this design is written to the **brain**.
- The `jobs` table has 82 columns. For the **299 jobs with `apply_status='applied'`**:
  - **`description` is populated on 299/299** (avg ~1,070 chars, max ~13K); a
    separate `full_description` column holds the complete scrape.
  - Rich pre-computed attributes exist and are usable for segmentation:
    `fit_score` (299/299), `audit_score` (299/299), `audit_label`,
    `fit_gap_category`, `recommended_action`, `fit_verdict`, `source_board`,
    `location` (299/299), `salary` (205/299), `title` (299/299),
    `company` (286/299), `applied_at` (299/299).
  - High-cardinality fields (`company` 253 distinct, `site` 255 distinct of 299)
    are **too granular** to be "lanes" on their own.
- ApplyPilot already has an `applications` table (235 rows) and an
  `application_events` timeline (246 rows) for **workflow status** — left
  unchanged by this design.
- **Denominator discrepancy (data-quality risk):** `jobs.apply_status='applied'`
  = 299, `applications` = 235, but the fleet Postgres `applied_set` had 651.
  Some applies (esp. fleet/LinkedIn) have not synced back to `jobs.apply_status`.
  Analytics denominators must be reconciled before they can be trusted
  (see §9).

## 3. Goals / Non-goals

**Goals (v1):**
- Read each application-related email and capture **outcome, reason, title,
  company, and timestamp**.
- Maintain a **full timeline** per application (every recruiter email as a dated
  step), and compute **time-to-first-response, time-to-stage, and
  applied→decision latency**.
- A **local web dashboard** to browse applications, read each email inline, and
  see the timeline + core analytics + a follow-up worklist; with a **CSV export**.
- A **descriptive "Lane Insights"** panel: response / positive-outcome rates by
  coarse segment vs your baseline, with sample-size and confidence guards.

**Non-goals (deferred):**
- **Prescriptive discovery feedback** (auto-suggesting new `searches.yaml` lanes
  back into the discovery pipeline) → **v1.1**, once outcome volume can support
  honest suggestions.
- Scheduled/auto-scan loop, push notifications, multi-account, rich charts.
- Any **black-box/learned recommender**. Lane insights are transparent statistics
  only (§7). Rationale: simple baselines are hard to beat, and the value here is
  trust and interpretability, not a score.

## 4. Architecture overview

```
Gmail (read-only OAuth)
   │  candidate emails (existing heuristic gate / query)
   ▼
outcome_extract  ── LLM reads each candidate ──► {stage, outcome, reason, title, company, confidence}
   │                         (heuristic fallback on failure)
   ▼
match to applied job (reuse match_email_to_job, strengthened by extracted title/company)
   │
   ▼
email_events  (NEW table in the brain — idempotent by message_id)
   │
   ├─ (opt-in, dry-run default) promote terminal outcomes → applications / application_events
   │
   ▼
outcome_timeline (pure)  +  lane_insights (pure)
   │
   ▼
outcome_dashboard (local read-only web server)  →  table · timeline · read-email · analytics · worklist · Lane Insights · /export.csv
```

The system is **additive and read-mostly** on existing tables: one new table
(`email_events`), everything else reuses or reads what's already there.

## 5. Data model — `email_events`

The evidence layer: one row per recruiter email. Idempotent, re-derivable from
Gmail, never lossy. This is the source for the timeline, the reasons, and the
"read each message" view.

```sql
CREATE TABLE IF NOT EXISTS email_events (
    message_id     TEXT PRIMARY KEY,   -- Gmail message id; dedup / idempotency key
    thread_id      TEXT,               -- Gmail thread id (groups a back-and-forth)
    job_url        TEXT,               -- FK -> jobs.url; NULL = unmatched bucket
    occurred_at    TEXT NOT NULL,      -- email Date header, ISO-8601 (the "time")
    sender         TEXT,
    sender_domain  TEXT,
    subject        TEXT,
    stage          TEXT NOT NULL,      -- acknowledged|screen|assessment|interview|offer|rejected|other
    outcome        TEXT,               -- offer|rejected|NULL (terminal only)
    reason         TEXT,               -- LLM-extracted free-text reason (NULL if none/unavailable)
    title          TEXT,               -- LLM-extracted job title
    company        TEXT,               -- LLM-extracted company
    match_method   TEXT,               -- board_slug|linkedin_job_id|company_domain|company_name|title|NULL
    match_score    REAL,
    confidence     TEXT,               -- high|medium|low (extraction confidence)
    body_text      TEXT,               -- cleaned plain-text body, capped ~20KB (offline "read")
    snippet        TEXT,               -- first ~300 chars for the list view
    extracted_by   TEXT,               -- model id/version, or 'heuristic_fallback' (provenance)
    scanned_at     TEXT NOT NULL,      -- when this row was processed
    FOREIGN KEY (job_url) REFERENCES jobs(url)
);
CREATE INDEX IF NOT EXISTS idx_email_events_job      ON email_events(job_url);
CREATE INDEX IF NOT EXISTS idx_email_events_occurred ON email_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_email_events_stage    ON email_events(stage);
```

Schema is added through ApplyPilot's **Python migration path** (Python owns the
brain schema, per the unified-brain rule) — not ad-hoc DDL.

The existing `applications` / `application_events` tables are **unchanged**.
A confident terminal `email_event` (offer/rejected) may be **promoted** into them
(opt-in, dry-run by default — identical posture to `scan-gmail --apply` today),
so the rest of ApplyPilot keeps working, but the dashboard is driven by
`email_events` + the job's existing `applied_at`.

## 6. Components

Each is small, single-purpose, and testable in isolation.

1. **`outcome_extract.py`** *(new; pure given a model client)*
   - Input: `(subject, body, sender)`. Output: validated JSON
     `{stage, outcome, reason, title, company, confidence}`.
   - One LLM call with a strict schema + few-shot prompt. **Pluggable backend**:
     Claude CLI / Codex CLI / DeepSeek API / (future) local model.
   - **Heuristic fallback**: if the model is unavailable or returns invalid JSON,
     fall back to the existing keyword classifier for `stage`/`outcome`, set
     `reason=NULL`, stamp `extracted_by='heuristic_fallback'`. Never hard-fails.
   - Output validation clamps `stage`/`outcome` to allowed enums and drops
     hallucinated title/company that don't improve the match.

2. **`outcome_scan`** *(extends `gmail_outcomes.py`)*
   - Reuses `build_gmail_service` (read-only OAuth), `_search_query`, threading,
     and `match_email_to_job`.
   - Per candidate email: heuristic **gate** → `outcome_extract` → match
     (strengthened by extracted title/company) → **upsert** `email_events`.
   - Idempotent: skip already-extracted `message_id` unless `--reextract`.
   - Backfill covers from the earliest `applied_at`; then incremental by `--days`.

3. **`outcome_timeline.py`** *(new; pure)*
   - Input: a job's `applied_at` + its `email_events`. Output: ordered timeline
     and derived metrics — **time-to-first-response, time-to-each-stage,
     applied→decision latency, current state (silent N days / decided)**.

4. **`lane_insights.py`** *(new; pure)* — see §7.

5. **`outcome_dashboard`** *(new; small local Python web server, same spirit as
   the `:8787` fleet console)*
   - Opens the brain **read-only**.
   - **Table**: one row per application — company, title, applied date, current
     stage, outcome, days-to-decision or days-silent; sortable / filterable.
   - **Row expand**: full timeline + read each email inline + the reason.
   - **Analytics panel**: response rate, median time-to-first-response,
     time-to-reject vs time-to-offer, breakdowns.
   - **Worklist**: silent ≥ N days (default 14) with no terminal outcome.
   - **Lane Insights panel** (§7).
   - **`/export.csv`**: flat, one row per application.

6. **CLI wiring** — new subcommands:
   - `applypilot outcomes scan [--days N] [--reextract] [--apply] [--model ...]`
   - `applypilot outcomes dashboard [--port N]`

## 7. Lane Insights (descriptive, guarded)

Join each application's **outcome** (from `email_events`) with the brain's
**coarse attributes**, and rank segments by conversion vs the user's baseline.

**Segment dimensions (coarse only):**
- `source_board`
- role-family (derived from `title` via a small normalizer/mapping)
- seniority (derived from `title`)
- `fit_score` band (e.g. `<5`, `5–6`, `7`, `8+`)
- `audit_label` / `fit_gap_category`
- location bucket (remote / metro / region)
- salary band (where present)

Raw `company` / `site` are **excluded** as standalone lanes (too granular).

**Per-segment metrics:** `n_applied`, `n_responded` (any non-acknowledged event),
`response_rate`, `n_positive` (interview + offer), `positive_rate`,
`median_days_to_first_response`, `median_days_to_decision` — each vs the overall
baseline.

**Honesty guards (required):**
- **Sample-size floor**: a segment is eligible to be flagged only at `n ≥ 8`
  (configurable). Below that it is shown but marked "not enough data."
- **Confidence intervals**: Wilson 95% interval on each rate.
- **Warm lane**: lower CI bound > baseline rate **and** `n ≥ floor`.
- **Cold lane**: upper CI bound < baseline rate **and** `n ≥ floor`.
- Framing is suggestive ("worth a closer look") with all underlying numbers
  visible — never a prescriptive command, never a hidden score.

## 8. Error handling & robustness

- **Per-message isolation**: one malformed email never crashes a scan (same
  try/except pattern `scan_inbox` already uses).
- **LLM fallback**: degrades to the heuristic; pipeline never breaks (§6.1).
- **Strict output validation**: enum clamping; drop unhelpful hallucinations.
- **Idempotent + resumable**: `message_id` PK; re-runs skip extracted rows unless
  `--reextract`; a large backfill can be chunked and re-run safely.
- **Nothing silently lost**: unmatched emails stored with `job_url=NULL`,
  surfaced in a dashboard "unmatched" bucket.
- **Promotion stays opt-in**: writing a terminal outcome into `applications` is
  dry-run by default (`--apply` to commit).

## 9. Brain-DB safety & data quality

- **Single writer**: `outcome_scan` is the only writer of `email_events`. It
  opens the live AppData brain with WAL + a `busy_timeout` and short
  transactions — never the OneDrive copy. (AppData reads fall through the app's
  overlay; writes/migrations must run in the user's real environment.)
- **Read-only dashboard**: opens the brain read-only so it cannot lock or corrupt
  live data.
- **Applied-denominator reconciliation** (v1 data-quality task): define the
  analytics universe as `jobs.apply_status='applied'` ∪ `applications.job_url`,
  and pull the fleet Postgres `applied_set` back into the brain so the 299 / 235
  / 651 gap is closed (or at minimum, the dashboard states its denominator
  explicitly and flags un-synced applies).

## 10. Privacy

- The heuristic **gate is the privacy lever**: only genuine job emails are sent
  to a model — promotional/financial/newsletter mail never leaves the box.
- `extracted_by` records which model saw each email (provenance/audit).
- **Model default**: the user's own **Claude or Codex CLI** (no marginal cost,
  already wired), DeepSeek as a cheap fallback. A **fully-local** model is a
  drop-in backend and remains an open, revisitable lever (§12).
- Gmail scope stays **read-only** (`gmail.readonly`), unchanged.

## 11. Testing

- **Pure functions** (`outcome_timeline`, `lane_insights`, JSON validation,
  matching) → unit tests; reuse the existing 70 gmail + adversarial cases.
- **Extractor** → recorded email fixtures with the model call mocked; plus a
  small **labeled golden set** measuring stage accuracy + reason-presence (the
  new accuracy risk gets its own eval, in the style of the project's other gates).
- **Idempotency** → scan twice, assert no dupes / no re-extract.
- **Lane insights** → fixtures asserting sample-size floor, Wilson bounds, and
  warm/cold flagging (incl. thin-cell cases that must NOT be flagged).
- **Dashboard** → endpoint smoke tests + a CSV-schema test; all metrics asserted
  through the pure assemblers, not the web layer.

## 12. Open decisions

- **Reading model** — default Claude/Codex CLI; revisit fully-local for maximum
  privacy. (Flagged; not blocking v1.)
- **Role-family / seniority normalizer** — start with a small static keyword
  mapping over `title`; upgrade to an LLM tag pass only if the static map proves
  too coarse.

## 13. Suggested build phases (for the implementation plan)

1. **Data + pipeline**: `email_events` migration, `outcome_extract` (+ fallback),
   `outcome_scan`, idempotency, backfill, golden-set eval.
2. **Read models**: `outcome_timeline`, `lane_insights` (pure, tested).
3. **Presentation**: `outcome_dashboard` (table, timeline, read-email, analytics,
   worklist, Lane Insights, CSV) + CLI wiring + denominator reconciliation.

> v1.1 (separate spec): prescriptive discovery feedback — turn warm lanes into
> `searches.yaml` suggestions.
