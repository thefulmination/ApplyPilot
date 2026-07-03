# Outcome-loop integrity (email→job match guards + re-audit + cadence) — design

**Approved:** 2026-07-03 (owner chose quarantine-+ report posture; approach A of A/B/C).
**Problem (audit 7/02, verified):** `match_email_to_job()` (src/applypilot/gmail_outcomes.py)
attributes emails to applied jobs with no temporal sanity check and no same-company
disambiguation. 2 of 26 matched rejections provably predate their application (an email about
an old, pre-ApplyPilot application at the same company gets pinned onto a fresh apply). This
matters beyond analytics: `fleet/email_reconcile.py` flips `crash_unconfirmed → applied` using
these events, so a misattributed "thank you for applying" can wrongly confirm an apply.
The interview/offer classifier stages ALREADY exist (rebuilt ~6/30) — classification is NOT in
scope; attribution integrity, a re-audit of stored rows, and scan cadence are.

## Non-goals
- No classifier changes (offer/interview/rejected/acknowledged/ambiguous stages are shipped).
- No LLM adjudication tier (untrusted-email injection surface feeding the reconciler; volume
  doesn't justify it — rejected approach B).
- No thread-ledger rewrite (rejected approach C: kills LinkedIn/external-ATS coverage).

## Components

### 1. Temporal guard (gmail_outcomes.py)
`match_email_to_job()` gains a required `occurred_at` (ISO str) parameter. A job in
`applied_jobs` is match-eligible only if `job.applied_at <= occurred_at + GRACE` where
`GRACE = 1h` (module constant, covers clock skew; acknowledgments can arrive seconds after
submit). Guard applies to EVERY tier (exact tiers included — a board-slug hit on a job applied
AFTER the email is still wrong). If one or more tiers matched a job but ALL matched candidates
fail the guard → return a quarantine result with reason `predates_application`.

Return type changes from `(job, method, score)` to a small dataclass
`MatchResult(job, method, score, status, reason)` with `status ∈ {"attributed",
"needs_review", "unmatched"}` — callers updated (single call site in the scan flow).

### 2. Same-company disambiguation (gmail_outcomes.py)
When the winning tier is fuzzy (`ats_domain` or `company_name`) and 2+ guard-eligible applied
jobs share the matched company (case-folded `_clean_company` equality): try title-token overlap
(job title tokens ∩ subject+body tokens, stopwords out); a unique highest-overlap winner with
≥1 token wins and the method is suffixed `+title`. No unique winner → quarantine with reason
`ambiguous_company`. `board_slug` and `linkedin_job_id` are job-specific and skip this check.
`company_domain` is exact to the EMPLOYER but not the job, so it IS subject to the ambiguity
check when 2+ guard-eligible jobs share that employer domain.

### 3. Quarantine tier (database.py + scan writer)
`email_events` gains additive columns (ALTER TABLE, idempotent, mirrors existing migrations):
- `match_status TEXT` — `attributed` | `needs_review` | NULL (legacy rows treated as
  attributed by consumers only if `job_url` is set; the re-audit backfills them).
- `match_reason TEXT` — `predates_application` | `ambiguous_company` | NULL.
- `prev_job_url TEXT` — audit trail: the attribution a re-audit removed (reversibility).
Quarantined events keep outcome/stage/company but `job_url = NULL`. The scan summary prints
`needs_review: N (predates=X ambiguous=Y)` so quarantines are never invisible.

### 4. Consumer hardening (fleet/email_reconcile.py, fleet/remediator.py)
Every query that reads `email_events` as apply-evidence adds
`AND job_url IS NOT NULL AND COALESCE(match_status,'attributed') = 'attributed'`.
Quarantined events can never confirm an apply or seed remediation.

### 5. Re-audit pass (cli.py: `scan-gmail --reaudit`)
Re-runs guards 1+2 over ALL stored `email_events` rows (no Gmail API calls — pure DB):
for each row with a `job_url`, re-validate against the CURRENT jobs table (`applied_at`
comparison + ambiguity re-check using the stored subject/company). Failing rows:
`prev_job_url = job_url`, `job_url = NULL`, `match_status='needs_review'`, reason set.
Passing rows: `match_status='attributed'` (backfills legacy NULLs). Prints a flip report
(count by reason + the flipped message_ids). Reversal: manual UPDATE from `prev_job_url`
(documented in the report footer); the pass itself never deletes rows.

### 6. Cadence (register-fleet-tasks.ps1)
Add a daily `applypilot-scan-gmail` task to the home machine's task list, following the file's
existing idempotent unregister-then-register pattern, invoking `run-applypilot.ps1 scan-gmail`
(owner env: Gmail OAuth creds live in ~/.applypilot; run-applypilot pins the live brain).
Schedule 07:00 daily — after the nightly PG backup (03:30), before the owner's day.

## Error handling
- Missing/garbled `occurred_at` on an email → the temporal guard cannot be evaluated →
  quarantine as `needs_review` with its own reason `no_timestamp` (never guess).
- Jobs with NULL `applied_at` in the candidate list (shouldn't happen — list is applied jobs)
  are skipped defensively.
- `--reaudit` on a brain with zero events exits 0 with "nothing to re-audit".

## Testing (pytest, existing patterns in tests/test_gmail_outcomes*.py / test_email_reconcile.py)
1. Regression: rejection email dated BEFORE `applied_at` at a company with one applied job →
   `needs_review/predates_application`, job_url NULL (reproduces the audit's Checkr case).
2. Acknowledgment 5 minutes after apply passes the guard (grace works).
3. Two applied jobs at one company, subject names one title → attributed `+title`; subject
   names neither → `needs_review/ambiguous_company`.
4. Exact board_slug match to a job applied after the email → quarantined (guard beats exactness).
5. Reconciler ignores `needs_review` rows (a quarantined "applied-confirmation" cannot flip
   crash_unconfirmed).
6. `--reaudit` flips a seeded-bad legacy row, backfills `attributed` on a good one, preserves
   `prev_job_url`, and is idempotent on second run.

## Success criteria
- Zero `attributed` events whose email predates the application (query in the report).
- The 2 known-bad live rows flip to `needs_review` on the owner's first `--reaudit`.

## Amendments (2026-07-03 planning + implementation)

1. `match_email_to_job` has THREE production call sites: `scan_inbox`, `outcome_scan.build_email_event`,
   and `fleet/email_reconcile.reconcile` — all three are guarded by the temporal + ambiguity checks
   described above. The reconciler's crash-candidate rows carry `guard_after = the PG row's
   `updated_at`` (not `applied_at`) since crash-candidate rows have no `applied_at` to guard against.
2. `remediator.py`'s `has_confirming_email` veto deliberately reads ALL `email_events` rows, INCLUDING
   quarantined (`match_status = 'needs_review'`) ones. Negative evidence (an email exists that could
   plausibly confirm an apply) stays conservative on purpose — quarantine only strips an event's
   authority to positively confirm an apply, not its authority to block a remediation. Only
   positive-evidence consumers (apply confirmation, crash_unconfirmed flips) filter on
   `job_url IS NOT NULL AND COALESCE(match_status,'attributed') = 'attributed'`.
3. A fourth `match_reason`, `rematch_mismatch`, exists alongside `predates_application`,
   `ambiguous_company`, and `no_timestamp`. It is written ONLY by `--reaudit` when a stored
   attribution (existing `job_url`) re-matches to a different job than the one it currently
   points to, or fails to re-match to any job at all — i.e. the re-audit's own attribution
   changed out from under a previously-attributed row, distinct from a plain temporal/ambiguity
   guard failure on first attribution.
4. §6 (cadence) is SUPERSEDED: the autonomy-loop build (register-fleet-tasks.ps1, Phase 1.4)
   already registers a 6-hourly home task `OutcomeScan` running `outcomes-scan --days 7` then
   `reconcile-email --apply` — strictly better than the daily 07:00 task this spec proposed.
   The duplicate daily task was implemented, then reverted; no new task ships with this plan.
   The success criterion "scan lands interview/offer events without owner action" is satisfied
   by the pre-existing 6-hourly task. `outcomes-scan`/`reconcile-email` were added to
   run-applypilot.ps1's `$WriteCmds` so MANUAL owner runs also trigger the integrity-gated
   brain backup (the scheduled wrapper invokes the exe directly and is unaffected).
