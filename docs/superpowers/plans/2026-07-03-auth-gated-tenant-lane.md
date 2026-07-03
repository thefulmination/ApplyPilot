# Auth-Gated Tenant Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make login-gated ATS tenants (Workday family) applyable tenant-by-tenant via a supervised→trusted registry, home-lane only, without touching the fleet or storing any ATS password.

**Architecture:** A new `ats_tenants` brain table holds per-host status. The home acquire path (`launcher.acquire_job`, launcher.py:735) currently hard-skips auth-gated jobs; that skip becomes tenant-status-aware. Supervised mode adds a confirm-before-submit gate in the apply flow; trusted tenants flow through the normal home loop. The fleet push SELECT is byte-unchanged — lane isolation is structural.

**Tech Stack:** Python 3.11 (`.conda-env\python.exe`), sqlite3 (brain), typer CLI, pytest. Browser reuse = the existing persistent-profile Chrome path.

**Spec:** `docs/superpowers/specs/2026-07-03-auth-gated-tenant-lane-design.md` (approved 2026-07-03).

## Global Constraints

1. Tests: `.\.conda-env\python.exe -m pytest` from the repo root. Never `.venv`.
2. SHARED BRANCH: `git add` ONLY files you touched, never `-A`/`-u`. Do NOT touch launcher.py's fleet paths beyond the one acquire hook, diagnoser.py, or setup-fleet-pg-tailscale files.
3. HARD RULE for implementers: do the work YOURSELF — never spawn subagents/background tasks.
4. NO ATS password/secret ever written to the DB, profile.json, env, or logs. Sessions are the only credential.
5. FLEET ISOLATION INVARIANT: `apply/fleet_sync.py:56`'s auth-gated exclusion is byte-unchanged. Fleet workers never receive auth-gated jobs. Any task touching fleet_sync.py fails review.
6. NEVER-DOUBLE-APPLY: dedup_key + applied_at guards run BEFORE the tenant filter; unchanged.
7. Exact table `ats_tenants`, statuses `excluded|supervised|trusted`, default `excluded`, default `daily_cap=5`, promotion evidence threshold `clean_submits >= 3`.

---

### Task 1: `ats_tenants` table + `tenant_status`/registry helpers

**Files:**
- Modify: `src/applypilot/database.py` (add CREATE + additive migration next to the other ensure blocks; find the ensure pattern via `grep -n "CREATE TABLE IF NOT EXISTS" src/applypilot/database.py`)
- Create: `src/applypilot/tenants.py` (registry read/write helpers)
- Test: `tests/test_tenants_registry.py`

**Interfaces:**
- Produces the table exactly as the spec's DDL (host PK, status default 'excluded', clean_submits/failed_submits default 0, daily_cap default 5, halted_until, last_result, updated_at).
- Produces in `tenants.py`:
  - `tenant_status(conn, host: str) -> str` — row's status, or `"excluded"` if no row / table absent (defensive: catch sqlite3.OperationalError → "excluded").
  - `list_tenants(conn) -> list[dict]` — all rows.
  - `set_tenant(conn, host: str, status: str, *, force: bool=False) -> dict` — upsert status; raise `ValueError` if status not in the 3-set; if promoting to 'trusted' with clean_submits < 3 and not force → raise `ValueError("needs >=3 clean submits (or --force)")`.
  - `record_submit(conn, host: str, *, ok: bool, result: str|None) -> None` — increments clean_submits or failed_submits, sets last_result + updated_at.
  - `halt_tenant(conn, host: str, until_iso: str) -> None` / `is_halted(conn, host, now_iso) -> bool`.
  - `_host_of(url: str) -> str` — urlsplit hostname, strip leading "www." (reuse if an equivalent already exists in config/gmail_outcomes; grep first).

- [ ] **Step 1: Failing tests** — unknown host → "excluded"; set/list round-trip; set to bogus status raises ValueError; promote-to-trusted with clean_submits=0 raises without force, succeeds with force; record_submit ok/fail increments the right counter; halt + is_halted honor the timestamp; table-absent tenant_status returns "excluded".
- [ ] **Step 2: Run** → FAIL (module/table missing).
- [ ] **Step 3: Implement** database.py DDL+migration and tenants.py.
- [ ] **Step 4: Run** `.\.conda-env\python.exe -m pytest tests/test_tenants_registry.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add src/applypilot/database.py src/applypilot/tenants.py tests/test_tenants_registry.py && git commit -m "feat(tenants): ats_tenants registry + status/submit/halt helpers"`

---

### Task 2: `applypilot tenants` CLI (list / set / halt)

**Files:**
- Modify: `src/applypilot/cli.py` (new `tenants` command group; mirror an existing multi-verb command's style)
- Test: `tests/test_tenants_cli.py`

**Interfaces:**
- Consumes: everything in `tenants.py` (Task 1).
- `applypilot tenants` (no verb) or `tenants list` → table: host, status, clean/failed, daily_cap, halted?, eligible-job count (`SELECT COUNT(*) FROM jobs WHERE <host-of application_url/url> = host AND applied_at IS NULL`).
- `applypilot tenants set <host> <status> [--force]` → calls `set_tenant`; prints the ValueError message and exits 1 on rejection.
- `applypilot tenants halt <host>` → `halt_tenant` to end of local day.

- [ ] **Step 1: Failing tests** (typer CliRunner, monkeypatch _bootstrap + a temp brain via init_db like tests/test_import_decisions.py's _setup): `tenants set foo.com supervised` then `tenants list` shows it; `tenants set foo.com trusted` without force exits non-zero with the evidence message; `--force` succeeds.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.\.conda-env\python.exe -m pytest tests/test_tenants_cli.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add src/applypilot/cli.py tests/test_tenants_cli.py && git commit -m "feat(tenants): tenants list/set/halt CLI"`

---

### Task 3: Tenant-aware acquire filter

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (the auth-gated skip block at ~:735, inside `acquire_job`)
- Test: `tests/test_apply_auth_gated_tenant.py`

**Interfaces:**
- Consumes: `tenants.tenant_status`, `tenants.is_halted`, and a per-tenant daily-submit count.
- The skip block at launcher.py:735 currently parks any auth-gated row `auth_required` when `APPLYPILOT_SKIP_AUTH_GATED` is on and inbox-auth is off. NEW logic: before parking, check `tenant_status(conn, _host_of(apply_url))`:
  - `supervised` or `trusted` AND not halted AND under `daily_cap` → DO NOT park; let the row proceed to apply (the row is allowed through the auth-gate skip).
  - else → existing park behavior, unchanged.
- A new env/param `APPLYPILOT_AUTH_GATED_MODE` set to `supervised` or `trusted` scopes WHICH tenant statuses are eligible this run: supervised-mode run accepts supervised+trusted tenants; a normal (trusted) home run accepts ONLY trusted (so supervised tenants never apply unattended). Default unset = trusted-only (safe).
- Daily cap: count submits for that host since local midnight from the applications ledger (`applications` table join, or jobs.applied_at) — a helper `tenants.submits_today(conn, host) -> int`.

- [ ] **Step 1: Failing tests** — seed a temp brain + an auth-gated job at host X; assert: X excluded (parked auth_required) when no tenant row; X allowed when tenant supervised AND mode=supervised; X NOT allowed when tenant supervised but mode=trusted (unattended run); X halted → parked; X at daily_cap → parked; a NON-auth-gated job is unaffected in all cases (regression). Use direct `acquire_job` calls against the temp brain with the fleet paths stubbed as the existing apply tests do (read tests/test_apply_auth_required.py + test_apply_lane_filter.py for the established harness FIRST).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — add `submits_today` to tenants.py; modify ONLY the skip block. Do not alter fleet_sync.py.
- [ ] **Step 4: Run** `.\.conda-env\python.exe -m pytest tests/test_apply_auth_gated_tenant.py tests/test_apply_auth_required.py tests/test_apply_lane_filter.py -q` → PASS (existing lane tests must stay green — they prove fleet isolation).
- [ ] **Step 5: Commit** — `git add src/applypilot/apply/launcher.py src/applypilot/tenants.py tests/test_apply_auth_gated_tenant.py && git commit -m "feat(tenants): tenant-aware auth-gate acquire filter (supervised/trusted/halt/cap)"`

---

### Task 4: Supervised confirm-before-submit gate

**Files:**
- Modify: `src/applypilot/apply/prompt.py` (add the SUPERVISED-CONFIRM instruction, gated on a flag) — grep for where the apply agent prompt is assembled
- Modify: `src/applypilot/apply/launcher.py` (thread a `supervised` flag from the run entry into the prompt build + the RESULT handling: on supervised, after the agent stops at the confirm point, prompt the OWNER via stdin `y/n`; y → allow the recorded RESULT:APPLIED + `tenants.record_submit(ok=True)`, n → mark abandoned + `record_submit(ok=False, result=<reason>)`)
- Test: `tests/test_apply_supervised_confirm.py`

**Interfaces:**
- Consumes: `tenants.record_submit`.
- The supervised prompt addition instructs the agent: complete the entire form, then EMIT a distinct sentinel line (e.g. `RESULT:AWAIT_CONFIRM`) INSTEAD of submitting, and wait. The launcher, seeing that sentinel in supervised mode, reads a `y/n` from stdin (owner present): `y` → tell the agent (or re-invoke) to submit and treat the subsequent `RESULT:APPLIED` normally; `n` → abandon, no submit.
- Because driving a live headed agent interactively is hard to unit-test, the TESTABLE seam is the launcher's confirm-decision function: `resolve_supervised_confirm(sentinel_seen: bool, owner_input: str) -> tuple[bool, str]` (submit?, reason) + the record_submit calls. Unit-test that function + that record_submit is called with ok=True on "y" and ok=False on "n"/reason. The actual stdin wiring is thin and exercised manually (owner-present by definition).

- [ ] **Step 1: Failing tests** — `resolve_supervised_confirm(True,"y") == (True,"")`; `(True,"n") == (False,"abandoned")`; `(True,"skip: bad fit") == (False,"bad fit")`; `(False, anything)` → (False, "no confirm sentinel"); and a test that a supervised run calling the decision path invokes `record_submit` with the right ok flag (monkeypatch record_submit, assert calls).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the prompt addition + `resolve_supervised_confirm` + record_submit wiring. Keep the sentinel out of the non-supervised path entirely (zero behavior change when supervised is off).
- [ ] **Step 4: Run** `.\.conda-env\python.exe -m pytest tests/test_apply_supervised_confirm.py tests/test_apply_prompt.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add src/applypilot/apply/prompt.py src/applypilot/apply/launcher.py tests/test_apply_supervised_confirm.py && git commit -m "feat(tenants): supervised confirm-before-submit gate + submit accounting"`

---

### Task 5: `apply --auth-gated` entry + same-day halt on challenge

**Files:**
- Modify: `src/applypilot/cli.py` (the `apply` command: add `--auth-gated` flag that sets `APPLYPILOT_AUTH_GATED_MODE=supervised`, forces headed, forces home-box, and `--tenant <host>` to scope to one host)
- Modify: `src/applypilot/apply/launcher.py` (on a CAPTCHA/login-wall/challenge RESULT during an auth-gated apply, call `tenants.halt_tenant` end-of-day + skip remaining jobs for that host)
- Test: `tests/test_apply_auth_gated_cli.py`

**Interfaces:**
- Consumes: everything above.
- `applypilot apply --auth-gated [--tenant X] [--limit N]` → supervised mode, headed, home-only; if zero supervised/trusted tenants exist, print the excluded list + the enable command and exit 0 (never silently no-op).
- Halt-on-challenge: when the RESULT parser sees CAPTCHA/LOGIN_ISSUE/AUTH_REQUIRED for an auth-gated apply, halt that tenant for the day and continue with other hosts.

- [ ] **Step 1: Failing tests** — CliRunner: `apply --auth-gated` with no enabled tenants prints the enable hint and exits 0 (stub acquire to assert it's never entered); halt-on-challenge unit: a function `handle_auth_gated_result(conn, host, result_token)` halts on the challenge tokens and no-ops on APPLIED (monkeypatch halt_tenant, assert).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.\.conda-env\python.exe -m pytest tests/test_apply_auth_gated_cli.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add src/applypilot/cli.py src/applypilot/apply/launcher.py tests/test_apply_auth_gated_cli.py && git commit -m "feat(tenants): apply --auth-gated entry + same-day halt on challenge"`

---

### Task 6: Trusted-tenant home-lane passthrough + verification

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (confirm the trusted-tenant acquire path from Task 3 also engages for the NORMAL home run — i.e. a plain `applypilot apply` / supervise-apply with no `--auth-gated` accepts trusted tenants). If Task 3's default-unset = trusted-only already covers this, this task is verification + a docs/runbook note.
- Create/Modify: `docs/auth-gated-tenant-runbook.md`
- Test: covered by Task 3's trusted-mode test; add one end-to-end filter test if a gap exists.

- [ ] **Step 1:** Verify (read Task 3's implementation): does a plain home run (no AUTH_GATED_MODE env) admit `trusted` tenants and still exclude `supervised`/`excluded`? If yes, add a test pinning exactly that if not already present. If no, fix the default so trusted passes through the normal loop.
- [ ] **Step 2: Full sweep** — `.\.conda-env\python.exe -m pytest tests/test_tenants_registry.py tests/test_tenants_cli.py tests/test_apply_auth_gated_tenant.py tests/test_apply_supervised_confirm.py tests/test_apply_auth_gated_cli.py tests/test_apply_auth_required.py tests/test_apply_lane_filter.py -q` → PASS. Plus fleet-isolation regression: `.\.conda-env\python.exe -m pytest tests/test_fleet_apply_lane.py tests/test_fleet_v3_sync.py -q` (prove fleet still excludes auth-gated).
- [ ] **Step 3: Runbook** — write `docs/auth-gated-tenant-runbook.md`: (1) log into a tenant once in the home Chrome profile; (2) `applypilot tenants set <host> supervised`; (3) `applypilot apply --auth-gated --tenant <host> --limit 3` and confirm each; (4) `applypilot tenants set <host> trusted` once ≥3 clean; (5) trusted jobs then apply via the normal home loop with the daily cap. Include the fleet-isolation guarantee + the never-store-password note.
- [ ] **Step 4: Read-only live sanity** — count eligible auth-gated jobs per tenant host in the live brain (mode=ro): `SELECT <host>, COUNT(*) FROM jobs WHERE applied_at IS NULL AND <auth-gated host> GROUP BY 1 ORDER BY 2 DESC` — report the top tenants + totals so the owner knows where to start (expect RBC/Adobe/FIS/BMO/CIBC/TD; ~94 of the res_build-kept set).
- [ ] **Step 5: Commit** — `git add docs/auth-gated-tenant-runbook.md <any test> && git commit -m "docs(tenants): auth-gated tenant runbook + trusted-passthrough verification"`
