# Fleet Auto-Fixer (Remediator) — Design Spec

**Date:** 2026-06-29
**Status:** Approved design. This is **Phase 2** of the Fleet Diagnoser line (Phase 1 = advisory log-reading
diagnosis, shipped). v1 scope is a single, deterministic, autonomous action: **re-queue never-submitted jobs.**
**Repo:** `applypilot` (Python fleet tool), branch `applypilot-hardening-and-brainstorm-integration`.

## Problem

The Fleet Diagnoser (Phase 1) names the real root cause of apply failures but is **advisory only** — it writes
`recommended` rows; a human still acts on every one. Failure volume now outpaces manual keep-up. Live data:
`failed:no_result_line` is **181 all-time and ~44/hour** — by far the dominant failure — and it is largely
**usage-limit casualties**: a worker hit its model quota *before* interacting with the page, so the job was
**never submitted** and was wrongly parked. Recovering these today is a manual chore (the operator re-queues the
usage-limit casualties by hand, exactly as was done after the 283-job quarantine incident).

## Goal

Automate that recovery: an autonomous, bounded, reversible remediator that **re-queues jobs the diagnosis proves
were never submitted**, so the dominant failure class clears itself without manual keep-up — while making a
double-apply **impossible by construction**.

## Non-negotiable safety rule

**NEVER double-apply under the user's name.** A job is re-queued only if it is *provably never submitted*. Any
ambiguity → leave it parked and emit a recommendation. This rule governs every decision below.

## Scope (v1)

- **In:** re-queue never-submitted (usage-limit-casualty) jobs, autonomously, ATS lane only.
- **Out (recommend-only for now, deferred to later phases):** restart stuck/broken workers, pause bot-flagged
  hosts, switch a worker's model. (These were considered and explicitly de-scoped from v1.)
- **No LLM in the action path.** "Never submitted" is decided by the diagnoser's **deterministic Tier-0
  `usage_limit`** signal, so v1 has **zero prompt-injection action surface**. The capable-agent Tier-2 from the
  original diagnoser spec is **not needed** for v1.

## Architecture

A new standalone unit `src/applypilot/fleet/remediator.py`, run on the **home box** (where both the fleet PG and
the SQLite brain are reachable). It is deliberately **not** folded into:

- **the Fleet Doctor** — the Doctor's safety rests on being *monotonically conservative* (every action makes the
  fleet **more** cautious). Re-queue is **expansionary** (it returns jobs to the apply pool); bolting it onto the
  Doctor would corrupt the one invariant that makes the Doctor trustworthy.
- **the Diagnoser** — Phase 1 is advisory/read-only; adding a mutation there destroys that separation.

So the remediator is its own bounded unit: it **reads** diagnoses + ground-truth signals and performs the single
expansionary action behind its own guards, caps, and audit trail.

## The 3-layer double-apply guard (the heart)

A candidate job is re-queued **only if ALL three guards pass**. Guards 2 and 3 are independent ground-truth
sources; either one vetoing is sufficient to block.

| # | Guard | Passes when | Source | Availability |
|---|-------|-------------|--------|--------------|
| 1 | **Provably never submitted** | the job is a usage-limit casualty (see Candidate selection) — driven by the deterministic Tier-0 `usage_limit` diagnosis | `fleet_diagnoses` (reason=`usage_limit`) + `apply_queue.apply_error` | live |
| 2 | **Internal record: not applied** | the job's `dedup_key` is **NOT** in `applied_set` | fleet PG `applied_set` (keyed by `dedup_key`; 871 rows live) | live |
| 3 | **External record: not acknowledged** | there is **NO** confirming `email_events` row for the job's url (recruiter never acknowledged an application) | brain SQLite `email_events` (keyed by `job_url`) | activates when the outcomes-tracker scanner runs; **absent today** |

**Guard 3 timing.** Email confirmations lag the apply by minutes–hours, so *absence* of an email is **not** proof
of non-application — guard 3 is a **veto when a confirming email is present**, layered on top of guards 1–2, never
a standalone "safe" signal. `email_events` does not exist in the live brain yet (the other session is building the
outcomes-tracker); the remediator therefore treats guard 3 as **optional + graceful**: if the table is missing or
empty it is skipped (the two live guards still gate every re-queue), and it strengthens automatically once the
scanner runs — **no remediator change required**.

If any guard fails (or guard data is contradictory) → **do not re-queue**; emit a `recommended` row instead.

## Candidate selection (what counts as a usage-limit casualty)

For each worker that currently has a Tier-0 `usage_limit` diagnosis (`fleet_diagnoses.reason='usage_limit'`,
`source='tier0'`, open/recent), select its jobs in `apply_queue` where **all** hold:

- `lane = 'ats'` (LinkedIn lane is **never** a candidate — additive to the existing no-LinkedIn-auto-action posture);
- `status IN ('failed','crash_unconfirmed')`;
- `apply_error` is in the **non-submission family**: `no_result_line`, `crash_unconfirmed`, or a usage-limit-family
  error (the agent never reached the form);
- `updated_at` falls within the worker's **usage-limit window** — defined as the diagnoser's recent-failure
  lookback that produced the diagnosis (the 30-minute window `load_worker_ctx` reads), bounded forward by the
  diagnosis `created_at`. (These are failures from the quota onset onward — the casualties — not older failures
  that may have been genuine attempts.)
- the remediator has not already re-queued this job `K` times (see caps).

The Tier-0 `usage_limit` diagnosis is the load-bearing "never submitted" evidence: a usage-limit error means the
agent's first model call hit the quota *before* any page interaction. Jobs whose evidence does **not** match this
(e.g., a real mid-form crash) are **not** candidates and stay parked. **Window precision affects coverage, not
safety:** even if the window over-selects, guards 2 (`applied_set`) and 3 (`email_events`) hard-veto any job that
was actually applied, so a loose window can only *miss* a casualty — never cause a double-apply.

## Re-queue mechanism + reversal

To re-queue a job: set `status='queued'`, reset the parked `attempts` (the reclaim fix sets `attempts=99` to make
crash_unconfirmed un-leasable) back to a re-leasable value, clear the lease (`lease_owner`/`lease_expires_at`), and
annotate `apply_error` with a remediator tag (e.g. `requeued_by_remediator:usage_limit`) so the prior reason is not
silently lost. Record the **prior** `(status, attempts, apply_error)` in the audit row so the action is exactly
reversible.

## Caps / anti-thrash

- **Per-pass blast-radius cap:** re-queue at most `N` jobs per pass (default `N=50`); overflow → recommend.
- **Per-job re-queue limit:** a job may be re-queued by the remediator at most `K` times ever (default `K=2`).
  After `K`, leave it parked and emit a `recommended` row (a job that keeps dying to usage-limit must not loop).
  Tracked via a small `remediation_actions` audit table (or a counter column), keyed by job url.
- Re-queued jobs become `queued` and are leasable by any non-usage-limited worker/pool; if every pool is still
  usage-limited they simply wait — harmless. (Holding until the parsed reset time is a possible later optimization,
  not in v1.)

## Reversibility / audit

Every re-queue writes one row to a `remediation_actions` table (fleet PG): `url`, `worker_id`, `action='requeue'`,
`reason` (the diagnosis), `prior_status`, `prior_attempts`, `prior_apply_error`, `created_at`, and `how_to_reverse`
(restore the prior triple). This renders in the LAN console and makes every autonomous action auditable and
one-step reversible. A re-queue that is later vetoed nowhere appears; a *recommendation* (guard failure / cap
overflow) is written as a `fleet_diagnoses` `recommended` row, reusing the existing surface.

## Trigger

A console-script `applypilot-fleet-remediate` (`src/applypilot/fleet/remediator_main.py`):

- `--once` — single pass (canary / manual).
- `--interval <s>` — loop (autonomous home-box operation), mirroring `applypilot-fleet-doctor`'s launcher pattern.
- `--max-requeue <N>` / `--max-per-job <K>` — override the caps.
- `--dsn` — fleet PG DSN (defaults to the env DSN `pgqueue.connect` resolves).

Default posture is **opt-in** (the operator starts it), consistent with every other lane. A standalone
`run-fleet-remediate.ps1` launcher is provided in the ops folder alongside `run-fleet-selfheal.ps1`.

## Safety invariants

- **Never double-apply:** the 3-guard gate is mandatory; any doubt → recommend, not act.
- **ATS only:** LinkedIn-lane jobs are never candidates and never re-queued.
- **Expansionary action is isolated:** only this unit re-queues; the Doctor stays conservative-pure, the diagnoser
  advisory-pure.
- **Bounded:** per-pass cap + per-job cap; no unbounded loop.
- **Reversible + audited:** every action has a prior-state audit row + how-to-reverse.
- **Deterministic + free:** no LLM in the action path; `$0/pass`.
- **Graceful degradation:** missing `email_events` (guard 3) or missing diagnoses → fewer candidates / skip guard,
  never a crash and never a *weaker* double-apply guarantee than guards 1–2 alone.

## Testing

- **Unit (fake conn, no real DB):** each guard independently vetoes (diagnosis-not-usage-limit, in-`applied_set`,
  confirming-`email_events`); candidate selection picks only ATS non-submission failures in the usage-limit window;
  per-pass cap and per-job cap enforced (overflow → recommend, not re-queue); LinkedIn-lane job is never selected;
  the re-queue writes the exact reversal audit row; guard 3 gracefully skipped when `email_events` is absent.
- **Adversarial:** a job with a confirming `email_events` row is **NOT** re-queued even when the diagnosis says
  `usage_limit` (guard 3 / guard 2 must hard-veto). A job whose evidence is a real mid-form crash is **NOT** a
  candidate.
- **Integration:** seed a fleet PG with usage-limited workers + parked casualty jobs + an `applied_set` hit + (when
  available) an `email_events` hit; one `--once` pass re-queues exactly the safe set and writes the audit rows.

## Files

- `src/applypilot/fleet/remediator.py` — candidate selection + the 3-guard gate + bounded re-queue + audit.
- `src/applypilot/fleet/remediator_main.py` — `applypilot-fleet-remediate` CLI (console-script in `pyproject.toml`).
- `remediation_actions` table — add to the fleet schema (additive, idempotent `CREATE TABLE IF NOT EXISTS`).
- `tests/test_remediator.py` (+ a CLI test).
- `ApplyPilot-ops\run-fleet-remediate.ps1` — standalone launcher (ops folder, not the repo).

## Phasing / deferred

- **v1 (this build):** re-queue never-submitted, the 3-guard gate, caps, audit/reversal, CLI + launcher.
- **Later:** restart stuck/broken workers (reuse `MonitorActions.restart_worker`); pause bot-flagged hosts
  (`pause_scope`, ATS-only, approval-gated); switch-model on usage limit (new action, approval-gated). These re-use
  the existing hardened action surfaces and the graduated-trust model and are out of v1 scope.

## Decisions made during design

- **Graduated-trust autonomy:** v1's single action (re-queue) is safe + reversible + high-volume → autonomous; the
  riskier fixes are deferred (approval-gated when built).
- **Deterministic, no LLM:** "never submitted" rides the Tier-0 `usage_limit` signal → no injection action surface.
- **Email as the external ground truth (guard 3):** consumed read-only from the outcomes-tracker's `email_events`;
  optional + graceful until that scanner runs; strengthens the guard automatically when it does.
- **Standalone remediator, not a Doctor knob:** keeps the Doctor's monotonically-conservative invariant intact.
