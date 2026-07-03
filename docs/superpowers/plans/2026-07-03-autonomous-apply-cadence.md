# Autonomous Apply Cadence + Dead-Man Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Schedule the three unscheduled pipeline stages (verify-live, apply push+approve, dead-man alert) so the fleet applies autonomously under the rolling $100/day cap and the owner is alerted within ~20 min of any silent death / stall / running-hot.

**Architecture:** Pure-Python testable cores (`deadman_check`, guarded `resume_if_safe`) + thin PowerShell wrappers registered as home scheduled tasks by a NEW `register-apply-cycle.ps1`. Nothing existing is modified except the Build-11 console (adds a red banner) and the fleet schema (one additive column).

**Tech Stack:** Python 3.11 (`.conda-env\python.exe`), psycopg via `pgqueue`, pytest + `fleet_db` fixture, stdlib console, PowerShell Task Scheduler.

**Spec:** `docs/superpowers/specs/2026-07-03-autonomous-apply-cadence-design.md` (approved 2026-07-03; lifetime `spend_cap_usd` removed live = 0; rolling `cost_cap_daily_usd`=$100 is the sole throttle).

## Global Constraints

1. Tests: `.\.conda-env\python.exe -m pytest` from `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`. Never `.venv`. PG tests use the `fleet_db` fixture (skip cleanly if pgtest env absent).
2. SHARED BRANCH: `git add` ONLY files you touched, never `-A`/`-u`. Do NOT touch `register-fleet-tasks.ps1` (other session's), `diagnoser.py`, `fleet/doctor.py`, `apply/fleet_sync.py`, `setup-fleet-pg-tailscale` files.
3. HARD RULE for implementers: do the work YOURSELF — never spawn subagents/background tasks.
4. **CATASTROPHE GUARD:** nothing in this build may clear `ats_paused`/`ats_pause_source` (Doctor-owned) or any LinkedIn halt. The guarded resume touches ONLY the plain `paused` flag and ONLY with `AND ats_paused=FALSE`. DeadMan is READ-ONLY on the fleet + advisory (never pauses/halts/resumes anything).
5. DeadMan thresholds (constants, one place): heartbeat-stale = 30 min; stalled-queue = 0 applies in 3h while armed; running-hot = rolling-24h ≥ 95% of `cost_cap_daily_usd` for ≥2 consecutive checks.

---

### Task 1: `deadman_check` pure detector + tests

**Files:**
- Create: `src/applypilot/fleet/deadman.py`
- Test: `tests/test_deadman_check.py` (`fleet_db` fixture)

**Interfaces:**
- Produces `@dataclass Alert: kind: str; severity: str; detail: str` and
  `deadman_check(conn, *, now: datetime, prev_hot_streak: int = 0) -> tuple[list[Alert], int]`
  returning (alerts, new_hot_streak). Conditions (each a distinct `kind`):
  - `silent_death`: `SELECT paused, ats_paused FROM fleet_config WHERE id=1` armed (`paused=FALSE AND ats_paused=FALSE`) AND `MAX(last_beat) FROM worker_heartbeat WHERE worker_id !~ 'watchdog|linkedin'` is NULL or `< now - 30min`.
  - `stalled_queue`: armed AND `EXISTS(SELECT 1 FROM apply_queue WHERE status='queued' AND approved_batch IS NOT NULL)` AND `NOT EXISTS(SELECT 1 FROM apply_queue WHERE status='applied' AND updated_at > now - 3h)`.
  - `selfheal_dead`: `MAX(last_beat) FROM worker_heartbeat WHERE worker_id ~ 'watchdog'` is NULL or `< now - 30min` (the Doctor/Watchdog that would heal is itself down).
  - `running_hot`: rolling-24h `SUM(llm_usage.cost_usd) WHERE ts >= now-24h` ≥ 0.95 * `cost_cap_daily_usd` (if daily>0). Increments a hot-streak; only emits an Alert when the returned streak ≥ 2. (Caller persists the streak — Task 2.)
- `now` is injected (never call `datetime.now()` inside the pure fn — tests pin it).

**Steps (TDD):**
- [ ] Failing tests: seed `fleet_db` for each condition and assert the exact `kind` set. E.g. silent_death: `UPDATE fleet_config SET paused=FALSE,ats_paused=FALSE`; insert a `worker_heartbeat` row with `last_beat = now-40min` → `silent_death` in kinds; then `last_beat=now-5min` → not. stalled_queue: seed one `queued`+approved row, no recent `applied` → alert; add an `applied` row `updated_at=now-1h` → clears. selfheal_dead: no watchdog beat → alert. running_hot: `cost_cap_daily_usd=100` + `llm_usage` rows summing $96 in-window, call twice → first returns streak 1 no alert, second streak 2 → alert. all-healthy → `([], 0)`.
- [ ] Run → FAIL (module missing). Implement `deadman.py`. Run → PASS.
- [ ] Commit: `git add src/applypilot/fleet/deadman.py tests/test_deadman_check.py && git commit -m "feat(deadman): pure fleet dead-man detector (silent-death/stall/selfheal-dead/running-hot)"`

---

### Task 2: DeadMan persistence + delivery + entrypoint

**Files:**
- Modify: `src/applypilot/fleet/schema.py` (`ensure_schema_v3` at :11 — add the additive columns)
- Modify: `src/applypilot/fleet/deadman.py` (add `run_deadman(...)` + `main()`)
- Modify: `pyproject.toml` (console-script `applypilot-fleet-deadman = "applypilot.fleet.deadman:main"`)
- Test: `tests/test_deadman_run.py` (`fleet_db` + `tmp_path`)

**Interfaces:**
- Schema: `ALTER TABLE fleet_config ADD COLUMN IF NOT EXISTS deadman_alert TEXT` and
  `... deadman_alert_at TIMESTAMPTZ` and `... deadman_hot_streak INTEGER NOT NULL DEFAULT 0` (idempotent, in `ensure_schema_v3`).
- `run_deadman(conn, *, now, alert_dir: Path) -> list[Alert]`: reads `deadman_hot_streak`, calls `deadman_check(conn, now=now, prev_hot_streak=streak)`, persists the new streak. If alerts: write a `|`-joined summary to `fleet_config.deadman_alert` + `deadman_alert_at=now`, AND write `alert_dir/fleet-ALERT.txt` with the timestamped detail, AND best-effort Windows toast (try `powershell ... BurntToast`; wrap in try/except → never raise). If NO alerts: clear `deadman_alert=NULL, deadman_alert_at=NULL` and remove `fleet-ALERT.txt` if present. Returns the alerts.
- `main(argv=None)`: `--dsn` (env `FLEET_PG_DSN` default), `--alert-dir` (default `%LOCALAPPDATA%\ApplyPilot`), `--once` (default; the scheduled task calls it once per 20-min trigger). Connects, `ensure_schema_v3`, `run_deadman(now=datetime.now(utc))`, prints the alert summary, exit 0 (a monitoring tool must not error-spam Task Scheduler even when it alerts).

**Steps (TDD):**
- [ ] Failing tests: seed a silent_death condition → `run_deadman` sets `fleet_config.deadman_alert` non-NULL + writes `tmp_path/fleet-ALERT.txt`; then heal the condition + re-run → alert cleared + file removed. running_hot twice → streak persists 1 then 2, file appears on the 2nd. Toast is monkeypatched/absent (assert no raise).
- [ ] Run → FAIL. Implement schema cols + `run_deadman` + `main`. Run → PASS.
- [ ] Commit: `git add src/applypilot/fleet/schema.py src/applypilot/fleet/deadman.py pyproject.toml tests/test_deadman_run.py && git commit -m "feat(deadman): persist+deliver alerts (fleet_config flag + ALERT file + toast) + entrypoint"`

---

### Task 3: Guarded self-resume (`apply-home resume-if-safe`)

**Files:**
- Modify: `src/applypilot/fleet/apply_home_main.py` (new subparser + dispatch branch near :106-123)
- Test: `tests/test_apply_home_resume_if_safe.py` (`fleet_db`)

**Interfaces:**
- New subcommand `resume-if-safe`: calls a new function `resume_if_safe(conn) -> bool` that:
  - returns False (no-op) if `queue._cost_cap_exceeded(conn)` is True;
  - else `UPDATE fleet_config SET paused=FALSE, updated_at=now() WHERE id=1 AND paused=TRUE AND ats_paused=FALSE` and returns `rowcount>0`.
  - NEVER touches `ats_paused`/`ats_pause_source`/LinkedIn. The `AND ats_paused=FALSE` guard is mandatory (a Doctor safety pause is never overridden).
- Prints `resumed` / `left-paused (cap exceeded)` / `left-paused (ats_paused or already running)`.

**Steps (TDD):**
- [ ] Failing tests: (a) `paused=TRUE, ats_paused=FALSE`, no cap exceeded → resume_if_safe True + `paused=FALSE`. (b) `paused=TRUE, ats_paused=TRUE` (Doctor pause) → returns False, `paused` UNCHANGED, `ats_paused` UNCHANGED (the catastrophe guard). (c) cap exceeded (`cost_cap_daily_usd=1` + `llm_usage` $5 in-window) → returns False, still paused. (d) already `paused=FALSE` → no-op False.
- [ ] Run → FAIL. Implement. Run → PASS. Also run `tests/test_fleet_apply_home.py` to confirm no regression.
- [ ] Commit: `git add src/applypilot/fleet/apply_home_main.py tests/test_apply_home_resume_if_safe.py && git commit -m "feat(apply-home): resume-if-safe (guarded plain-pause clear; never overrides ats_paused)"`

---

### Task 4: Console red banner for DeadMan alert

**Files:**
- Modify: `src/applypilot/fleet/console_app.py` (the status read ~:391 + `build_status` ~:457 add `deadman_alert`; `_INDEX_HTML` renders a red banner when set)
- Test: `tests/test_console_deadman_banner.py` (`fleet_db` + the live-server pattern from `test_console_challenges_api.py`)

**Interfaces:**
- `build_status()` / the status JSON gains `"deadman_alert": <str|null>` and `"deadman_alert_at": <iso|null>` read from `fleet_config` (extend the existing `SELECT ... ats_pause_source` at :391).
- `_INDEX_HTML`: a fixed top banner (reuse the `.tokbanner` style pattern) shown when `deadman_alert` is non-null: red, text = the alert summary + relative age. Hidden when null. GET-only (no token needed — it's a status field).

**Steps (TDD):**
- [ ] Failing test: seed `fleet_config.deadman_alert='silent_death: ...'` → `GET /api/status` JSON contains `deadman_alert`; the HTML contains a banner element id (e.g. `deadmanBanner`) + the fetch wiring. Null alert → JSON null.
- [ ] Run → FAIL. Implement. Run → PASS; re-run `test_console_token.py test_console_challenges_api.py` (no regression to Build-11).
- [ ] Commit: `git add src/applypilot/fleet/console_app.py tests/test_console_deadman_banner.py && git commit -m "feat(console): red DeadMan banner from fleet_config.deadman_alert"`

---

### Task 5: `register-apply-cycle.ps1` (VerifyLive / ApplyCycle / DeadMan tasks)

**Files:**
- Create: `register-apply-cycle.ps1` (repo root, next to `register-fleet-tasks.ps1` — but a SEPARATE file)

**Interfaces:**
- Mirrors `register-fleet-tasks.ps1`'s idempotent unregister-then-register + generated-wrapper pattern (READ it first for the helper shape; do NOT edit it). Home-only. Registers three tasks:
  - `ApplyPilot ApplyCycle` — trigger every 4h; wrapper runs, via `run-applypilot.ps1` for the brain-touching first step and the fleet exe for the rest:
    `run-applypilot.ps1 verify-live` (skip if VerifyLive task covers it — keep in ApplyCycle too for a fresh pre-push check, `--limit` a sane batch) → `applypilot-fleet-apply-home.exe push --score-floor 7` → `... approve --all-pushed` → `... lift-canary` → `... resume-if-safe` → `... pull`. Each step logged; wrapper exits non-zero if any step exits non-zero (Task Scheduler Last-Result visibility).
  - `ApplyPilot VerifyLive` — trigger every 6h; wrapper runs `run-applypilot.ps1 verify-live`.
  - `ApplyPilot DeadMan` — trigger every 20 min (`-Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration (New-TimeSpan -Days 3650)`); wrapper runs `applypilot-fleet-deadman.exe` with `FLEET_PG_DSN` set inline.
- `-Unregister` removes the three. Sets `FLEET_PG_DSN` inline in each wrapper (don't rely on user-profile env), matching register-fleet-tasks.ps1's approach.

**Steps:**
- [ ] Implement `register-apply-cycle.ps1`.
- [ ] PARSE GATE (do NOT register from the sandbox — owner runs it): `powershell -NoProfile -Command "$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('register-apply-cycle.ps1',[ref]$null,[ref]$e)|Out-Null; if($e.Count){$e|%{$_.Message}}else{'PARSE OK'}"` → PARSE OK.
- [ ] Commit: `git add register-apply-cycle.ps1 && git commit -m "feat(autonomy): register-apply-cycle.ps1 (VerifyLive/ApplyCycle/DeadMan home tasks)"`

---

### Task 6: Verification + runbook

**Files:**
- Create: `docs/autonomous-apply-runbook.md`

**Steps:**
- [ ] Full sweep: `.\.conda-env\python.exe -m pytest tests/test_deadman_check.py tests/test_deadman_run.py tests/test_apply_home_resume_if_safe.py tests/test_console_deadman_banner.py tests/test_fleet_apply_home.py tests/test_console_token.py -q` → all pass.
- [ ] Read-only live DeadMan demo (no writes): run `deadman_check` against the live fleet PG via a one-off python snippet (SELECT-only, do NOT call run_deadman which writes) and report which alerts currently fire (the fleet is `paused=True` now, so `silent_death` should NOT fire — armed=false; report the real state).
- [ ] Runbook `docs/autonomous-apply-runbook.md`: (1) `register-apply-cycle.ps1` one-time; (2) what each task does + cadence; (3) the ONLY throttle is the rolling $100/day (`cost_cap_daily_usd`) — how to change it; (4) the DeadMan red banner on the console + `fleet-ALERT.txt` location; (5) the safety guard (never clears a Doctor/LinkedIn halt); (6) how to stop autonomy (`register-apply-cycle.ps1 -Unregister` + pause). Note the current live state: `spend_cap_usd=0` (removed), `cost_cap_daily_usd=100`, fleet currently `paused=True` (ApplyCycle's resume-if-safe will clear it on first run since ats_paused=False + cap not exceeded).
- [ ] Commit: `git add docs/autonomous-apply-runbook.md && git commit -m "docs(autonomy): autonomous-apply runbook + live-state verification"`
