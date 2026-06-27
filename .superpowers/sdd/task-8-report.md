# Task 8 Report: End-to-End Test + Full Suite (Frontier Quality Lane)

## TDD RED/GREEN

### Step 1 – Write tests

Created `tests/test_frontier_e2e.py` with:
1. Verbatim e2e test from the brief (`test_frontier_pass_end_to_end_advisory_and_report`)
2. Governor-deny coverage test (`test_governor_deny_fails_over_to_metered`)

Also edited `tests/test_frontier_main.py` to tighten the guard assertion with `match="enable-subscription"`.

### Step 2 – First run (immediate GREEN — no iteration needed)

```
.conda-env/python.exe -m pytest tests/test_frontier_e2e.py -v
```

```
tests/test_frontier_e2e.py::test_frontier_pass_end_to_end_advisory_and_report PASSED
tests/test_frontier_e2e.py::test_governor_deny_fails_over_to_metered PASSED
2 passed in 0.21s
```

No wiring bugs found in src/ — all pieces from Tasks 1-7 connected correctly.

### Step 3 – All frontier tests green

```
.conda-env/python.exe -m pytest tests/test_frontier_*.py -v
```

```
tests/test_frontier_db.py::test_upsert_and_disagreement_report PASSED
tests/test_frontier_e2e.py::test_frontier_pass_end_to_end_advisory_and_report PASSED
tests/test_frontier_e2e.py::test_governor_deny_fails_over_to_metered PASSED
tests/test_frontier_governor.py::test_allow_min_gap_and_limit_trip PASSED
tests/test_frontier_governor.py::test_window_budget_optional_bound PASSED
tests/test_frontier_main.py::test_subscription_requires_explicit_enable PASSED
tests/test_frontier_pass.py::test_subscription_path_writes_advisory_and_picks_top_model PASSED
tests/test_frontier_pass.py::test_failover_to_metered_on_subscription_unavailable PASSED
tests/test_frontier_select.py::test_backlog_orders_by_cheap_score_respects_floor_and_exclusions PASSED
9 passed in 0.33s
```

## Full Suite Run

```
.conda-env/python.exe -m pytest tests/test_frontier_*.py tests/test_cli_providers.py \
  tests/test_build_score_prompt_text.py tests/test_fleet_v3_*.py \
  tests/test_fleet_compute_*.py tests/test_fleet_pgqueue.py -q
```

**Result: 136 passed, 1 skipped in 34.38s** (no regressions)

## Files Changed

- **CREATED**: `tests/test_frontier_e2e.py` — e2e test (verbatim from brief) + governor-deny test
- **MODIFIED**: `tests/test_frontier_main.py` — tightened `pytest.raises(SystemExit, match="enable-subscription")`

No `src/` files modified.

## Commit

SHA: `2c0bdbe`
Subject: `test(fleet): frontier pass end-to-end advisory + disagreement report`
Files: 2 changed, 71 insertions(+), 1 deletion(-)

## Self-Review

**Advisory-only asserted?** Yes — after `run_frontier_pass`, the e2e test checks that `jobs.fit_score` for 'disagree' (frontier_score=3 vs cheap=9) is still 9. The `frontier_scores` table gets the advisory score; the `jobs` table is never touched by the pass.

**Disagreement report correct?** Yes — 'agree' job (cheap=8, frontier=8, agreement=1.0) is above threshold and NOT in the report. 'disagree' job (cheap=9, frontier=3, agreement=0.333) is below 0.8 and IS in the report. The monkeypatch distinguishes jobs by "PM" in the prompt, which appears via `TITLE: PM` in `build_score_prompt_text`.

**Governor-deny path covered?** Yes — `_DenyGov.allow()` returns False unconditionally; `score_via_codex` is patched to raise `AssertionError` if called; assertions: `failed_over==1`, `by_subscription==0`, upserted provider is "gpt-5.5" (metered).

**Guard match pinned?** Yes — `pytest.raises(SystemExit, match="enable-subscription")` now pins the guard's specific exit message from `frontier_main.py` line 56, not just any `SystemExit`.

## Concerns

None. All 136 tests pass with no `src/` modifications. The deferred Opus cross-check (`score_via_claude`, `--cross-check-opus`) remains a clean additive follow-up per the brief's self-review.

---

# Task 8 (Part 2) — Whole-Branch Review Findings Applied

## Implemented vs Documented

### Finding #1 (IMPLEMENTED) — Metered LLM error pollutes disagreement report
- **File:** `src/applypilot/fleet/frontier_pass.py`
- **Change:** After receiving `result` from either backend, if `result.get("error")` is truthy OR `result.get("score") is None`, `fscore` is set to `None`. The `_agreement` helper already returns `None` for a `None` score, so the upsert writes `frontier_score=NULL, agreement=NULL`. `NULL < 0.8` is false in SQLite, so errored jobs self-exclude from `disagreement_report` while the attempt row is still recorded.
- **Also:** Added comment at `gov.record("limit")` noting the deliberate conservative cooling choice.
- **Test (GREEN):** `test_metered_llm_error_writes_null_scores_and_not_in_disagreement_report`

### Finding #7 (IMPLEMENTED) — Empty urls crashes the selector
- **File:** `src/applypilot/fleet/frontier_select.py`
- **Change:** Added `if mode == "urls" and not urls: return []` guard before `IN ()` SQL is built.
- **Test (GREEN):** `test_urls_mode_empty_list_returns_empty`

### Finding #2 (IMPLEMENTED) — codex-not-installed gives silent failover forever
- **File:** `src/applypilot/fleet/cli_providers.py`
- **Change:** Separated `FileNotFoundError` from the catch-all `except Exception`. Now raises `SubscriptionUnavailable("codex CLI not found on PATH -- is Codex installed/logged in? Falling back to metered.")`.
- **Test (GREEN):** `test_score_via_codex_file_not_found_raises_clear_message`

### Findings #3/#4/#5/#6 (DOCUMENTED only)
- **File:** `.superpowers/sdd/frontier-lane-followups.md` (created)
- Follow-up items: configurable timeout/retries, per-model provider string in `frontier_scores`, CSV/JSON report format, Opus cross-check + `frontier_decision` reserved/deferred.

## Covering-Test Command + Output

```
.conda-env/python.exe -m pytest tests/test_frontier_pass.py tests/test_frontier_select.py tests/test_cli_providers.py -q
.........
9 passed in 0.28s
```

3 pre-existing tests GREEN (unchanged). 3 new tests GREEN.

## Files Modified (this part)
- `src/applypilot/fleet/frontier_pass.py`
- `src/applypilot/fleet/frontier_select.py`
- `src/applypilot/fleet/cli_providers.py`
- `tests/test_frontier_pass.py`
- `tests/test_frontier_select.py`
- `tests/test_cli_providers.py`
- `.superpowers/sdd/frontier-lane-followups.md` (new)
- `.superpowers/sdd/task-8-report.md` (appended)
