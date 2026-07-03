# Task 8 Report: Inert-Invariant Safety Test (Outcomes Integration)

## Status: DONE_WITH_CONCERNS

## What was implemented

Created `tests/test_outcomes_inert_invariant.py` verbatim from the brief (2 tests):
- `test_pure_modules_have_no_write_paths`: inspects raw source of `outcome_implied` and `outcome_lane_signal` for `record_application`, `INSERT`, `UPDATE `, and `conn.commit`.
- `test_outcomes_promote_is_preview_only_in_source`: inspects source of `cli.outcomes_promote_command` for `--apply`, `record_application`, `INSERT`, and `UPDATE `.

## Test Result: 1 FAILED, 1 PASSED

`test_outcomes_promote_is_preview_only_in_source` — PASSED (clean).

`test_pure_modules_have_no_write_paths` — FAILED:

```
AssertionError: 'record_application' is contained in source of outcome_implied
```

### Root cause: docstring false positive

`src/applypilot/outcome_implied.py` line 4 contains `record_application` inside its module-level docstring as a deliberate disclaimer:

```python
"""Pure mapping from a per-application outcome row to the tracker status it WOULD
imply -- WITHOUT writing anything. INERT by construction: no DB, no I/O, no
mutation. A future activation could route these decisions through
applications.record_application; that is deliberately NOT here."""
```

This is a **docstring-only mention** (not a call, import, or write path). The module IS genuinely inert. The verbatim test from the brief triggers a false positive because `inspect.getsource()` returns the full source including docstrings.

### Confirmed: no actual write paths in either module

- `outcome_implied.py`: no `INSERT`, no `UPDATE`, no `conn.commit`, no actual `record_application` call. Only the docstring contains the word.
- `outcome_lane_signal.py`: no `record_application`, `INSERT`, `UPDATE`, or `conn.commit` anywhere (0 matches).
- `cli.outcomes_promote_command`: test 2 PASSES — no `--apply`, no writes.

## Full new-suite result

Not run per brief instruction ("STOP and report as DONE_WITH_CONCERNS" on safety test failure).

## Commit

Staged and committed `tests/test_outcomes_inert_invariant.py` as written (the file documents the invariant; the test currently fails on the docstring FP).

## Remediation options (not applied — require sign-off per brief STOP instruction)

1. **Edit the docstring** in `src/applypilot/outcome_implied.py` to remove or rephrase the `record_application` mention. Takes <1 minute.
2. **Strengthen the test** to strip docstrings before asserting (e.g., use `ast.get_docstring` or `textwrap` to remove triple-quoted strings). More robust long-term.

Either fix is trivial but requires approval given the brief's explicit STOP instruction on failure.

## Self-Review

- Inert invariant HOLDS in the implementation — zero actual write paths.
- False positive is from verbatim string scan on docstring text.
- The safety property itself is correctly enforced; only the test needs a minor adjustment.

---

# Task 8 Report: applypilot-fleet-linkedin-home driver + push/approve/resolve LinkedIn helpers

## Status: GREEN

## RED phase
Added two verbatim tests from the brief to `tests/test_fleet_linkedin_home.py`. Ran:
```
.conda-env/python.exe -m pytest tests/test_fleet_linkedin_home.py -q
```
Result:
```
FAILED tests/test_fleet_linkedin_home.py::test_linkedin_approve_gated_by_canary
  ImportError: cannot import name 'linkedin_home_main' from 'applypilot.fleet'
FAILED tests/test_fleet_linkedin_home.py::test_push_linkedin_jobs_dedup_key
  AttributeError: module 'applypilot.fleet.queue' has no attribute 'push_linkedin_jobs'
2 failed in 6.32s
```

## GREEN phase

Implemented all required code. Ran:
```
.conda-env/python.exe -m pytest tests/test_fleet_linkedin_home.py -q
```
Result: `2 passed in 10.23s`

Import check: `python -c "import applypilot.fleet.linkedin_home_main"` → `import OK`

Regression check (`apply_home_main`, `linkedin_lane`, `sync`): `23 passed in 12.78s`

## Files Changed

| File | Action |
|------|--------|
| `tests/test_fleet_linkedin_home.py` | CREATED — verbatim tests from brief |
| `src/applypilot/fleet/queue.py` | MODIFIED — added `push_linkedin_jobs`, `approve_linkedin_jobs`, `resolve_linkedin_challenge` |
| `src/applypilot/fleet/sync.py` | MODIFIED — added `push_linkedin_eligible` |
| `src/applypilot/fleet/linkedin_home_main.py` | CREATED — home driver |
| `pyproject.toml` | MODIFIED — registered `applypilot-fleet-linkedin-home` entrypoint |

## Approve Canary-Gate Trace

`approve(conn, all_pushed=True)` calls `_linkedin_canary_armed(conn)`, which runs:
```sql
SELECT linkedin_canary_enabled FROM fleet_config WHERE id=1
```
- Returns `False` (default) → `raise SystemExit(...)` — test asserts `SystemExit` is raised.
- After `set_linkedin_canary(conn, 1)` sets `linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1`:
  - `_linkedin_canary_armed` returns `True`
  - `approve` generates a UTC timestamp token, calls `approve_linkedin_jobs(conn, ['q1'], token)`
  - `approve_linkedin_jobs` runs `UPDATE linkedin_queue SET approved_batch=%s ... WHERE url = ANY(%s) AND status='queued'`
  - Test verifies `linkedin_queue.approved_batch == token` ✓

The gate reads/writes ONLY `linkedin_canary_enabled` / `linkedin_canary_remaining` — never touches A's `canary_enabled` / `canary_remaining`.

## dedup_key Confirmation

`push_linkedin_jobs` computes:
```python
dk = r.get("dedup_key") or _dedup.dedup_key(r.get("company"), r.get("title"))
```
This is the exact same `_dedup.dedup_key` call the offsite `push_apply_jobs` uses.

Test asserts `r["dedup_key"] == _dedup.dedup_key("Acme", "COS")`. `"COS"` normalizes to `"chief of staff"` via `_ROLE_SYNONYMS`, so the key matches identically between lanes → cross-lane `applied_set` dedup works. ✓

## Host-Filter Confirmation (push_linkedin_eligible)

The `_PUSH_LINKEDIN_SELECT` query uses the INVERSE effective-host predicate:
```sql
AND (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'
```
vs. offsite (`_PUSH_APPLY_SELECT`):
```sql
AND application_url LIKE 'http%'
AND application_url NOT LIKE '%linkedin.com%'
```
Rows stage UNAPPROVED (`approved_batch=None` by default); approval requires arming the LinkedIn canary first. ✓

## Self-Review Checklist

- [x] `approve` raises `SystemExit` unless `_linkedin_canary_armed` returns True
- [x] `_linkedin_canary_armed` reads `linkedin_canary_enabled` NOT `canary_enabled`
- [x] `set_linkedin_canary` / `lift_linkedin_canary` touch `linkedin_canary_*` columns only
- [x] `push_linkedin_jobs` writes `dedup_key=_dedup.dedup_key(company, title)` (same function as offsite)
- [x] `push_linkedin_eligible` uses inverse effective-host LinkedIn predicate, stages UNAPPROVED
- [x] `resolve_linkedin_challenge` mirrors `resolve_challenge` over `linkedin_queue`
- [x] Offsite functions `push_apply_jobs`, `approve_jobs`, `resolve_challenge`, `push_apply_eligible` are UNTOUCHED
- [x] `_LEASE_APPLY`, `_LEASE_LINKEDIN` are UNTOUCHED
- [x] `applypilot-fleet-linkedin-home` entrypoint registered in `pyproject.toml`
- [x] dirty files not touched
- [x] `git add` targets only the five exact paths from the brief

## Concerns

None.

---

# Task 8 Report: build_health_report (Fleet Watchdog Layer B reporting half)

## Status: GREEN

## RED phase
After adding the two verbatim tests from the brief, ran:
```
.conda-env/python.exe -m pytest tests/test_fleet_monitor.py -q
```
Result: 2 failed (AttributeError: module 'applypilot.fleet.monitor' has no attribute 'build_health_report'), 2 passed.

## GREEN phase
Implemented `build_health_report` in `src/applypilot/fleet/monitor.py` as specified verbatim in the brief. Ran again:
Result: 4 passed in 7.70s.

## Files changed
- `src/applypilot/fleet/monitor.py` — added module-level `build_health_report` function (45 lines); `MonitorActions` untouched
- `tests/test_fleet_monitor.py` — added two new tests verbatim from brief

## Commit
SHA: c516688
Subject: feat(fleet): monitor health-report generator with anomaly escalation

## Self-review checklist
- [x] All 6 section labels present: `MACHINES`, `QUEUES`, `GOVERNOR`, `CAPTCHA BACKLOG`, `SPEND`, `NEEDS YOUR DECISION`
- [x] High-challenge scope (`host:bad.com`, rate=0.55 >= 0.4) appears in anomaly section
- [x] Offline machine (`w2`, last_beat=None) appears in anomaly section
- [x] Near-cap spend ($9.50 of $10.00 cap = 95% >= 90%) flagged; "cap" present in output
- [x] Clean snapshot yields `"none"` in the NEEDS YOUR DECISION section
- [x] All dict accesses use `.get(...)` defensively — no KeyError on partial snapshots
- [x] Spend-near-cap flag only triggers when `cost_cap_total` is provided and > 0
- [x] `MonitorActions` (Task 7) unchanged
- [x] Only the two exact paths staged (`git add src/applypilot/fleet/monitor.py tests/test_fleet_monitor.py`)
- [x] Not pushed

## Concerns
None.

---

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

---

# Task 8 (Final) — Apply Lane A Canary Go-Live E2E + Runbook

## e2e Status: GREEN (first run, no iteration)

`tests/test_fleet_apply_e2e.py::test_canary_go_live_path` PASSED immediately.

### Seed adjustment (host-gap avoidance)

The brief's seed used `apply_domain='acme.com'` for all 5 rows.  Adjusted to
distinct domains `acme0.com … acme4.com` per the task brief note.  Rationale: after
the first confirmed apply, `write_apply_result` stamps `last_applied_at=now()` on
`host:acme.com` and the next lease query blocks that host for ~90s.  Without distinct
domains the second apply would be blocked by the host-gap governor rather than the
canary, making `applied == 2` accidentally true for the wrong reason.  With distinct
domains the CANARY (not the host gap) is what caps at K=2.  Minimal, correct
adjustment per brief instructions.

## Full Suite Gate

Command:
```
.conda-env/python.exe -m pytest tests/test_fleet_*.py tests/test_codex_bridge.py \
  tests/test_discovery_adapter.py tests/test_frontier_*.py tests/test_cli_providers.py \
  tests/test_build_score_prompt_text.py -q
```

Result: **201 passed, 1 skipped, 0 failures** (50.53s)

(The 1 skip is a pre-existing codex-bridge skip, unrelated to this task.)

## Production Code Touched

**None.** E2e passes against code built in Tasks 1–7 with zero production changes.

## Self-Review

**E2E proves the canary caps at exactly K then auto-pauses:**
- Seeds 5 rows with distinct apply_domain (host-gap avoidance).
- Arms canary K=2 via `hm.set_canary(conn, 2)`.
- Approves all queued rows via `hm.approve(conn, all_pushed=True)` (the approve
  call itself would refuse if the canary were not armed — double-tests that gate).
- Runs 6 worker ticks (2 more than the budget) with a stub `apply_fn` returning
  `{"run_status": "applied", "est_cost_usd": 0.01}`.
- Asserts `applied == 2` (canary caps) AND `fleet_config.paused is True` (auto-pause).

**Runbook covers:**
- P1: v1-fleet-off precondition.
- P2: watchdog-running requirement.
- Ordered steps: pull → push → canary K → approve --all-pushed →
  start applypilot-fleet-apply → applies ≤K then auto-pauses →
  pull + review + challenges/resolve-challenge →
  canary N or lift-canary + set spend_cap_usd.
- Residuals: aggregator/push cross-check, approved_batch presence-stamp,
  per-destination block risk, spend_cap_usd=0 means no cap.

## Files Created

- `tests/test_fleet_apply_e2e.py`
- `docs/fleet-apply-lane-runbook.md`

## Files Modified (production code)

None.

## Concerns

None.

---

# Task 8 Report: Outcomes Tracker CLI — `outcomes-scan` + `outcomes-dashboard`

## Status: GREEN

## What was implemented

Two flat Typer commands added to `src/applypilot/cli.py` after the `scan-gmail` command (after line 1577):

### `outcomes-scan` (`@app.command("outcomes-scan")`, function `outcomes_scan_command`)
- Options: `--days/-d` (int, default 30), `--reextract` (bool flag), `--credentials` (Optional[Path])
- Calls `_bootstrap()`, then lazily imports and calls `applypilot.outcome_scan.scan_outcomes(days=, credentials_path=, reextract=)`
- Handles `FileNotFoundError` (missing creds) and `ImportError` (missing google deps) with colored error + Exit(1)
- On success renders a Rich `Table` titled "Outcome scan" with rows for inserted/updated/skipped/errors

### `outcomes-dashboard` (`@app.command("outcomes-dashboard")`, function `outcomes_dashboard_command`)
- Options: `--port/-p` (int, default 8765), `--host` (str, default "127.0.0.1"), `--open/--no-open` (bool, default open=True)
- Calls `_bootstrap()`, then lazily imports `applypilot.outcome_dashboard.serve`
- If `open_browser=True`, calls `webbrowser.open(f"http://{host}:{port}")` before serving
- Delegates to `serve(host=host, port=port)`

Test file created: `tests/test_outcomes_cli.py` with the two CliRunner tests verbatim from the brief.

## TDD Evidence

### RED (Step 2)
```
FAILED tests/test_outcomes_cli.py::test_outcomes_scan_renders_counts - assert 2 == 0  (exit code 2 = no such command)
FAILED tests/test_outcomes_cli.py::test_outcomes_dashboard_invokes_serve - assert 2 == 0
2 failed in 0.51s
```

### GREEN (Step 4)
```
tests/test_outcomes_cli.py::test_outcomes_scan_renders_counts PASSED
tests/test_outcomes_cli.py::test_outcomes_dashboard_invokes_serve PASSED
2 passed in 0.37s
```

## Full-suite run (Step 5)
```
27 passed in 1.35s
```
All 27 tests in the outcomes suite pass:
- test_outcome_schema.py: 3 passed
- test_outcome_extract.py: 5 passed
- test_outcome_scan.py: 3 passed
- test_outcome_timeline.py: 6 passed
- test_lane_insights.py: 4 passed
- test_outcome_dashboard.py: 4 passed
- test_outcomes_cli.py: 2 passed

## Files Changed
- `src/applypilot/cli.py` — added 43 lines (two commands) after line 1577
- `tests/test_outcomes_cli.py` — new file, 27 lines (verbatim from brief)
- `src/applypilot/fleet/watchdog.py` — NOT touched (left unstaged as instructed)

## Commit
`cc88f65 feat(outcomes): outcomes-scan + outcomes-dashboard CLI commands`

## Self-Review Findings

1. Spec coverage: Both commands match the brief verbatim.
2. Lazy imports: Both commands use inside-function imports matching the codebase pattern.
3. `open_browser` flag: The `--no-open` flag correctly passes `open_browser=False` through Typer's `--open/--no-open` toggle.
4. `serve()` kwargs: Monkeypatched `serve` captures `**kw`, validating keyword args pass through correctly.
5. No sub-groups introduced: Both commands are flat `@app.command(...)` decorators.
6. Only target files staged: `watchdog.py` and `test_fleet_linkedin_push.py` left unstaged.

## Concerns

None.

---

# Task 8 Fix: Inert-Invariant False Positive — Docstring Stripping

## Status: FIXED + GREEN

## What changed

**File edited:** `tests/test_outcomes_inert_invariant.py` only. `outcome_implied.py` NOT touched.

**Root cause:** `inspect.getsource()` returns raw source including the module docstring of
`outcome_implied.py`, which contains the phrase `record_application` as a "deliberately NOT here"
disclaimer. The inert invariant HOLDS in the actual code — no real write path exists.

**Fix:** Added `_code_only(obj)` helper that uses `ast.parse` + `ast.unparse` to strip docstrings
from all AST nodes before asserting. Since `ast.unparse` omits comments entirely, both docstrings
and `# comments` are removed before the no-write-path assertions run.

In `test_pure_modules_have_no_write_paths`: replaced `inspect.getsource(mod)` with `_code_only(mod)`.
In `test_outcomes_promote_is_preview_only_in_source`: replaced `inspect.getsource(cli.outcomes_promote_command)` with `_code_only(cli.outcomes_promote_command)`.
All 4 string assertions in each test kept exactly as-is.

## Test results

Safety test:
```
tests/test_outcomes_inert_invariant.py::test_pure_modules_have_no_write_paths PASSED
tests/test_outcomes_inert_invariant.py::test_outcomes_promote_is_preview_only_in_source PASSED
2 passed in 0.21s
```

Full outcomes suite:
```
13 passed in 0.73s
```
(test_outcome_implied, test_outcome_lane_signal, test_outcome_export, test_outcomes_integration_cli, test_outcomes_inert_invariant — all 13 PASSED)

## Commit

SHA: 9eb5426
Subject: fix(test): strip docstrings+comments before write-path assertions (Task 8)
Files: tests/test_outcomes_inert_invariant.py only (1 file changed, 18 insertions(+), 2 deletions(-))
