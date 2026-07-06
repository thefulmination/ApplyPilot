# Fleet Machine Blackout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add master fleet control that can block all ApplyPilot fleet work on selected machine labels until an expiration time while allowing home and Mac machines.

**Architecture:** Add an explicit Postgres-backed `fleet_machine_blackout` policy table and a small Python read/write module. PowerShell launchers and the apply fleet-agent call a shared query helper before starting or reconciling local workers, so apply, discovery, and compute lanes all honor the same policy.

**Tech Stack:** Python 3.11, psycopg/Postgres, argparse CLI entry point, PowerShell launcher guards, pytest script-content and disposable Postgres tests.

---

### Task 1: Policy Storage And Read Model

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Create: `src/applypilot/fleet/machine_blackout.py`
- Test: `tests/test_fleet_machine_blackout.py`

- [ ] Add `fleet_machine_blackout` with active/expiration fields and allow/block pattern arrays.
- [ ] Implement `is_machine_allowed`, `create_blackout`, `clear_blackouts`, and `active_blackouts`.
- [ ] Test that `home` and `mac-*` are allowed while `m2`/`m4` are blocked until expiry.

### Task 2: CLI And Query Helper

**Files:**
- Modify: `pyproject.toml`
- Create: `src/applypilot/fleet/machine_blackout_main.py`
- Create: `fleet-blackout-query.py`
- Test: `tests/test_fleet_machine_blackout.py`

- [ ] Add `applypilot-fleet-control` entry point.
- [ ] Add `blackout`, `status`, and `clear` commands.
- [ ] Add a PowerShell-friendly query helper that prints `OK|...`, `BLOCKED|...`, or `KEEP|...`.

### Task 3: Launcher Guards

**Files:**
- Modify: `fleet-agent.ps1`
- Modify: `run-fleet-compute.ps1`
- Modify: `run-fleet-discovery.ps1`
- Test: `tests/test_fleet_machine_blackout_scripts.py`

- [ ] `fleet-agent.ps1` treats blocked labels as effective desired workers `0`.
- [ ] Compute and discovery launchers refuse to start when their label is blocked.
- [ ] Tests assert each lane invokes `fleet-blackout-query.py`.

### Task 4: Health Visibility And Verification

**Files:**
- Modify: `fleet-health.ps1`
- Test: `tests/test_fleet_health_script.py`

- [ ] Include active machine blackout policies in fleet health output.
- [ ] Run focused tests for the policy module, scripts, and health check.
