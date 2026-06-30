# Fleet Email-Verification Reconcile — Design

- **Date:** 2026-06-30
- **Status:** Draft (approved in brainstorm; pending written-spec review)
- **Owner:** Jonathan
- **Builds on:** the Outcomes Tracker (`email_events` table + `match_email_to_job`,
  spec `2026-06-29-applypilot-outcomes-tracker-design.md`) and the fleet apply lane
  (`apply_queue`, `applied_set`, `fleet/queue.py`).

---

## 1. Goal

Resolve the **`crash_unconfirmed` ("may-have-submitted") backlog** in the fleet
`apply_queue` using email ground truth. When the apply agent submits an
application but the launcher never captures a parseable `RESULT:` line, the job is
parked `crash_unconfirmed` (apply_error `failed:no_result_line`) and never
re-leased — deliberately, to avoid double-applying. Today **480** such rows sit
unresolved; their true outcome is unknown.

This feature matches those jobs against **application-outcome emails** (`email_events`):
an acknowledgement / interview / offer / rejection email **proves the application
was submitted**. On a confident match we flip the fleet job `crash_unconfirmed → applied`.

## 2. Why a naive join fails (motivating evidence)

Measured 2026-06-30 against the live data (read-only):

- `email_events`: 200 rows (178 `acknowledged`, 22 `rejected`), 131 distinct
  `job_url`, spanning 2026-06-24 → 06-30 03:20.
- `apply_queue` `crash_unconfirmed` / `failed:no_result_line`: 480 rows.
- **Exact join (by `job_url` or `dedup_key`): 0 matches.** Two artifacts cause this:
  1. `match_email_to_job` only ever links an email to a job in the **applied** set;
     a `crash_unconfirmed` job is never "applied", so no `email_events.job_url`
     points at one.
  2. `dedup_key = sha(normalized_company | normalized_title)` diverges between the
     email-extracted title and the queue's title.
- **But 27 companies overlap** between the two sets (alphasense, anthropic,
  justworks, plaid, scale ai, doordash, notion, …). The signal is real; it just
  needs the existing **fuzzy matcher**, not an exact join.

**Conclusion:** reuse `match_email_to_job` with the `crash_unconfirmed` jobs as the
candidate pool.

## 3. Hard safety invariants

1. **No re-application.** This feature never re-queues, re-leases, or re-applies a
   job. Resolving unknowns to `applied` only *removes* work; it never creates an
   apply attempt. (Re-attempting genuine failures is account-risk and is explicitly
   out of scope — see §10.)
2. **Home brain untouched.** Writes go **only** to the fleet Postgres
   `apply_queue` (status of confirmed jobs) plus a new fleet audit table
   (`email_reconcile_actions`). The home brain's `applications`, `jobs`,
   `email_events`, and all scores are **read-only** here.
3. **Strong-match-only auto-flip.** A job is auto-flipped to `applied` only on a
   high-precision match (§6). Coarse matches are reported as *probable* and require
   a human `--apply-probable` opt-in; they never flip silently.
4. **Dry-run by default.** The command reports what it *would* do; `--apply` is
   required to write. Idempotent and safe to re-run.
5. **No double-apply exposure.** Every target row is already in `applied_set`
   (inserted at crash time by `fleet/queue.py`), so flipping it to `applied`
   cannot widen the apply surface.

## 4. Architecture & data flow

Runs **home-side** (the only place where both the home brain and the fleet PG are
reachable; per the AppData-overlay constraint, brain access must run in the user's
real environment).

```
Gmail (read-only) --outcomes-scan--> email_events (home SQLite)   [Phase 0, optional-but-default]
                                          |
crash_unconfirmed jobs (fleet PG) --------+--> reconcile matcher (reuse match_email_to_job)
                                          |
                                          v
                          confident match (proves submitted)
                                          |
                                          v
            apply_queue.status: crash_unconfirmed -> applied   (fleet PG, --apply only)
            + email_reconcile_actions audit row (fleet PG)
```

## 5. Components / modules

New module **`src/applypilot/fleet/email_reconcile.py`** (pure, testable):

- `load_outcome_emails(home_conn) -> list[OutcomeEmail]`
  Reads `email_events` rows whose `stage` is submission-proving
  (`acknowledged|screen|assessment|interview|offer|rejected`; `other`/`not_job`
  excluded). Returns `(message_id, sender, subject, body_text, company, title,
  job_url, stage, occurred_at)`.
- `load_crash_jobs(pg_conn) -> list[dict]`
  Reads `apply_queue` rows `status='crash_unconfirmed' AND apply_error='failed:no_result_line'`,
  shaped as the matcher's `applied_jobs` candidate (`url, application_url,
  company, title, site=apply_domain, dedup_key`).
- `reconcile(emails, jobs) -> ReconcileResult`
  For each email, call the existing `match_email_to_job(sender, subject, body, jobs)`.
  Classify each hit by method/score into **confirmed** (strong) vs **probable**
  (coarse) per §6. Pure function; no I/O. De-dupes so one job is resolved once
  (best match wins).
- `apply_resolutions(pg_conn, result, *, include_probable=False) -> Counts`
  In one transaction per job: `UPDATE apply_queue SET status='applied',
  apply_status='applied', apply_error=NULL, applied_at=<email.occurred_at>,
  updated_at=now() WHERE url=%s AND status='crash_unconfirmed'` (guarded on current
  status to stay idempotent), and INSERT an `email_reconcile_actions` audit row
  (url, message_id, method, score, stage, prior_status, how_to_reverse).

New entrypoint **`src/applypilot/fleet/email_reconcile_main.py`**
(`applypilot-fleet-reconcile-email`):
- Flags: `--dsn`, `--home-db` (default `%LOCALAPPDATA%\ApplyPilot\applypilot.db`),
  `--scan-days N` (default 7; `--no-scan` to skip Phase 0), `--apply`,
  `--apply-probable`, `--min-score` (override the fuzzy threshold).
- Phase 0 (default on): invoke the existing `outcomes-scan` over `--scan-days` to
  refresh `email_events` (live `gmail.readonly`). Skippable.
- Phase 1: load → reconcile → print report → (if `--apply`) `apply_resolutions`.

New fleet table **`email_reconcile_actions`** (audit / reversibility), added to
`schema_v3.sql` with `IF NOT EXISTS`:
`id, url, message_id, match_method, match_score, stage, prior_status,
how_to_reverse, created_at`.

## 6. Matching & confidence policy

`match_email_to_job` returns `(job, method, score)`. Policy:

- **Confirmed (auto-flip on `--apply`):** `method ∈ {board_slug, linkedin_job_id,
  company_domain}` (these are exact/near-exact, score 1.0), **or** `method ∈
  {ats_domain, company_name, title}` with `score ≥ MIN_STRONG` (default **0.6**,
  override via `--min-score`).
- **Probable (report only; flip only with `--apply-probable`):** a fuzzy hit below
  `MIN_STRONG`.
- **No match:** stays `crash_unconfirmed` (still unknown).

Rationale: the costly error is flipping a job to `applied` that actually failed —
that silently drops a wanted application. The threshold + dry-run report + audit
trail bound that risk; `how_to_reverse` makes every flip undoable.

## 7. Error handling

- Missing `email_events` table / empty result → report "no outcome data; run a
  scan" and exit 0 (no writes).
- Gmail scan failure in Phase 0 → log, continue Phase 1 against existing
  `email_events` (scan is best-effort enrichment, not a hard dependency).
- `apply_resolutions` is per-job transactional and status-guarded: a row already
  moved off `crash_unconfirmed` (e.g. by another process) is skipped, not clobbered.
- Home brain opened **read-only** (`mode=ro`); fleet PG via the standard
  `pgqueue.connect`.

## 8. Testing (TDD)

Unit (pure, no live services — inject fakes like `test_diagnoser.py`):
- `load_outcome_emails` filters out `other`/`not_job`, keeps submission-proving stages.
- `reconcile`: strong method → confirmed; coarse fuzzy below threshold → probable;
  no hit → unmatched; one job matched by two emails resolves once (best wins).
- `reconcile` never classifies a `dry_run`/non-proving stage as confirmation.
- `apply_resolutions`: status-guarded UPDATE (skips a row not in
  `crash_unconfirmed`); writes one audit row per flip; idempotent on re-run.
- Threshold boundary: score exactly at `MIN_STRONG` is confirmed; just below is probable.

Integration (gated, opt-in): a small fixture PG + temp SQLite proving an
end-to-end confirmed flip and the audit row.

## 9. Interface summary

```
applypilot-fleet-reconcile-email                 # dry-run, scans last 7d, reports confirmed/probable/unknown
applypilot-fleet-reconcile-email --apply         # flip CONFIRMED matches -> applied
applypilot-fleet-reconcile-email --apply --apply-probable   # also flip probable
applypilot-fleet-reconcile-email --no-scan --apply          # use existing email_events only
```

## 10. Out of scope (explicit)

- **Re-attempting genuine failures** (unknowns with no email). Account-risk; a
  separate design with its own canary if ever pursued.
- **Track 2** (reducing `no_result_line` at the capture/parse path) — separate
  sub-project.
- **Continuous reconcile** (Approach C: extend the live scanner's candidate set so
  future scans auto-reconcile). Revisit once v1 proves match quality.
- Writing outcomes back into the home brain's `applications`/scores — owned by the
  Outcomes Integration spec, not this one.

## 11. Success criteria

- A dry-run report classifies all 480 into confirmed / probable / unknown with
  per-job method+score evidence.
- `--apply` flips only confirmed rows, writes an audit row per flip, and is
  idempotent.
- No write touches the home brain or any score; no apply attempt is ever created.
