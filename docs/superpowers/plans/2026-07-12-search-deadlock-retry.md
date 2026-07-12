# Search Deadlock Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Tarpon's transient search-deadlock recovery in the canonical fleet branch and deploy one tested implementation to reachable workers.

**Architecture:** A private retry helper owns rollback and bounded backoff for PostgreSQL deadlocks. Existing search operations retain ownership of SQL, commit behavior, and return values.

**Tech Stack:** Python 3.11+, psycopg 3, pytest, PostgreSQL, PowerShell/Tailscale SSH.

---

### Task 1: Regression Tests

**Files:**
- Modify: `tests/test_fleet_v3_governor_queue.py`

- [ ] Add tests that inject `DeadlockDetected`, verify rollback and retry, and verify exhaustion re-raises.
- [ ] Run the focused tests and confirm they fail because the retry helper does not exist.

### Task 2: Retry Implementation

**Files:**
- Modify: `src/applypilot/fleet/queue.py`

- [ ] Import `time` and `DeadlockDetected`.
- [ ] Add a four-attempt private retry helper with rollback and exponential backoff.
- [ ] Route `lease_search` and `complete_search` through the helper without changing their SQL or results.
- [ ] Run focused scheduler/governor tests and confirm they pass.

### Task 3: Verification and Rollout

**Files:**
- Modify: none

- [ ] Run the complete runtime test suite with the integration worktree on `PYTHONPATH`.
- [ ] Commit and push the exact changed files.
- [ ] Reconcile GGGTower and Tarpon to the canonical branch.
- [ ] Verify fresh heartbeat versions while fleet pauses and policy activation remain unchanged.
