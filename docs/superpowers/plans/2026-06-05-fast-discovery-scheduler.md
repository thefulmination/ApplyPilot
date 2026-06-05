# Fast Discovery Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce ApplyPilot discovery wall-clock time without increasing IP-ban risk.

**Architecture:** Add a source-level scheduler around existing discovery modules. JobSpy remains conservative; public boards, HiringCafe, Corporate ATS, Workday, and SmartExtract run as independent source tasks with configurable parallelism and fast-mode source filtering. Existing per-source workers remain source-owned.

**Tech Stack:** Python 3.11, ThreadPoolExecutor, Typer CLI, existing searches.yaml config, pytest.

---

### Task 1: Source Scheduler Unit Tests

**Files:**
- Test: `tests/test_discovery_scheduler.py`
- Modify: `src/applypilot/pipeline.py`

- [ ] Write failing tests for source task selection, fast mode skips, parallel execution, and result capture.
- [ ] Run `python -m pytest tests/test_discovery_scheduler.py` and confirm failures.

### Task 2: Scheduler Implementation

**Files:**
- Modify: `src/applypilot/pipeline.py`
- Modify: `src/applypilot/cli.py`
- Modify: `.applypilot/searches.yaml`
- Modify: `src/applypilot/config/searches.example.yaml`

- [ ] Add `_discover_mode_config`, `_discover_source_tasks`, `_run_discover_task`, and `_run_discover` parallel orchestration.
- [ ] Add `--discover-mode` CLI option with `safe`, `fast`, and `full` modes.
- [ ] Configure safe defaults: source parallelism 3, JobSpy serial, Corporate ATS 8, Workday 4, HiringCafe disabled in fast mode unless explicitly enabled.

### Task 3: Verification

**Files:**
- Test: all touched tests and source files.

- [ ] Run focused scheduler tests.
- [ ] Run full pytest.
- [ ] Run ruff on touched files.
- [ ] Run compileall and pip check.
- [ ] Run `run discover --dry-run` or a narrow CLI check to confirm option plumbing.
