# CapSolver Fleet Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every apply-capable fleet machine prove CapSolver readiness before it launches workers that can encounter CAPTCHA walls.

**Architecture:** Keep the existing `applypilot capsolver-check` account probe, and add a fleet readiness layer that combines the account check with a static prompt capability check for the unsupported-CapSolver fast-fail behavior. PowerShell launchers call this readiness command before starting apply workers, while setup scripts persist the key so worker processes inherit it.

**Tech Stack:** Python Typer CLI, existing `applypilot.apply.capsolver` helper, PowerShell fleet launch/setup scripts, pytest script-content tests.

---

### Task 1: Add Fleet Readiness Tests

**Files:**
- Modify: `tests/test_capsolver_health.py`
- Create: `tests/test_fleet_capsolver_scripts.py`

- [x] **Step 1: Write failing Python readiness tests**

Add tests that import `applypilot.apply.capsolver`, monkeypatch `check_balance`, and assert a new `check_fleet_readiness()` returns `ready=True` only when the CapSolver account is reachable and the prompt includes the unsupported-CapSolver fast-fail instructions.

- [x] **Step 2: Write failing script integration tests**

Add tests asserting:
- `run-fleet-worker.ps1` calls `fleet-capsolver-check --json` and refuses to start when it fails.
- `fleet-agent.ps1` includes CapSolver readiness in startup preflight.
- `setup-fleet-worker.ps1` and `setup-fleet-machine.ps1` persist `CAPSOLVER_API_KEY`.

- [x] **Step 3: Run focused tests and verify they fail for missing feature**

Run: `.\.conda-env\python.exe -m pytest tests\test_capsolver_health.py tests\test_fleet_capsolver_scripts.py -q`

Expected: FAIL because the readiness API and script hooks do not exist yet.

### Task 2: Implement Readiness API and CLI

**Files:**
- Modify: `src/applypilot/apply/capsolver.py`
- Modify: `src/applypilot/cli.py`

- [x] **Step 1: Add `CapSolverFleetReadiness`**

Create a dataclass that carries `ready`, `account`, `prompt_fast_fail`, `balance`, `error_code`, `error_description`, and `note`, with `to_dict()`.

- [x] **Step 2: Add `check_fleet_readiness()`**

Call `check_balance()`, inspect `_build_captcha_section()` for `ERROR_INVALID_TASK_DATA`, `ERROR_TASK_NOT_SUPPORTED`, and `RESULT:CAPTCHA`, and return `ready=True` only when both checks pass.

- [x] **Step 3: Add `applypilot fleet-capsolver-check --json`**

Print one-line JSON for machine consumption. Exit `1` if `ready` is false.

### Task 3: Wire Fleet Scripts

**Files:**
- Modify: `run-fleet-worker.ps1`
- Modify: `fleet-agent.ps1`
- Modify: `setup-fleet-worker.ps1`
- Modify: `setup-fleet-machine.ps1`

- [x] **Step 1: Add fail-closed worker preflight**

Derive `applypilot.exe` from the same virtualenv scripts directory as `applypilot-fleet-apply.exe`; run `fleet-capsolver-check --json`; throw if it exits non-zero.

- [x] **Step 2: Add fleet-agent report-only preflight**

Find `applypilot.exe` and add a startup preflight problem when `fleet-capsolver-check --json` fails. Leave reconciliation behavior unchanged because `run-fleet-worker.ps1` is the fail-closed guard.

- [x] **Step 3: Persist CapSolver key in setup scripts**

Prompt for an optional CapSolver API key, set it in the User environment, current process, and `InstallDir\.applypilot\.env` without printing it later.

### Task 4: Verify and Deploy

- [x] **Step 1: Run focused tests**

Run: `.\.conda-env\python.exe -m pytest tests\test_capsolver_health.py tests\test_fleet_capsolver_scripts.py -q`

- [ ] **Step 2: Run related regression tests**

Run: `.\.conda-env\python.exe -m pytest tests\test_capsolver_health.py tests\test_apply_prompt.py tests\test_fleet_worker_launcher_identity.py tests\test_fleet_tailscale_defaults.py tests\test_setup_fleet_pg_tailscale.py -q`

- [ ] **Step 3: Verify local CapSolver readiness**

Run: `.\.conda-env\Scripts\applypilot.exe fleet-capsolver-check --json`

- [ ] **Step 4: Deploy to reachable fleet machines**

Bundle the local commit, fetch it on each reachable worker checkout, restart the FleetAgent scheduled task, and verify `fleet-capsolver-check --json` on each machine.
