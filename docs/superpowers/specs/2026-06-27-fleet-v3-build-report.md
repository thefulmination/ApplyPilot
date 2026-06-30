# Distributed Residential Fleet v3 — Build Report

**Date:** 2026-06-27 (overnight autonomous build)
**Repo:** `New project/ApplyPilot` (the Python tool), branch `applypilot-hardening-and-brainstorm-integration`
**Backup:** pushed to remote `private` (`thefulmination/applypilot-private`)
**Spec:** [`2026-06-26-distributed-residential-fleet-design.md`](2026-06-26-distributed-residential-fleet-design.md)

---

## TL;DR

The v3 fleet **coordination foundation** is built, adversarially reviewed, and tested: **102 Postgres-backed tests, all green.** Every module sits in `src/applypilot/fleet/` and is built on the proven `apply.pgqueue` lease pattern.

**What this is:** a tested, reviewable substrate for a distributed apply/compute/discovery fleet — the queues, the outcome-aware rate governor + circuit-breaker, the cross-board dedup, the owner approval gate, the token-auth broker (trust boundary), the brain↔Postgres sync bridge, fleet health/heartbeat, the captcha classifier, and the worker loop.

**What this is NOT:** a fleet that is applying to jobs right now. The browser/LLM/scrape calls are injected and stubbed; there are no live worker machines, no hosted Postgres, no owner-side Gmail relay wired, and no Helper app yet. See **§7 Runway to live**.

Nothing here touched your 7 in-flight files (`run-applypilot.ps1`, `discovery/jobspy.py`, `pipeline.py`, `scoring/cover_letter.py`, `scoring/tailor.py`, and two tests) — every commit staged only its own new/owned paths.

---

## 1. Module map

All under `src/applypilot/fleet/` (new package). "Tested-green" = real-Postgres tests via the disposable `applypilot-pgtest` cluster.

| Module | Lines | Status | What it does |
|---|---|---|---|
| `schema_v3.sql` + `schema.py` | 303 + 22 | **tested** | All v3 tables (compute/search/linkedin queues, rate_governor, applied_set, answer_bank, auth_challenge, otp_request, workers, heartbeat, poison_jobs, remote_commands, command_acks); idempotent `ensure_schema_v3`. |
| `dedup.py` | 73 | **tested** | Cross-board `(company, role)` dedup key (R9). Synonym-canonicalize *before* seniority strip so "Chief of Staff" survives. |
| `config.py` | 90 | **tested** | Approval policy, cost caps, pinned/canary version. |
| `governor.py` | 150 | **tested** | Outcome-aware adaptive breaker (R6): rising `challenge_rate` → throttle (paced) → pause → demote (sticky); auto-recover. Scopes: global / host / board / home_ip / account:linkedin. |
| `queue.py` | 449 | **tested** | Governed atomic claims (FOR UPDATE SKIP LOCKED): approval-gated + dedup-guarded apply; cost-capped compute; recurring board-governed search; **claim-time-serialized LinkedIn mutex**; lease-owner-guarded result writes; `park_challenge`/`resolve_challenge`. |
| `scheduler.py` | 176 | **tested** | Search-config → recurring `search_tasks` expansion (RF3); operator-owned enable/disable; coverage view. |
| `heartbeat.py` | 287 | **tested** | Health/stuck-detection, poison quarantine, per-worker broadcast commands, dashboard snapshot (R7). |
| `answer_bank.py` | 141 | **tested** | Fail-safe screening answers; defer-on-unknown; never guesses (R10). |
| `sync.py` | 330 | **tested** | SQLite-brain ↔ Postgres bridge: push-eligible / pull-results / advisory-only compute (never auto-promotes a score; never demotes a confirmed apply; never re-pushes a possibly-submitted posting). |
| `broker.py` | 518 | **tested (class)** | Token-auth trust boundary: enroll (sha256-hashed), lane-routed lease, **server-side owner-IP LinkedIn gate**, no-secrets config, Gmail-OTP stub. FastAPI surface is guarded (fastapi not installed in this env → tested at the class level). |
| `captcha.py` | 211 | **tested** | 8-way wall classifier; fail-safe (never returns a pass kind on a wall). |
| `worker.py` | 313 | **scaffold** | `WorkerLoop` with INJECTED `apply_fn`/`score_fn`/`search_fn` (wrap the real browser/LLM/jobspy — stubbed in tests). Compute + wall-park paths are exercised end-to-end with fakes; the live apply/search path is not runnable in CI. |

**Total:** ~3,091 lines of fleet code/schema, ~2,086 lines of tests.

---

## 2. How to run the tests

From the repo root, with the editable install in `.conda-env`:

```
.conda-env/python.exe -m pytest tests/test_fleet_v3_*.py tests/test_fleet_pgqueue.py -q
```

The `fleet_db` fixture (in `tests/conftest.py`) spins a **disposable local Postgres** from the `applypilot-pgtest` conda env (`initdb`/`pg_ctl`), applies the v3 schema, and truncates between tests. No external services, no network. If that env is missing the fleet tests skip cleanly (they don't fail).

Expected: **102 passed** (~25s).

---

## 3. Adversarial review — what it caught

After the first green suite (89 tests), I ran an 8-module adversarial review (independent reviewers reading source + tests for real defects and *vacuous* tests). It surfaced **12 HIGH + 10 MEDIUM** findings — several were R1-catastrophe-class (LinkedIn) that the green suite missed *because the security tests were vacuous on the adversarial path*. All HIGH and 9/10 MEDIUM are fixed, each with a regression test that fails-before / passes-after.

The ones that mattered most:

- **LinkedIn mutex didn't serialize.** `lease_linkedin` read the account governor row without a lock and only stamped the cap/gap *after* the apply — so two machines on the owner IP could both lease different LinkedIn rows concurrently → **two live sessions on one account**. Fixed: `FOR UPDATE` on the account row + reserve cap/min-gap *at claim time*. New test: 6 threads → exactly 1 lease.
- **LinkedIn results were structurally broken.** The broker routed LinkedIn results into `write_apply_result`, which updates `apply_queue` — but LinkedIn jobs live in `linkedin_queue`. The lease never closed (→ re-lease → double-apply) and the account cap never advanced. Fixed: new `write_linkedin_result` + correct broker routing.
- **Owner-IP gate was bypassable.** The LinkedIn `owner_ip` came from the *client* request body, so a friend could pass their own IP and pass the gate. Fixed: owner-IP + breaker home-IP are now **server-side only** (registered `public_ip` / broker-configured `owner_ip`); client values are ignored.
- **Parked captcha wasn't frozen.** A human-needed wall was "parked" but its lease kept its normal TTL, so the reclaim sweep re-queued it ~20 min later and another machine re-drove the same wall blind — burning the IP. Fixed: `park_challenge` pushes the lease out of the reclaim window; `resolve_challenge` is the owner-side release.
- **crash_unconfirmed re-applied.** A posting that may already have been submitted under your name could be re-pushed. Fixed: excluded from re-push + entered into `applied_set`.
- **answer_bank collapsed C++/C#/C** to one key (a vetted answer for one served for another). Fixed with technical-token sentinels.
- **Broadcast commands** were consumed by the first acker (others never saw them). Fixed with a per-worker `command_acks` table.
- Plus: governor "throttled" was a dead state (now leases, paced by a widened gap that no longer compounds), generic failures no longer pollute the captcha breaker signal, a manual search disable now survives a config re-expand, and read-only health queries roll back (no idle-in-transaction).

---

## 4. Safety invariants now enforced (and tested)

These map directly to the hard constraints:

- **One LinkedIn session, owner-IP only.** Claim-time mutex (serialized) + server-side owner-IP gate. A friend on a different egress IP physically cannot lease the LinkedIn lane, even with a valid token + capability.
- **Never double-apply under your name.** Cross-board `applied_set` dedup at lease time, covering confirmed *and* crash-unconfirmed postings; sync never re-pushes a possibly-submitted posting.
- **Captcha fail-safe.** The classifier never returns a pass kind on a wall; a human-needed wall is parked and frozen out of reclaim (never re-driven blind); only the owner resolves it.
- **No secret leaves the broker.** Tokens are stored sha256-hashed; `get_config` returns no DSN/token/answer-bank; the Gmail token is never distributed — `request_otp` is a stub and the real read is owner-side.
- **Research is advisory.** Compute results land in `research_*` columns; `fit_score`/`audit_score` are never auto-promoted. A confirmed apply is never demoted.

---

## 5. What is scaffolded (honest gaps)

- **`worker.py` is a scaffold.** The loop structure is real and unit-tested with fakes, but `apply_fn` (Playwright via `container_worker.run_job`), `search_fn` (jobspy), and `score_fn` (LLM) are injected and **stubbed**. The live apply/search path has never run here.
- **The FastAPI broker surface is untested in this env** (fastapi isn't installed). The `Broker` class — where all the security logic lives — is fully tested; the thin HTTP wrapper is guarded and would need a TestClient run once fastapi is present.
- **The owner-side Gmail OTP relay does not exist.** `request_otp` only records an audit row. The actual `gmail.readonly` read/match/return must be built on the trusted box.
- **No Helper app, no dashboard UI, no canary harness.** `dashboard_snapshot` returns the data; nothing renders it yet.
- **Captcha coverage is good, not exhaustive.** The classifier now covers Cloudflare/Turnstile/hCaptcha/reCAPTCHA v2+v3 plus Arkose/FunCaptcha/GeeTest/PerimeterX/DataDome, and fails safe (a wall never reads as `clear`). A future hardening is a positive success-marker check (require a confirmation token before declaring `clear`), which needs per-ATS tuning and isn't wired yet.

---

## 6. Commits (all on `private`)

```
9003bc6  feat(fleet-v3): foundation — schema, dedup, config
295b5e0  feat(fleet-v3): outcome-aware governor + governed atomic claims (S1 core)
e2ef53f  feat(fleet-v3): scheduler, health, answer-bank, sync, broker, worker+captcha (S1 breadth)
5c8f6b0  fix(fleet-v3): adversarial-review wave 1 — R1/safety-critical defects
c543092  fix(fleet-v3): adversarial-review wave 2 — correctness + safety hardening
abac0f5  docs(fleet-v3): BUILD REPORT + otp-docstring honesty fix
5ad982e  fix(fleet-v3): adversarial-review wave 3 — remaining correctness items
```

**Test count: 110 passing** (89 at first green → +11 wave-1/2 regressions → +8 wave-3).

---

## 7. Runway to live (next steps, in order)

1. **Rotate the DeepSeek key** (still outstanding from earlier — it was exposed).
2. Stand up a **hosted Postgres** (or run Topology A with the home box as broker) and point `ensure_schema_v3` at it.
3. **Wire the worker stubs to the real tool:** `apply_fn` → `container_worker.run_job`, `search_fn` → jobspy, `score_fn` → the live scorer. Run the FastAPI broker behind the worker RPC.
4. Build the **owner-side Gmail OTP relay** (the one trusted box that reads the code and returns it — never distributed).
5. Build the **Helper app** + zero-touch onboarding for the friend machines (non-technical, mixed Win/Mac; notarization deferred — walk them through).
6. **Canary first:** one owner-IP machine, tiny cap, owner-approval gate on, watch `dashboard_snapshot` + the breaker before adding machines.

This foundation is the hard, correctness-critical part — the queues, the governor, the trust boundary, the safety invariants — built and tested. The remaining work is integration and operational plumbing, not new core logic.
