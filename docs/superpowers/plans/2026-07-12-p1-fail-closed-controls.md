# P1 Fail-Closed Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OTP overload, monitor infrastructure failure, and fleet-lock uncertainty fail closed and observable.

**Architecture:** Preserve existing safety bounds and advisory locks while changing ambiguous success results into typed or tri-state outcomes. Keep failure details privacy-safe and allow non-LinkedIn work to continue during fleet coordination outages.

**Tech Stack:** Python 3.12, psycopg, pytest, PowerShell.

---

### Task 1: OTP overload

- [x] Add request and candidate overflow regression tests.
- [x] Raise a typed overload error.
- [x] Record responder failure heartbeat and return nonzero in one-shot mode.

### Task 2: Dead-man failure

- [x] Add sanitized fallback alert and failure-status regression coverage.
- [x] Return nonzero without exposing exception contents.

### Task 3: LinkedIn interlock uncertainty

- [x] Add unknown-state regression coverage.
- [x] Treat configured unknown state as a LinkedIn acquisition exclusion.

### Task 4: Verification

- [x] Run focused and combined test matrices.
- [x] Run Ruff, compileall, PowerShell parsing, and `git diff --check`.
