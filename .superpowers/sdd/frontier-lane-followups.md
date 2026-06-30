# Frontier Quality Lane — Deferred Follow-ups

These items were identified during the whole-branch code review but deferred from the initial implementation sprint. They are not bugs; they are incremental improvements worth tracking.

---

## #3 — Configurable timeout and retries (per-job)

**Location:** `src/applypilot/fleet/cli_providers.py` → `score_via_codex`, and its callers `src/applypilot/fleet/frontier_pass.py` → `run_frontier_pass` → `frontier_main` (CLI entry).

**Problem:** `timeout_s=120` and `retries=2` are hard-coded in `score_via_codex`. A degraded backend (slow Codex CLI, flaky metered API) can stall an entire run at 120 s/job with no user control.

**Suggested fix:** Thread `--timeout` and `--retries` CLI flags through `frontier_main` → `run_frontier_pass` → `score_via_codex` (already accepts both kwargs). Expose as optional args with sane defaults so the user can tighten for interactive runs or loosen for overnight batches.

---

## #4 — Record `provider=f"codex-subscription:{model}"` for per-model reporting

**Location:** `src/applypilot/fleet/frontier_pass.py`, line where `provider = "codex-subscription"` is set.

**Problem:** The `frontier_scores.provider` column currently stores `"codex-subscription"` for all subscription calls regardless of model (top vs backlog). The `disagreement_report` and future analytics cannot distinguish which Codex model tier produced a given score.

**Suggested fix:** Set `provider = f"codex-subscription:{model}"` (e.g. `"codex-subscription:gpt-5.5"` vs `"codex-subscription:gpt-5.5-mini"`). No schema change needed — the column is free-text.

---

## #5 — CSV/JSON `--report` output format for `frontier_main`

**Location:** `src/applypilot/fleet/frontier_pass.py` / CLI entry (`frontier_main`).

**Problem:** The current `run_frontier_pass` return dict is printed as a plain Python repr. Downstream scripts (dashboards, notebooks) need a machine-readable format.

**Suggested fix:** Add a `--report {csv,json}` flag to `frontier_main` that writes the disagreement report to stdout or a file in the chosen format. The `disagreement_report` function already returns a list of dicts, making this a thin formatting layer.

---

## #6 — Opus cross-check and `frontier_decision` column (reserved/deferred)

**Location:** `src/applypilot/fleet/frontier_db.py` (`frontier_decision` column), `src/applypilot/fleet/cli_providers.py` (`score_via_claude` stub and `cross_check_opus`).

**Status:** The `frontier_decision` column in the schema and the `score_via_claude` / `cross_check_opus` functions are scaffolded but intentionally unused. They are reserved for a future "Opus arbitration" step where a premium model resolves high-disagreement cases.

**When to revisit:** After pairwise comparative labels are collected and the gate passes, Opus cross-check becomes the logical next quality tier. At that point wire `cross_check_opus` into `run_frontier_pass` for jobs where `agreement < 0.6` and write the verdict into `frontier_decision`.
