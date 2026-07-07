# Apply Cost-Quality Router Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working slice of the apply cost-quality router: a repeatable cost scoreboard, specific no-result/crash classification, additive result metadata, host policy gating for low-yield hosts, and route metrics for the existing Greenhouse shadow path.

**Architecture:** Keep the current fleet/apply behavior intact by default, add measurement first, then enforce only the safest host policy rule: Workday tenants are not unattended unless explicitly trusted. The new modules are small and pure where possible so tests can run without live Postgres, live SQLite, or browsers; integration points are thin wrappers in the CLI, launcher, queue, and sync layers.

**Tech Stack:** Python 3, Typer CLI, psycopg dict rows, sqlite3, existing fleet Postgres schema, existing local SQLite brain, pytest.

---

## Scope

This plan implements Phase 1 from `docs/superpowers/specs/2026-07-06-apply-cost-quality-router-design.md`.

This plan does not build the Ashby adapter or trusted account/profile canaries. Those depend on the scoreboard and route metadata created here.

## File Structure

- Create `src/applypilot/fleet/cost_quality_report.py`
  - Owns pure cost/success/failure aggregation and live data fetch helpers.
  - Exposes `build_report()`, `render_report_markdown()`, `classify_ats()`, and `classify_failure_bucket()`.
- Create `tests/test_cost_quality_report.py`
  - Tests local/fleet row aggregation without touching live databases.
- Create `src/applypilot/apply/failure_classification.py`
  - Owns no-result/crash failure classification from run evidence.
  - Keeps launcher parsing easier to reason about.
- Create `tests/test_apply_failure_classification.py`
  - Tests transcript/tool-call/runtime classifications.
- Modify `src/applypilot/fleet/schema_v3.sql`
  - Adds nullable result-event metadata columns to `apply_result_events`.
- Modify `src/applypilot/fleet/queue.py`
  - Accepts optional result metadata in `write_apply_result()` and inserts it into `apply_result_events`.
- Modify `tests/test_fleet_v3_schema.py`
  - Verifies additive schema columns exist.
- Modify `tests/test_fleet_v3_governor_queue.py`
  - Verifies `write_apply_result()` stores route/failure metadata and remains backwards-compatible.
- Create `src/applypilot/fleet/host_policy.py`
  - Owns deterministic host policy decisions and reasons.
- Create `tests/test_host_policy.py`
  - Tests Workday default supervision, allowlisted stable hosts, and low-success park behavior.
- Modify `src/applypilot/fleet/sync.py`
  - Applies host policy during push in report-only or enforced mode.
- Modify `tests/test_fleet_v3_sync.py`
  - Verifies Workday rows are parked/skipped from unattended push with durable reasons.
- Modify `src/applypilot/apply/launcher.py`
  - Adds failure classification metadata and Greenhouse route metadata into `_last_run_stats`.
- Modify `src/applypilot/fleet/apply_worker_main.py`
  - Passes `_last_run_stats` route/failure metadata to the fleet worker result.
- Modify `src/applypilot/fleet/worker.py`
  - Passes optional route/failure metadata to `queue.write_apply_result()`.
- Modify `src/applypilot/cli.py`
  - Adds `apply-cost-report` command for the scoreboard.
- Create `tests/test_apply_cost_report_cli.py`
  - Tests command rendering with monkeypatched report builder.

---

### Task 1: Add Pure Cost-Quality Report Aggregation

**Files:**
- Create: `src/applypilot/fleet/cost_quality_report.py`
- Create: `tests/test_cost_quality_report.py`

- [ ] **Step 1: Write failing aggregation tests**

Add `tests/test_cost_quality_report.py`:

```python
from decimal import Decimal

from applypilot.fleet.cost_quality_report import (
    classify_ats,
    classify_failure_bucket,
    summarize_fleet_queue,
    summarize_local_jobs,
)


def test_classify_ats_from_application_url():
    assert classify_ats("https://jobs.ashbyhq.com/acme/123") == "ashby"
    assert classify_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert classify_ats("https://grnh.se/abc") == "greenhouse"
    assert classify_ats("https://jobs.lever.co/acme/123") == "lever"
    assert classify_ats("https://adobe.wd5.myworkdayjobs.com/external/job/1") == "workday"
    assert classify_ats("https://example.com/apply") == "other"


def test_classify_failure_bucket_is_stable():
    assert classify_failure_bucket("crash_unconfirmed", "failed:no_result_line") == "agent_browser_runtime"
    assert classify_failure_bucket("failed", "failed:browser_unavailable") == "agent_browser_runtime"
    assert classify_failure_bucket("failed", "expired") == "preflight_or_policy"
    assert classify_failure_bucket("failed", "failed:not_eligible_location") == "preflight_or_policy"
    assert classify_failure_bucket("failed", "failed:email_verification_required") == "email_auth_related"
    assert classify_failure_bucket("blocked", "challenge_pending") == "challenge_related"
    assert classify_failure_bucket("failed", "failed:no_confirmation") == "other"


def test_summarize_fleet_queue_computes_all_in_cost_per_apply():
    rows = [
        {"status": "applied", "apply_error": None, "application_url": "https://jobs.ashbyhq.com/a", "est_cost_usd": Decimal("0.50")},
        {"status": "applied", "apply_error": None, "application_url": "https://boards.greenhouse.io/a/jobs/1", "est_cost_usd": Decimal("0.70")},
        {"status": "failed", "apply_error": "expired", "application_url": "https://boards.greenhouse.io/a/jobs/2", "est_cost_usd": Decimal("0.20")},
        {"status": "crash_unconfirmed", "apply_error": "failed:no_result_line", "application_url": "https://adobe.wd5.myworkdayjobs.com/x", "est_cost_usd": Decimal("1.10")},
        {"status": "queued", "apply_error": None, "application_url": "https://jobs.ashbyhq.com/b", "est_cost_usd": Decimal("0")},
    ]

    summary = summarize_fleet_queue(rows)

    assert summary.applied == 2
    assert summary.terminal_attempts == 4
    assert summary.total_cost_usd == 2.5
    assert summary.cost_per_applied_all_in == 1.25
    assert summary.by_ats["greenhouse"].applied == 1
    assert summary.by_failure_bucket["agent_browser_runtime"].count == 1


def test_summarize_local_jobs_computes_historical_success_rate():
    rows = [
        {"apply_status": "applied", "apply_error": "", "application_url": "https://jobs.ashbyhq.com/a"},
        {"apply_status": "failed", "apply_error": "expired", "application_url": "https://jobs.ashbyhq.com/b"},
        {"apply_status": "applied", "apply_error": "", "application_url": "https://boards.greenhouse.io/a/jobs/1"},
        {"apply_status": "failed", "apply_error": "failed:no_confirmation", "application_url": "https://adobe.wd5.myworkdayjobs.com/x"},
    ]

    summary = summarize_local_jobs(rows)

    assert summary.touched == 4
    assert summary.applied == 2
    assert summary.by_ats["ashby"].success_pct == 50.0
    assert summary.by_ats["workday"].success_pct == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_cost_quality_report.py -q
```

Expected: fail because `applypilot.fleet.cost_quality_report` does not exist.

- [ ] **Step 3: Implement report dataclasses and pure helpers**

Create `src/applypilot/fleet/cost_quality_report.py` with:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable


TERMINAL_STATUSES = {"applied", "failed", "blocked", "crash_unconfirmed"}


@dataclass
class CountCost:
    count: int = 0
    applied: int = 0
    terminal: int = 0
    cost: float = 0.0

    @property
    def success_pct(self) -> float:
        return round((100.0 * self.applied / self.count), 1) if self.count else 0.0

    @property
    def cost_per_applied(self) -> float | None:
        return round(self.cost / self.applied, 4) if self.applied else None


@dataclass
class FailureBucket:
    count: int = 0
    cost: float = 0.0


@dataclass
class FleetQueueSummary:
    applied: int = 0
    terminal_attempts: int = 0
    queued_or_leased: int = 0
    total_cost_usd: float = 0.0
    cost_per_applied_all_in: float | None = None
    cost_per_terminal_attempt: float | None = None
    by_ats: dict[str, CountCost] = field(default_factory=dict)
    by_failure_bucket: dict[str, FailureBucket] = field(default_factory=dict)


@dataclass
class LocalJobsSummary:
    touched: int = 0
    applied: int = 0
    by_ats: dict[str, CountCost] = field(default_factory=dict)
    by_failure_bucket: dict[str, FailureBucket] = field(default_factory=dict)


@dataclass
class CostQualityReport:
    fleet: FleetQueueSummary
    local: LocalJobsSummary


def _money(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def classify_ats(url: str | None) -> str:
    text = (url or "").lower()
    if "ashbyhq.com" in text:
        return "ashby"
    if "greenhouse" in text or "grnh.se" in text:
        return "greenhouse"
    if "lever.co" in text:
        return "lever"
    if "workdayjobs.com" in text or "myworkdayjobs.com" in text:
        return "workday"
    if "smartrecruiters" in text:
        return "smartrecruiters"
    if "workable.com" in text:
        return "workable"
    return "other"


def classify_failure_bucket(status: str | None, apply_error: str | None) -> str:
    err = (apply_error or "").lower()
    st = (status or "").lower()
    if any(token in err for token in ("email", "otp", "auth", "login", "verification")):
        return "email_auth_related"
    if "captcha" in err or "challenge" in err:
        return "challenge_related"
    if (
        st == "crash_unconfirmed"
        or "browser" in err
        or "no_result" in err
        or "timeout" in err
    ):
        return "agent_browser_runtime"
    if any(token in err for token in ("expired", "not_eligible", "location", "already_applied", "excluded")):
        return "preflight_or_policy"
    return "other"


def summarize_fleet_queue(rows: Iterable[dict]) -> FleetQueueSummary:
    summary = FleetQueueSummary()
    for row in rows:
        status = str(row.get("status") or "")
        cost = _money(row.get("est_cost_usd"))
        ats = classify_ats(row.get("application_url"))

        summary.total_cost_usd += cost
        ats_row = summary.by_ats.setdefault(ats, CountCost())
        ats_row.count += 1
        ats_row.cost += cost

        if status == "applied":
            summary.applied += 1
            ats_row.applied += 1
        if status in TERMINAL_STATUSES:
            summary.terminal_attempts += 1
            ats_row.terminal += 1
        if status in {"queued", "leased"}:
            summary.queued_or_leased += 1

        if status in {"failed", "blocked", "crash_unconfirmed"}:
            bucket = classify_failure_bucket(status, row.get("apply_error"))
            bucket_row = summary.by_failure_bucket.setdefault(bucket, FailureBucket())
            bucket_row.count += 1
            bucket_row.cost += cost

    summary.total_cost_usd = round(summary.total_cost_usd, 2)
    summary.cost_per_applied_all_in = (
        round(summary.total_cost_usd / summary.applied, 4) if summary.applied else None
    )
    summary.cost_per_terminal_attempt = (
        round(summary.total_cost_usd / summary.terminal_attempts, 4)
        if summary.terminal_attempts else None
    )
    return summary


def summarize_local_jobs(rows: Iterable[dict]) -> LocalJobsSummary:
    summary = LocalJobsSummary()
    for row in rows:
        status = str(row.get("apply_status") or "")
        if not status:
            continue
        ats = classify_ats(row.get("application_url"))
        summary.touched += 1
        ats_row = summary.by_ats.setdefault(ats, CountCost())
        ats_row.count += 1
        if status == "applied":
            summary.applied += 1
            ats_row.applied += 1
        else:
            bucket = classify_failure_bucket(status, row.get("apply_error"))
            bucket_row = summary.by_failure_bucket.setdefault(bucket, FailureBucket())
            bucket_row.count += 1
    return summary
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_cost_quality_report.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/fleet/cost_quality_report.py tests/test_cost_quality_report.py
git commit -m "feat(fleet): add apply cost quality report core"
```

Expected: commit succeeds with only these two files.

---

### Task 2: Add Live Scoreboard CLI

**Files:**
- Modify: `src/applypilot/fleet/cost_quality_report.py`
- Modify: `src/applypilot/cli.py`
- Create: `tests/test_apply_cost_report_cli.py`

- [ ] **Step 1: Write failing CLI rendering test**

Add `tests/test_apply_cost_report_cli.py`:

```python
from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot.fleet.cost_quality_report import CostQualityReport, FleetQueueSummary, LocalJobsSummary


def test_apply_cost_report_command_prints_summary(monkeypatch):
    runner = CliRunner()
    report = CostQualityReport(
        fleet=FleetQueueSummary(
            applied=2,
            terminal_attempts=4,
            total_cost_usd=2.5,
            cost_per_applied_all_in=1.25,
            cost_per_terminal_attempt=0.625,
        ),
        local=LocalJobsSummary(touched=5, applied=3),
    )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        "applypilot.fleet.cost_quality_report.build_report",
        lambda pg_dsn=None, sqlite_path=None: report,
    )
    monkeypatch.setattr(
        "applypilot.fleet.cost_quality_report.render_report_markdown",
        lambda r: "Cost per applied: $1.25",
    )

    result = runner.invoke(cli.app, ["apply-cost-report"])

    assert result.exit_code == 0
    assert "Cost per applied: $1.25" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_apply_cost_report_cli.py -q
```

Expected: fail because the CLI command does not exist.

- [ ] **Step 3: Add live fetch and renderer**

Append to `src/applypilot/fleet/cost_quality_report.py`:

```python
def fetch_fleet_queue_rows(pg_dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status::text AS status, apply_error, application_url, "
                "COALESCE(est_cost_usd,0) AS est_cost_usd FROM apply_queue"
            )
            return list(cur.fetchall())


def fetch_local_job_rows(sqlite_path: str | Path) -> list[dict]:
    path = Path(sqlite_path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT apply_status, apply_error, application_url "
            "FROM jobs WHERE apply_status IS NOT NULL"
        ).fetchall()
        return [dict(row) for row in rows]


def default_sqlite_path() -> Path:
    import os

    return Path(os.environ.get("LOCALAPPDATA", "")) / "ApplyPilot" / "applypilot.db"


def build_report(*, pg_dsn: str | None = None, sqlite_path: str | Path | None = None) -> CostQualityReport:
    if not pg_dsn:
        import os

        pg_dsn = os.environ.get("FLEET_PG_DSN") or "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
    local_path = Path(sqlite_path) if sqlite_path else default_sqlite_path()
    return CostQualityReport(
        fleet=summarize_fleet_queue(fetch_fleet_queue_rows(pg_dsn)),
        local=summarize_local_jobs(fetch_local_job_rows(local_path)),
    )


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def render_report_markdown(report: CostQualityReport) -> str:
    lines = [
        "# Apply Cost Quality Report",
        "",
        "## Fleet Cost",
        f"- Applied: {report.fleet.applied}",
        f"- Terminal attempts: {report.fleet.terminal_attempts}",
        f"- Total recorded cost: ${report.fleet.total_cost_usd:.2f}",
        f"- All-in cost per successful apply: {_fmt_money(report.fleet.cost_per_applied_all_in)}",
        f"- Cost per terminal attempt: {_fmt_money(report.fleet.cost_per_terminal_attempt)}",
        "",
        "## Local History",
        f"- Touched jobs: {report.local.touched}",
        f"- Applied jobs: {report.local.applied}",
        "",
        "## ATS History",
        "| ATS | Touched | Applied | Success % | Fleet cost/apply |",
        "|---|---:|---:|---:|---:|",
    ]
    ats_keys = sorted(set(report.local.by_ats) | set(report.fleet.by_ats))
    for ats in ats_keys:
        local = report.local.by_ats.get(ats, CountCost())
        fleet = report.fleet.by_ats.get(ats, CountCost())
        lines.append(
            f"| {ats} | {local.count} | {local.applied} | {local.success_pct:.1f}% | "
            f"{_fmt_money(fleet.cost_per_applied)} |"
        )
    lines.extend(["", "## Failure Buckets", "| Bucket | Count | Cost |", "|---|---:|---:|"])
    for bucket, row in sorted(report.fleet.by_failure_bucket.items(), key=lambda item: item[1].cost, reverse=True):
        lines.append(f"| {bucket} | {row.count} | ${row.cost:.2f} |")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Add Typer command**

In `src/applypilot/cli.py`, add near the other apply/fleet diagnostic commands:

```python
@app.command("apply-cost-report")
def apply_cost_report_command(
    pg_dsn: Optional[str] = typer.Option(None, "--dsn", help="Fleet Postgres DSN. Defaults to FLEET_PG_DSN or local fleet DB."),
    sqlite_path: Optional[str] = typer.Option(None, "--sqlite", help="Local ApplyPilot SQLite brain path."),
) -> None:
    """Print quality-adjusted apply cost and success metrics."""
    _bootstrap()

    from applypilot.fleet.cost_quality_report import build_report, render_report_markdown

    report = build_report(pg_dsn=pg_dsn, sqlite_path=sqlite_path)
    console.print(render_report_markdown(report))
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_cost_quality_report.py tests\test_apply_cost_report_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run live command**

Run:

```powershell
.\.conda-env\Scripts\applypilot.exe apply-cost-report
```

Expected: prints sections named `Fleet Cost`, `Local History`, `ATS History`, and `Failure Buckets`.

- [ ] **Step 7: Commit**

Run:

```powershell
git add src/applypilot/fleet/cost_quality_report.py src/applypilot/cli.py tests/test_apply_cost_report_cli.py
git commit -m "feat(fleet): add apply cost report command"
```

Expected: commit succeeds.

---

### Task 3: Add Runtime Failure Classification Core

**Files:**
- Create: `src/applypilot/apply/failure_classification.py`
- Create: `tests/test_apply_failure_classification.py`

- [ ] **Step 1: Write failing classifier tests**

Add `tests/test_apply_failure_classification.py`:

```python
from applypilot.apply.failure_classification import FailureEvidence, classify_apply_failure


def test_usage_limit_before_application_tools_is_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="You've hit your session limit. Switch to another model.",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True


def test_usage_limit_after_browser_tool_is_not_safe_requeue():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="You've hit your usage limit",
        application_tool_calls=2,
        tool_calls_total=2,
        last_tool="browser_click",
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "post_browser_no_result"
    assert result.safe_requeue is False


def test_mcp_start_failure_is_worker_level():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="MCP startup failed: handshaking with MCP server failed",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "mcp_start_failure"
    assert result.worker_level is True


def test_timeout_after_form_touch_is_unconfirmed():
    evidence = FailureEvidence(
        status="failed:timeout",
        transcript="",
        application_tool_calls=5,
        tool_calls_total=5,
        last_tool="browser_click",
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "post_form_crash_unconfirmed"
    assert result.safe_requeue is False


def test_zero_tool_no_result_is_distinct():
    evidence = FailureEvidence(
        status="failed:no_result_line",
        transcript="agent exited without result",
        application_tool_calls=0,
        tool_calls_total=0,
    )

    result = classify_apply_failure(evidence)

    assert result.failure_class == "zero_tool_no_result"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_apply_failure_classification.py -q
```

Expected: fail because `applypilot.apply.failure_classification` does not exist.

- [ ] **Step 3: Implement classifier**

Create `src/applypilot/apply/failure_classification.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureEvidence:
    status: str
    transcript: str = ""
    application_tool_calls: int = 0
    tool_calls_total: int = 0
    last_tool: str = ""
    chrome_launch_ok: bool | None = None
    cdp_connect_ok: bool | None = None
    mcp_started_ok: bool | None = None
    agent_exit_code: int | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    safe_requeue: bool = False
    worker_level: bool = False


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(needle in low for needle in needles)


def classify_apply_failure(evidence: FailureEvidence) -> FailureClassification:
    status = (evidence.status or "").lower()
    transcript = evidence.transcript or ""
    touched_application = evidence.application_tool_calls > 0

    if _has_any(transcript, ("mcp startup failed", "handshaking with mcp server failed", "mcp server failed")):
        return FailureClassification("mcp_start_failure", worker_level=True)
    if evidence.chrome_launch_ok is False:
        return FailureClassification("browser_launch_failure", worker_level=True)
    if evidence.cdp_connect_ok is False or _has_any(transcript, ("cdp", "browser connection lost")):
        return FailureClassification("cdp_lost", worker_level=True)
    if _has_any(transcript, ("usage limit", "session limit", "switch to another model")) and not touched_application:
        return FailureClassification("usage_or_session_limit", safe_requeue=True)
    if _has_any(transcript, ("auth required", "invalid api key", "no access token")) and not touched_application:
        return FailureClassification("agent_auth", safe_requeue=True, worker_level=True)
    if "timeout" in status and touched_application:
        return FailureClassification("post_form_crash_unconfirmed")
    if "timeout" in status:
        return FailureClassification("timeout", safe_requeue=True, worker_level=True)
    if "no_result_line" in status and not touched_application and evidence.tool_calls_total == 0:
        return FailureClassification("zero_tool_no_result", safe_requeue=True)
    if "no_result_line" in status and touched_application:
        return FailureClassification("post_browser_no_result")
    if "crash_unconfirmed" in status and touched_application:
        return FailureClassification("post_form_crash_unconfirmed")
    return FailureClassification("malformed_result")
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_apply_failure_classification.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/apply/failure_classification.py tests/test_apply_failure_classification.py
git commit -m "feat(apply): classify no-result apply failures"
```

Expected: commit succeeds.

---

### Task 4: Persist Result Metadata in Fleet Events

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `src/applypilot/fleet/queue.py`
- Modify: `tests/test_fleet_v3_schema.py`
- Modify: `tests/test_fleet_v3_governor_queue.py`

- [ ] **Step 1: Write failing schema test**

In `tests/test_fleet_v3_schema.py`, add a test that applies the schema and checks event columns:

```python
def test_apply_result_events_include_cost_router_metadata(fleet_db):
    with fleet_db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='apply_result_events'"
        )
        cols = {r[0] for r in cur.fetchall()}

    assert "route" in cols
    assert "failure_class" in cols
    assert "tool_calls_total" in cols
    assert "application_tool_calls" in cols
    assert "last_tool" in cols
    assert "host_policy" in cols
    assert "result_metadata" in cols
```

- [ ] **Step 2: Write failing queue metadata test**

In `tests/test_fleet_v3_governor_queue.py`, add:

```python
def test_write_apply_result_persists_router_metadata(fleet_db):
    from applypilot.fleet import queue

    with fleet_db.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, company, title, application_url, status, approved_batch) "
            "VALUES ('u-meta', 'Acme', 'Analyst', 'https://boards.greenhouse.io/acme/jobs/1', 'leased', 'b1')"
        )
        fleet_db.commit()

    ok = queue.write_apply_result(
        fleet_db,
        worker_id="w1",
        url="u-meta",
        status="failed",
        apply_status="failed",
        apply_error="failed:no_result_line",
        target_host="boards.greenhouse.io",
        home_ip="127.0.0.1",
        est_cost_usd=0.25,
        agent="claude",
        agent_model="sonnet",
        route="agent",
        failure_class="zero_tool_no_result",
        tool_calls_total=0,
        application_tool_calls=0,
        last_tool="",
        host_policy="allow:greenhouse",
        result_metadata={"job_log": "worker.log"},
    )

    assert ok is True
    with fleet_db.cursor() as cur:
        cur.execute(
            "SELECT route, failure_class, tool_calls_total, application_tool_calls, "
            "last_tool, host_policy, result_metadata->>'job_log' "
            "FROM apply_result_events WHERE url='u-meta'"
        )
        row = cur.fetchone()

    assert row[0] == "agent"
    assert row[1] == "zero_tool_no_result"
    assert row[2] == 0
    assert row[3] == 0
    assert row[4] == ""
    assert row[5] == "allow:greenhouse"
    assert row[6] == "worker.log"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_fleet_v3_schema.py tests/test_fleet_v3_governor_queue.py -q
```

Expected: fail because the metadata columns and `write_apply_result()` parameters do not exist.

- [ ] **Step 4: Add schema columns**

In `src/applypilot/fleet/schema_v3.sql`, add after `apply_result_events` creation:

```sql
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS route TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS failure_class TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS tool_calls_total INTEGER;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS application_tool_calls INTEGER;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS last_tool TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS host_policy TEXT;
ALTER TABLE apply_result_events ADD COLUMN IF NOT EXISTS result_metadata JSONB;
```

- [ ] **Step 5: Extend `queue.write_apply_result()` signature and insert**

In `src/applypilot/fleet/queue.py`, extend `write_apply_result()` with keyword-only defaults:

```python
def write_apply_result(
    conn,
    worker_id,
    url,
    status,
    apply_status=None,
    apply_error=None,
    target_host=None,
    home_ip=None,
    est_cost_usd=0,
    agent=None,
    agent_model=None,
    apply_duration_ms=None,
    outcome=None,
    apply_channel=None,
    apply_external_host=None,
    route=None,
    failure_class=None,
    tool_calls_total=None,
    application_tool_calls=None,
    last_tool=None,
    host_policy=None,
    result_metadata=None,
):
```

Import JSON support at the top:

```python
from psycopg.types.json import Jsonb
```

Change the `INSERT INTO apply_result_events` column list to include:

```python
"agent, agent_model, est_cost_usd, apply_duration_ms, result_line, source, "
"route, failure_class, tool_calls_total, application_tool_calls, last_tool, host_policy, result_metadata"
```

Change the values clause to include:

```python
") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,0),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
```

Append these values after `"worker"`:

```python
route,
failure_class,
tool_calls_total,
application_tool_calls,
last_tool,
host_policy,
Jsonb(result_metadata or {}) if result_metadata is not None else None,
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_fleet_v3_schema.py tests/test_fleet_v3_governor_queue.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

Run:

```powershell
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/queue.py tests/test_fleet_v3_schema.py tests/test_fleet_v3_governor_queue.py
git commit -m "feat(fleet): store apply result route metadata"
```

Expected: commit succeeds.

---

### Task 5: Feed Failure and Route Metadata from Launcher to Worker

**Files:**
- Modify: `src/applypilot/apply/launcher.py`
- Modify: `src/applypilot/fleet/apply_worker_main.py`
- Modify: `src/applypilot/fleet/worker.py`
- Modify: `tests/test_apply_usage_limit_requeue.py`
- Modify: `tests/test_fleet_v3_worker.py`

- [ ] **Step 1: Write launcher metadata test**

In `tests/test_apply_usage_limit_requeue.py`, add:

```python
def test_classified_usage_limit_stats_are_available_for_worker_metadata(monkeypatch):
    from applypilot.apply.failure_classification import FailureEvidence, classify_apply_failure

    result = classify_apply_failure(
        FailureEvidence(
            status="failed:no_result_line",
            transcript="You've hit your usage limit. Switch to another model.",
            application_tool_calls=0,
            tool_calls_total=0,
        )
    )

    assert result.failure_class == "usage_or_session_limit"
    assert result.safe_requeue is True
```

This test uses the classifier directly because `run_job()` process spawning is already covered by existing launcher tests.

- [ ] **Step 2: Write worker metadata handoff test**

In `tests/test_fleet_v3_worker.py`, add a test using the existing worker loop fixtures:

```python
def test_apply_worker_passes_route_metadata_to_queue(monkeypatch):
    from applypilot.fleet.worker import FleetWorker

    captured = {}

    def fake_write(conn, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("applypilot.fleet.queue.write_apply_result", fake_write)

    worker = FleetWorker(
        dsn="dbname=fake",
        worker_id="w1",
        home_ip="127.0.0.1",
        apply_fn=lambda job: {
            "run_status": "failed:no_result_line",
            "est_cost_usd": 0.0,
            "agent": "claude",
            "agent_model": "sonnet",
            "route": "agent",
            "failure_class": "zero_tool_no_result",
            "tool_calls_total": 0,
            "application_tool_calls": 0,
            "last_tool": "",
            "result_metadata": {"job_log": "worker.log"},
        },
    )

    row = {
        "url": "u1",
        "application_url": "https://boards.greenhouse.io/acme/jobs/1",
        "target_host": "boards.greenhouse.io",
    }
    worker._record_apply_result(None, row, worker.apply_fn(row))

    assert captured["route"] == "agent"
    assert captured["failure_class"] == "zero_tool_no_result"
    assert captured["result_metadata"]["job_log"] == "worker.log"
```

If `FleetWorker` does not expose `_record_apply_result`, add the assertion to the narrowest existing worker-result test in `tests/test_fleet_v3_worker.py` and use the same output dictionary shape.

- [ ] **Step 3: Run tests to verify they fail where handoff is missing**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_apply_usage_limit_requeue.py tests/test_fleet_v3_worker.py -q
```

Expected: classifier test passes after Task 3; worker metadata handoff fails until worker code passes new keys.

- [ ] **Step 4: Add launcher classification metadata**

In `src/applypilot/apply/launcher.py`, import:

```python
from applypilot.apply.failure_classification import FailureEvidence, classify_apply_failure
```

After `output`, `result_source`, `application_tool_calls`, and `stats` are known, build classification for failure statuses:

```python
failure = None
if status != "applied":
    failure = classify_apply_failure(
        FailureEvidence(
            status=status,
            transcript=output,
            application_tool_calls=application_tool_calls[0],
            tool_calls_total=stats.get("turns", 0) or application_tool_calls[0],
            last_tool=(get_state(worker_id).last_action if get_state(worker_id) else ""),
            timeout_seconds=AGENT_TIMEOUT_SECONDS,
        )
    )
run_stats["failure_class"] = failure.failure_class if failure else None
run_stats["safe_requeue"] = failure.safe_requeue if failure else False
run_stats["worker_level_failure"] = failure.worker_level if failure else False
run_stats["application_tool_calls"] = application_tool_calls[0]
run_stats["tool_calls_total"] = stats.get("turns", 0) or application_tool_calls[0]
run_stats["last_tool"] = get_state(worker_id).last_action if get_state(worker_id) else ""
run_stats["route"] = run_stats.get("route") or "agent"
```

- [ ] **Step 5: Return metadata from `apply_worker_main` apply function**

In `src/applypilot/fleet/apply_worker_main.py`, where `out` is built from `_last_run_stats`, add:

```python
out.update({
    "route": stats.get("route") or "agent",
    "failure_class": stats.get("failure_class"),
    "tool_calls_total": stats.get("tool_calls_total"),
    "application_tool_calls": stats.get("application_tool_calls"),
    "last_tool": stats.get("last_tool"),
    "result_metadata": {
        "job_log": stats.get("job_log"),
        "safe_requeue": stats.get("safe_requeue"),
        "worker_level_failure": stats.get("worker_level_failure"),
    },
})
```

- [ ] **Step 6: Pass metadata through fleet worker**

In `src/applypilot/fleet/worker.py`, when calling `queue.write_apply_result()`, pass:

```python
route=res.get("route"),
failure_class=res.get("failure_class"),
tool_calls_total=res.get("tool_calls_total"),
application_tool_calls=res.get("application_tool_calls"),
last_tool=res.get("last_tool"),
result_metadata=res.get("result_metadata"),
```

- [ ] **Step 7: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_apply_failure_classification.py tests/test_apply_usage_limit_requeue.py tests/test_fleet_v3_worker.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

Run:

```powershell
git add src/applypilot/apply/launcher.py src/applypilot/fleet/apply_worker_main.py src/applypilot/fleet/worker.py tests/test_apply_usage_limit_requeue.py tests/test_fleet_v3_worker.py
git commit -m "feat(fleet): pass apply failure metadata"
```

Expected: commit succeeds.

---

### Task 6: Add Host Policy Core

**Files:**
- Create: `src/applypilot/fleet/host_policy.py`
- Create: `tests/test_host_policy.py`

- [ ] **Step 1: Write failing policy tests**

Add `tests/test_host_policy.py`:

```python
from applypilot.fleet.host_policy import HostPolicyDecision, decide_host_policy, host_from_url


def test_host_from_url_normalizes_host():
    assert host_from_url("https://Boards.Greenhouse.io/acme/jobs/1") == "boards.greenhouse.io"
    assert host_from_url("not a url") == ""


def test_greenhouse_and_ashby_are_allowed_by_default():
    assert decide_host_policy("https://boards.greenhouse.io/acme/jobs/1").mode == "allow"
    assert decide_host_policy("https://jobs.ashbyhq.com/acme/1").mode == "allow"


def test_workday_is_supervised_by_default():
    decision = decide_host_policy("https://adobe.wd5.myworkdayjobs.com/external/job/1")

    assert decision == HostPolicyDecision(
        mode="supervised",
        reason="workday_tenant_requires_trust",
        host="adobe.wd5.myworkdayjobs.com",
        ats="workday",
    )


def test_trusted_workday_tenant_canary_overrides_default():
    decision = decide_host_policy(
        "https://adobe.wd5.myworkdayjobs.com/external/job/1",
        trusted_hosts={"adobe.wd5.myworkdayjobs.com": "canary"},
    )

    assert decision.mode == "canary"
    assert decision.reason == "trusted_host"


def test_known_low_yield_public_board_is_parked():
    decision = decide_host_policy("https://www.indeed.com/viewjob?jk=1")

    assert decision.mode == "supervised"
    assert decision.reason == "login_gate_prone"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_host_policy.py -q
```

Expected: fail because `applypilot.fleet.host_policy` does not exist.

- [ ] **Step 3: Implement host policy module**

Create `src/applypilot/fleet/host_policy.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from applypilot.fleet.cost_quality_report import classify_ats


@dataclass(frozen=True)
class HostPolicyDecision:
    mode: str
    reason: str
    host: str
    ats: str

    @property
    def unattended_allowed(self) -> bool:
        return self.mode in {"allow", "canary"}

    @property
    def label(self) -> str:
        return f"{self.mode}:{self.reason}"


LOW_YIELD_SUPERVISED_HOSTS = {
    "www.indeed.com": "login_gate_prone",
    "hiring.cafe": "login_gate_prone",
    "www.linkedin.com": "linkedin_profile_required",
}


def host_from_url(url: str | None) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    return (parsed.hostname or "").lower()


def decide_host_policy(
    application_url: str | None,
    *,
    trusted_hosts: dict[str, str] | None = None,
) -> HostPolicyDecision:
    host = host_from_url(application_url)
    ats = classify_ats(application_url)
    trusted_hosts = trusted_hosts or {}
    if host in trusted_hosts:
        return HostPolicyDecision(mode=trusted_hosts[host], reason="trusted_host", host=host, ats=ats)
    if ats == "workday":
        return HostPolicyDecision(mode="supervised", reason="workday_tenant_requires_trust", host=host, ats=ats)
    if host in LOW_YIELD_SUPERVISED_HOSTS:
        return HostPolicyDecision(mode="supervised", reason=LOW_YIELD_SUPERVISED_HOSTS[host], host=host, ats=ats)
    if ats in {"ashby", "greenhouse", "workable"}:
        return HostPolicyDecision(mode="allow", reason=f"{ats}_baseline_healthy", host=host, ats=ats)
    if ats in {"lever", "smartrecruiters"}:
        return HostPolicyDecision(mode="canary", reason=f"{ats}_limited_history", host=host, ats=ats)
    return HostPolicyDecision(mode="allow", reason="default_unclassified", host=host, ats=ats)
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_host_policy.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/fleet/host_policy.py tests/test_host_policy.py
git commit -m "feat(fleet): add apply host policy"
```

Expected: commit succeeds.

---

### Task 7: Enforce Workday Host Policy During Push

**Files:**
- Modify: `src/applypilot/fleet/sync.py`
- Modify: `tests/test_fleet_v3_sync.py`

- [ ] **Step 1: Locate push row selection**

Run:

```powershell
rg -n "push_apply_eligible|apply_queue|INSERT INTO apply_queue|application_url" src/applypilot/fleet/sync.py tests/test_fleet_v3_sync.py
```

Expected: output shows the function that builds rows for `apply_queue` and existing push tests.

- [ ] **Step 2: Write failing Workday gate test**

In `tests/test_fleet_v3_sync.py`, add a test near existing push tests:

```python
def test_push_apply_eligible_parks_untrusted_workday(monkeypatch, fleet_db, tmp_path):
    from applypilot.fleet import sync

    rows = [
        {
            "url": "job-workday",
            "company": "Adobe",
            "title": "Analyst",
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "score": 8.5,
            "apply_domain": "adobe.wd5.myworkdayjobs.com",
            "dedup_key": "dedup-workday",
        },
        {
            "url": "job-greenhouse",
            "company": "Acme",
            "title": "Analyst",
            "application_url": "https://boards.greenhouse.io/acme/jobs/1",
            "score": 8.5,
            "apply_domain": "boards.greenhouse.io",
            "dedup_key": "dedup-greenhouse",
        },
    ]

    result = sync.push_apply_rows(fleet_db, rows, enforce_host_policy=True)

    with fleet_db.cursor() as cur:
        cur.execute("SELECT url, status, apply_error FROM apply_queue ORDER BY url")
        stored = cur.fetchall()

    assert result["pushed"] == 1
    assert result["parked"] == 1
    assert stored == [
        ("job-greenhouse", "queued", None),
        ("job-workday", "failed", "host_policy:workday_tenant_requires_trust"),
    ]
```

If `push_apply_rows()` does not exist, create that focused helper in the implementation step and have the existing `push_apply_eligible()` call it. This keeps policy testable without constructing a full local SQLite fixture.

- [ ] **Step 3: Run test to verify it fails**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_fleet_v3_sync.py -q
```

Expected: fail because policy is not applied and/or `push_apply_rows()` is missing.

- [ ] **Step 4: Implement focused push helper with policy**

In `src/applypilot/fleet/sync.py`, add:

```python
def push_apply_rows(conn, rows, *, enforce_host_policy: bool = False) -> dict:
    from applypilot.fleet.host_policy import decide_host_policy

    pushed = 0
    parked = 0
    with conn.cursor() as cur:
        for row in rows:
            decision = decide_host_policy(row.get("application_url"))
            status = "queued"
            apply_status = None
            apply_error = None
            if enforce_host_policy and not decision.unattended_allowed:
                status = "failed"
                apply_status = "skipped"
                apply_error = f"host_policy:{decision.reason}"
                parked += 1
            else:
                pushed += 1
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, apply_domain, status, apply_status, apply_error, dedup_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::apply_queue_status,%s,%s,%s) "
                "ON CONFLICT (url) DO NOTHING",
                (
                    row.get("url"),
                    row.get("company"),
                    row.get("title"),
                    row.get("application_url"),
                    row.get("score"),
                    row.get("apply_domain"),
                    status,
                    apply_status,
                    apply_error,
                    row.get("dedup_key"),
                ),
            )
    conn.commit()
    return {"pushed": pushed, "parked": parked}
```

Then update the existing push path to call `push_apply_rows(..., enforce_host_policy=True)` once the rows have been selected from the local brain. Preserve all existing dedup and approval behavior around the helper.

- [ ] **Step 5: Run focused sync tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_fleet_v3_sync.py tests/test_host_policy.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/applypilot/fleet/sync.py tests/test_fleet_v3_sync.py
git commit -m "feat(fleet): gate untrusted workday pushes"
```

Expected: commit succeeds.

---

### Task 8: Tag Greenhouse Shadow and Adapter Routes

**Files:**
- Modify: `src/applypilot/apply/launcher.py`
- Modify: `tests/test_greenhouse_submit.py`
- Modify: `tests/test_apply_agent_selection.py`

- [ ] **Step 1: Write route-tag behavior test**

In `tests/test_greenhouse_submit.py`, add a pure assertion for route names:

```python
def test_apply_greenhouse_result_includes_route_for_deterministic_dry_run():
    page = FakePage()
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE,
        resume_text=_RESUME,
        resume_path="/r.pdf",
        page=page,
        fetch=lambda u: {"questions": _READY_QS},
        answer_fn=_good,
    )

    assert res["route"] == "deterministic"
    assert res["ready"] is True
```

In `tests/test_apply_agent_selection.py`, add a direct helper test for route naming:

```python
def test_route_from_greenhouse_result_names_shadow_and_submit():
    from applypilot.apply import launcher

    assert launcher._route_from_greenhouse_result(
        {"route": "deterministic", "ready": True},
        own=False,
    ) == "adapter_shadow:greenhouse"
    assert launcher._route_from_greenhouse_result(
        {"route": "deterministic", "ready": True},
        own=True,
    ) == "adapter_submit:greenhouse"
    assert launcher._route_from_greenhouse_result(
        {"route": "agent_fallback", "ready": False},
        own=False,
    ) == "agent"
```

- [ ] **Step 2: Run route tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_greenhouse_submit.py tests/test_apply_agent_selection.py -q
```

Expected: Greenhouse pure route test passes; launcher helper test fails until the helper exists.

- [ ] **Step 3: Add route helper in launcher**

In `src/applypilot/apply/launcher.py`, add near `_maybe_greenhouse_apply()`:

```python
def _route_from_greenhouse_result(res: dict | None, *, own: bool) -> str | None:
    if not res:
        return None
    if res.get("route") != "deterministic":
        return "agent"
    return "adapter_submit:greenhouse" if own else "adapter_shadow:greenhouse"
```

When `_maybe_greenhouse_apply()` gets a deterministic shadow result and returns `None`, store shadow metadata in a module-level per-worker map:

```python
_adapter_route_stats[worker_id] = {
    "route": _route_from_greenhouse_result(res, own=own),
    "adapter_name": "greenhouse",
    "adapter_plan_ready": bool(res.get("ready")),
}
```

When `_run_job_impl()` builds `run_stats`, merge and clear the route metadata:

```python
adapter_stats = _adapter_route_stats.pop(worker_id, {})
run_stats.update(adapter_stats)
run_stats["route"] = run_stats.get("route") or "agent"
```

For adapter-owned submit, `_maybe_greenhouse_apply()` should set `_last_run_stats[worker_id]` before returning:

```python
_last_run_stats[worker_id] = {
    "route": "adapter_submit:greenhouse",
    "adapter_name": "greenhouse",
    "adapter_plan_ready": True,
    "failure_class": None if status == "applied" else "adapter_no_confirmation",
    "application_tool_calls": 0,
    "tool_calls_total": 0,
    "last_tool": "greenhouse_adapter",
}
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_greenhouse_submit.py tests/test_apply_agent_selection.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/apply/launcher.py tests/test_greenhouse_submit.py tests/test_apply_agent_selection.py
git commit -m "feat(apply): tag greenhouse adapter routes"
```

Expected: commit succeeds.

---

### Task 9: Verification Sweep

**Files:**
- No new files.

- [ ] **Step 1: Run phase-focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_cost_quality_report.py tests/test_apply_cost_report_cli.py tests/test_apply_failure_classification.py tests/test_host_policy.py tests/test_greenhouse_submit.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run fleet integration tests touched by this plan**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests/test_fleet_v3_schema.py tests/test_fleet_v3_governor_queue.py tests/test_fleet_v3_sync.py tests/test_fleet_v3_worker.py tests/test_apply_usage_limit_requeue.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run live scoreboard command**

Run:

```powershell
.\.conda-env\Scripts\applypilot.exe apply-cost-report
```

Expected:

- Output includes `Fleet Cost`, `Local History`, `ATS History`, and `Failure Buckets`.
- `Fleet Cost` reports nonzero applied count when the live fleet DB is reachable.
- If the fleet DB is unreachable, the command exits nonzero with the psycopg connection error.

- [ ] **Step 4: Run whitespace check**

Run:

```powershell
git diff --check
```

Expected: no output.

- [ ] **Step 5: Review git status**

Run:

```powershell
git status --short
```

Expected: only files intentionally modified by this phase appear. Existing unrelated dirty files from before this plan may still appear; do not revert them.
