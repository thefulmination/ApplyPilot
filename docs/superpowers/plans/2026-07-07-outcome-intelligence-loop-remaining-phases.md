# Outcome Intelligence Loop Remaining Phases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the outcome-intelligence rollout after the Phase 1 trust backbone by adding operator review surfaces, alert generation/digest delivery, and trusted learning exports.

**Architecture:** Reuse the Phase 1 review/effective-state layer as the single source for downstream consumers. Split the remaining work into three focused additions: `outcome_operator.py` for action/review payloads, `outcome_alerts.py` for alert/digest logic, and `outcome_learning.py` for trusted-only reports and exports. Extend the existing outcomes HTTP server and CLI instead of creating a parallel stack.

**Tech Stack:** Python 3.11, Typer, Rich, sqlite3, stdlib HTTP server, pytest.

---

## File Structure

- `src/applypilot/outcome_review.py`
  Extend effective-state logic with high-value/actioned/trusted-only helpers.
- `src/applypilot/outcome_operator.py`
  New pure assemblers for inbox review, action queue, and operator summary payloads.
- `src/applypilot/outcome_alerts.py`
  New pure alert builder plus digest artifact writer.
- `src/applypilot/outcome_learning.py`
  New trusted-only learning/report export functions.
- `src/applypilot/outcome_dashboard.py`
  Extend API and HTML to expose operator views and review actions.
- `src/applypilot/cli.py`
  Add `outcomes-operator`, `outcomes-alerts digest`, and `outcomes-learn export`.
- `tests/test_outcome_operator.py`
  New operator payload tests.
- `tests/test_outcome_alerts.py`
  New alert/digest tests.
- `tests/test_outcome_learning.py`
  New trusted-learning export tests.
- `tests/test_outcome_dashboard.py`
  Extend with operator API + review POST tests.
- `tests/test_outcomes_alerts_cli.py`
  New CLI tests for digest generation.
- `tests/test_outcomes_learning_cli.py`
  New CLI tests for trusted-learning export.

---

### Task 1: Extend effective-state helpers for operator and trusted-only use

**Files:**
- Modify: `src/applypilot/outcome_review.py`
- Test: `tests/test_outcome_operator.py`

- [ ] Write failing tests for:
  - high-value trusted events that still need operator action
  - `mark_actioned` review rows clearing that action requirement
  - trusted-only filtering excluding `needs_review` rows from analytics
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement the minimal helper functions and flags in `outcome_review.py`.
- [ ] Re-run targeted pytest and confirm pass.

### Task 2: Add operator payload assembly

**Files:**
- Create: `src/applypilot/outcome_operator.py`
- Test: `tests/test_outcome_operator.py`

- [ ] Write failing tests for:
  - inbox review payload includes `needs_review` and unmatched recruiting rows
  - action queue includes trusted high-value unactioned rows
  - application timeline summary uses trusted rows for stage/outcome but still exposes review counts
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement the pure operator payload assemblers.
- [ ] Re-run targeted pytest and confirm pass.

### Task 3: Extend outcomes HTTP server into an operator surface

**Files:**
- Modify: `src/applypilot/outcome_dashboard.py`
- Test: `tests/test_outcome_dashboard.py`

- [ ] Write failing tests for:
  - `/api/data` includes review/action queue payloads
  - POST `/api/review` writes a review resolution
  - the rendered page contains operator sections for review queue and action queue
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement the minimal HTTP/API/HTML changes.
- [ ] Re-run targeted pytest and confirm pass.

### Task 4: Add alert building and digest artifact output

**Files:**
- Create: `src/applypilot/outcome_alerts.py`
- Test: `tests/test_outcome_alerts.py`
- Test: `tests/test_outcomes_alerts_cli.py`

- [ ] Write failing tests for:
  - trusted offer/interview/screen rows produce critical alerts
  - high-value `needs_review` rows produce warning alerts
  - repeated thread updates collapse into one active alert
  - digest artifact writes text/json summaries with counts and items
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement pure alert builders and the digest writer, then wire `outcomes-alerts digest` in `cli.py`.
- [ ] Re-run targeted pytest and confirm pass.

### Task 5: Add trusted learning/report exports

**Files:**
- Create: `src/applypilot/outcome_learning.py`
- Modify: `src/applypilot/cli.py`
- Test: `tests/test_outcome_learning.py`
- Test: `tests/test_outcomes_learning_cli.py`

- [ ] Write failing tests for:
  - learning exports exclude `needs_review` rows from response/positive metrics
  - export bundle contains trusted timelines, lane report, latency report, score-band report, and recommendations
  - CLI `outcomes-learn export` writes the bundle and prints summary counts
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement the minimal trusted-learning export path and CLI.
- [ ] Re-run targeted pytest and confirm pass.

### Task 6: Full slice verification

**Files:**
- Test: `tests/test_outcome_review.py`
- Test: `tests/test_outcome_operator.py`
- Test: `tests/test_outcome_dashboard.py`
- Test: `tests/test_outcome_alerts.py`
- Test: `tests/test_outcomes_alerts_cli.py`
- Test: `tests/test_outcome_learning.py`
- Test: `tests/test_outcomes_learning_cli.py`
- Test: `tests/test_outcome_export.py`
- Test: `tests/test_outcomes_cli.py`
- Test: `tests/test_outcomes_integration_cli.py`

- [ ] Run the combined outcome suite and confirm it passes.
- [ ] Review the current diff against the design spec and note any remaining intentional exclusions.

---

## Self-Review

- Spec coverage:
  This plan covers the remaining spec requirements after Phase 1: operator review UI, action queue, critical/warning/digest alerts, and trusted learning exports.
- Placeholder scan:
  No TODO/TBD placeholders remain.
- Type consistency:
  The plan consistently treats Phase 1 review state as authoritative for operator, alert, and learning consumers.
