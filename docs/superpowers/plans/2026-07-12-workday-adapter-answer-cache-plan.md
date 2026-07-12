# Workday Adapter And Answer Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete deterministic Workday field mapping and cache approved answers before model fallback.

**Architecture:** Extend the existing canonical resume and Workday field-plan contracts without adding a second browser abstraction. Put cache lookup at the existing approved-answer boundary, preserve host/question isolation, and expose hit/miss counters through existing execution metadata.

**Tech Stack:** Python, SQLite, Playwright adapter contracts, pytest, Ruff.

---

### Task 1: Canonical Workday dynamic fields

**Files:**
- Modify: `src/applypilot/apply/workday_adapter.py`
- Test: `tests/test_workday_field_mapping.py`, `tests/test_workday_resume_corrections.py`

- [x] Add failing tests for indexed work-history and education fields receiving canonical company, title, location, school, degree, and field-of-study values.
- [x] Add a failing test proving missing role description or historical location remains unresolved.
- [x] Pass `canonical_resume` into `build_field_plan` from `WorkdayAdapterRunner`.
- [x] Add deterministic group/index mapping and role-description extraction from explicit resume bullets only.
- [x] Run the focused mapping tests and confirm they pass.

### Task 2: Cache-first approved answers

**Files:**
- Modify: `src/applypilot/apply/answer_exceptions.py`, `src/applypilot/apply/workday_adapter.py`
- Test: `tests/test_answer_exceptions.py`, `tests/test_workday_runner.py`

- [x] Add failing tests for approved host-specific hits, host isolation, pending misses, and normalized question matching.
- [x] Route approved-answer lookup before any model resolver in the Workday field-plan path.
- [x] Record cache hits, misses, and avoided model calls in runner metadata without storing secrets or raw page text.
- [x] Run answer-cache and runner tests and confirm they pass.

### Task 3: Verification and rollout readiness

**Files:**
- Verify: `tests/test_workday_*.py`, answer/cache tests, changed production modules.
- Runtime: local ApplyPilot SQLite database and Workday prepare command.

- [x] Run the complete Workday and answer/cache suites (169 passed).
- [x] Run Ruff and Python compilation on changed modules.
- [x] Run fresh non-submitting Workday prepare batches and inspect review-ready count and cache metrics.
- [x] Do not launch a submission canary unless five unique fresh jobs are review-ready and all safety gates pass.

**Current rollout status:** Submission remains gated. The latest fresh batches do not
contain five unique review-ready jobs; Visa parks on an unavailable controlled field
(`Quantitative Finance`), and unsupported HiringCafe tenants are excluded before
browser launch.
