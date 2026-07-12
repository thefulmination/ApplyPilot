# P1 Fail-Closed Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OTP overload, monitor infrastructure failure, and fleet-lock uncertainty fail closed and observable.

**Architecture:** Preserve existing safety bounds and advisory locks while changing ambiguous success results into typed or tri-state outcomes. Keep operational failure details privacy-safe and allow non-LinkedIn work to continue during fleet coordination outages.

**Tech Stack:** Python 3.12, psycopg, pytest, PowerShell startup scripts.

---

### Task 1: Make OTP responder overload observable

**Files:**
- Modify: `src/applypilot/fleet/otp_relay.py`
- Modify: `src/applypilot/fleet/otp_responder_main.py`
- Test: `tests/test_otp_relay_responder.py`
- Test: `tests/test_otp_responder_main.py`

- [ ] Add failing tests asserting request and mail candidate overflow raise a typed responder overload error.
- [ ] Add failing tests asserting a failed cycle writes an error heartbeat, omits the idle heartbeat, and makes `--once` return nonzero.
- [ ] Implement the typed overload exception and responder failure heartbeat behavior.
- [ ] Run `python -m pytest tests/test_otp_relay_responder.py tests/test_otp_responder_main.py -q` and require all tests to pass.

### Task 2: Make dead-man infrastructure failure visible

**Files:**
- Modify: `src/applypilot/fleet/deadman.py`
- Test: `tests/test_deadman_cli.py`

- [ ] Add failing tests asserting database/check failure returns nonzero and writes a generic local fallback alert.
- [ ] Implement privacy-safe fallback alert writing and nonzero failure status.
- [ ] Run `python -m pytest tests/test_deadman_cli.py tests/test_deadman_run.py tests/test_deadman_check.py -q` and require all tests to pass.

### Task 3: Fail closed on an unknown LinkedIn fleet lock

**Files:**
- Modify: `src/applypilot/apply/launcher.py`
- Test: `tests/test_fleet_linkedin_lane.py`

- [ ] Add failing tests for active, inactive, unknown, and absent-DSN probe outcomes.
- [ ] Implement a three-state probe and make acquisition exclude LinkedIn for active or unknown results.
- [ ] Run `python -m pytest tests/test_fleet_linkedin_lane.py tests/test_apply_supervisor.py tests/test_launcher_process_guard.py -q` and require all tests to pass.

### Task 4: Combined verification

**Files:**
- Verify all files modified above.

- [ ] Run the focused OTP, dead-man, launcher, supervisor, and browser lifecycle test matrix.
- [ ] Run `python -m compileall -q src`.
- [ ] Parse `run-otp-responder.ps1` and `register-otp-responder-startup.ps1` with the PowerShell parser.
- [ ] Run `git diff --check` and inspect the final scoped diff.
- [ ] Confirm the live responder process count and lifecycle-fault file count without reading message content or credentials.
