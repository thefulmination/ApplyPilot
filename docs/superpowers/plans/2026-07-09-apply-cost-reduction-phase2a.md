# Apply Cost Reduction Phase 2A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop fleet-v3 workers from paying an apply agent for strongly dead postings and expose append-only route economics in `apply-cost-report`.

**Architecture:** Inject the existing conservative `apply.liveness.probe_url()` into `WorkerLoop` so tests remain network-free and production uses the proven GET-only classifier before `apply_fn`. Extend the cost report with a separately labeled result-event route summary that tolerates a pre-migration database, so the CLI remains usable while Phase 1 is rolled out.

**Tech Stack:** Python 3.11+, pytest, psycopg 3, PostgreSQL, Typer/Rich, existing `apply.liveness`, `fleet.worker`, and `fleet.cost_quality_report` modules.

---

## Scope

This is the first independently deployable slice of
`docs/superpowers/specs/2026-07-09-apply-cost-reduction-phase2-design.md`.

It implements:

- fleet-v3 read-only liveness before browser/agent execution;
- zero-cost durable closure for strong `dead` outcomes;
- fail-open behavior for `live`, `uncertain`, blocked, server, and probe-error outcomes;
- production wiring without a new dependency;
- route-level append-only cost and success reporting;
- backward-compatible reporting before the Phase 1 schema migration.

It does not implement the submit-attempt ledger, independent verifier, Greenhouse production
canary, Ashby adapter, low-cost agent canary, or authenticated-profile work. Those receive
separate plans after this slice passes.

## File structure

- Modify `src/applypilot/fleet/worker.py`
  - Accept an injected `preflight_fn`.
  - Normalize probe results.
  - Close only strong dead outcomes before `apply_fn`.
- Modify `src/applypilot/fleet/apply_worker_main.py`
  - Inject `apply.liveness.probe_url` into production apply workers.
- Modify `src/applypilot/fleet/cost_quality_report.py`
  - Aggregate append-only result events by route.
  - Fetch route rows with pre-migration compatibility.
  - Render a separately labeled route table.
- Modify `tests/test_fleet_v3_worker.py`
  - Cover dead, live, uncertain, and exception preflight behavior.
- Modify `tests/test_cost_quality_report.py`
  - Cover route aggregation and rendering.
- Modify `tests/test_apply_cost_report_cli.py`
  - Preserve CLI coverage with the expanded report object.

---

### Task 1: Add Fleet-v3 Preflight Contract

**Files:**
- Modify: `src/applypilot/fleet/worker.py:188-257`
- Test: `tests/test_fleet_v3_worker.py`

- [ ] **Step 1: Write the failing dead-posting test**

Add a test beside the existing apply worker tests. Reuse the file's queue seeding helpers and
inject both functions so no network or browser runs:

```python
def test_apply_preflight_dead_closes_without_calling_apply_fn(fleet_db):
    calls = []
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "preflight-dead", host="jobs.example")
    loop = WorkerLoop(
        _factory(fleet_db),
        "w-preflight-dead",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda job: calls.append(job) or {"run_status": "applied"},
        preflight_fn=lambda url: ("dead", "http_404"),
    )

    result = loop.run_once()

    assert result == {
        "action": "preflight_dead",
        "url": "preflight-dead",
        "reason": "http_404",
    }
    assert calls == []
    with pgqueue.connect(fleet_db) as conn:
        row = conn.execute(
            "SELECT status::text, apply_status, apply_error, est_cost_usd "
            "FROM apply_queue WHERE url=%s",
            ("preflight-dead",),
        ).fetchone()
        event = conn.execute(
            "SELECT route, failure_class, result_metadata "
            "FROM apply_result_events WHERE url=%s ORDER BY id DESC LIMIT 1",
            ("preflight-dead",),
        ).fetchone()
    assert row == {
        "status": "failed",
        "apply_status": "expired",
        "apply_error": "preflight_http_404",
        "est_cost_usd": 0,
    }
    assert event["route"] == "preflight"
    assert event["failure_class"] == "preflight_dead"
    assert event["result_metadata"] == {
        "preflight_status": "dead",
        "preflight_reason": "http_404",
    }
```

- [ ] **Step 2: Run the dead-posting test to verify it fails**

Run:

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_fleet_v3_worker.py -k preflight_dead -q
```

Expected: FAIL because `WorkerLoop.__init__()` does not accept `preflight_fn`.

- [ ] **Step 3: Add the injected preflight dependency**

In `WorkerLoop.__init__`, add the keyword argument and store it:

```python
preflight_fn: Optional[Callable[[str], tuple[str, str]]] = None,
```

```python
self.preflight_fn = preflight_fn
```

Update the class docstring:

```python
preflight_fn:  INJECTED read-only posting probe. ``url -> (status, reason)``;
               only ``status == 'dead'`` prevents apply execution.
```

- [ ] **Step 4: Add the fail-closed dead branch before `apply_fn`**

In `_tick_apply`, after validating `self.apply_fn` and before calling it:

```python
        if self.preflight_fn is not None:
            try:
                preflight_status, preflight_reason = self.preflight_fn(
                    job.get("application_url") or url
                )
            except Exception as exc:
                preflight_status = "uncertain"
                preflight_reason = f"probe_err:{type(exc).__name__}"
            preflight_status = str(preflight_status or "uncertain").strip().lower()
            preflight_reason = str(preflight_reason or "unknown")[:160]
            if preflight_status == "dead":
                queue.write_apply_result(
                    conn,
                    self.worker_id,
                    url,
                    status="failed",
                    apply_status="expired",
                    apply_error=f"preflight_{preflight_reason}"[:200],
                    est_cost_usd=0.0,
                    target_host=target_host,
                    home_ip=self.home_ip,
                    route="preflight",
                    failure_class="preflight_dead",
                    result_metadata={
                        "preflight_status": "dead",
                        "preflight_reason": preflight_reason,
                    },
                )
                self._record_event(f"wrote apply preflight_dead {url} ({preflight_reason})")
                self._beat(conn, state="idle")
                return {"action": "preflight_dead", "url": url, "reason": preflight_reason}
```

Do not close `live`, `uncertain`, unknown, blocked, or exception outcomes.

- [ ] **Step 5: Run the dead-posting test to verify it passes**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 6: Commit the preflight contract**

```powershell
git add src/applypilot/fleet/worker.py tests/test_fleet_v3_worker.py
git commit -m "feat(fleet): preflight dead apply rows"
```

---

### Task 2: Prove Fail-open Preflight Behavior

**Files:**
- Modify: `tests/test_fleet_v3_worker.py`
- Modify only if tests expose a bug: `src/applypilot/fleet/worker.py`

- [ ] **Step 1: Add parameterized live and uncertain tests**

```python
@pytest.mark.parametrize(
    ("preflight_result", "expected_status"),
    [
        (("live", "gh_api_200"), "live"),
        (("uncertain", "blocked_403"), "uncertain"),
        (("unexpected", "new_probe_state"), "unexpected"),
    ],
)
def test_apply_preflight_non_dead_still_calls_apply_fn(
    fleet_db, preflight_result, expected_status
):
    calls = []
    url = f"preflight-{expected_status}"
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, url, host="jobs.example")
    loop = WorkerLoop(
        _factory(fleet_db),
        f"w-preflight-{expected_status}",
        home_ip="1.2.3.4",
        role="apply",
        preflight_fn=lambda value: preflight_result,
        apply_fn=lambda job: calls.append(job["url"]) or {
            "run_status": "applied",
            "est_cost_usd": 0.25,
        },
    )

    result = loop.run_once()

    assert result["action"] == "applied"
    assert calls == [url]
```

- [ ] **Step 2: Add probe-exception coverage**

```python
def test_apply_preflight_exception_still_calls_apply_fn(fleet_db):
    calls = []
    url = "preflight-probe-error"
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, url, host="jobs.example")

    def fail_probe(_url):
        raise TimeoutError("probe timed out")

    loop = WorkerLoop(
        _factory(fleet_db),
        "w-preflight-error",
        home_ip="1.2.3.4",
        role="apply",
        preflight_fn=fail_probe,
        apply_fn=lambda job: calls.append(job["url"]) or {
            "run_status": "applied",
            "est_cost_usd": 0.25,
        },
    )

    result = loop.run_once()

    assert result["action"] == "applied"
    assert calls == [url]
```

- [ ] **Step 3: Run the new tests**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_fleet_v3_worker.py -k preflight -q
```

Expected: all preflight tests PASS.

- [ ] **Step 4: Run the full worker file**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_fleet_v3_worker.py -q
```

Expected: PASS with no changed legacy apply, captcha, auth, usage-limit, or adapter behavior.

- [ ] **Step 5: Commit fail-open coverage**

```powershell
git add tests/test_fleet_v3_worker.py src/applypilot/fleet/worker.py
git commit -m "test(fleet): cover apply preflight fallthrough"
```

---

### Task 3: Wire the Existing Liveness Probe in Production

**Files:**
- Modify: `src/applypilot/fleet/apply_worker_main.py:167-226`
- Modify: `tests/test_apply_worker_slot.py` or create `tests/test_apply_worker_preflight.py`

- [ ] **Step 1: Add a failing production-wiring test**

Create `tests/test_apply_worker_preflight.py` if the existing slot test is intentionally narrow:

```python
def test_build_apply_loop_injects_existing_liveness_probe(monkeypatch):
    from applypilot.apply import liveness
    from applypilot.fleet import apply_worker_main

    captured = {}

    class FakeLoop:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(apply_worker_main, "_setup_apply_env", lambda: None)
    monkeypatch.setattr(apply_worker_main, "_apply_timeout_override", lambda dsn: None)
    monkeypatch.setattr("applypilot.fleet.worker.WorkerLoop", FakeLoop)
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: None)
    monkeypatch.setattr(apply_worker_main, "make_apply_fn", lambda *a, **k: object())
    monkeypatch.setattr(apply_worker_main, "make_log_tail_fn", lambda *a, **k: None)

    apply_worker_main.build_apply_loop(
        dsn="postgresql://example",
        worker_id="w0",
        home_ip="1.2.3.4",
    )

    assert captured["preflight_fn"] is liveness.probe_url
```

Use direct module monkeypatches instead of string targets where the current test style does so.

- [ ] **Step 2: Run the wiring test to verify it fails**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_apply_worker_preflight.py -q
```

Expected: FAIL because `build_apply_loop()` does not pass `preflight_fn`.

- [ ] **Step 3: Inject `probe_url` in `build_apply_loop`**

Use the existing module; add no dependency:

```python
    from applypilot.apply import liveness, pgqueue
```

```python
    return WorkerLoop(
        lambda: pgqueue.connect(dsn),
        worker_id,
        home_ip=home_ip,
        role="apply",
        apply_fn=make_apply_fn(model, agent, slot),
        preflight_fn=liveness.probe_url,
        machine_owner=machine_owner,
        log_tail_fn=make_log_tail_fn(slot),
    )
```

- [ ] **Step 4: Run production-wiring and liveness tests**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_apply_worker_preflight.py tests/test_apply_preflight_liveness.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit production wiring**

```powershell
git add src/applypilot/fleet/apply_worker_main.py tests/test_apply_worker_preflight.py
git commit -m "feat(fleet): wire apply liveness preflight"
```

---

### Task 4: Add Append-only Route Economics

**Files:**
- Modify: `src/applypilot/fleet/cost_quality_report.py`
- Modify: `tests/test_cost_quality_report.py`

- [ ] **Step 1: Write failing route aggregation tests**

Add imports for `RouteSummary` and `summarize_result_routes`, then add:

```python
def test_summarize_result_routes_compares_verified_cost():
    rows = [
        {"status": "failed", "route": "preflight", "cost_usd": 0},
        {"status": "applied", "route": "adapter_submit:greenhouse", "cost_usd": 0.05},
        {"status": "failed", "route": "adapter_submit:greenhouse", "cost_usd": 0.03},
        {"status": "applied", "route": "agent", "cost_usd": 0.80},
        {"status": "crash_unconfirmed", "route": None, "cost_usd": 0.20},
    ]

    summary = summarize_result_routes(rows, available=True)

    assert summary.available is True
    assert summary.by_route["preflight"].count == 1
    assert summary.by_route["adapter_submit:greenhouse"].applied == 1
    assert summary.by_route["adapter_submit:greenhouse"].cost_per_applied == 0.08
    assert summary.by_route["agent"].cost_per_applied == 0.80
    assert summary.by_route["unknown"].terminal == 1
```

```python
def test_summarize_result_routes_can_report_pre_migration_unavailable():
    summary = summarize_result_routes([], available=False)
    assert summary == RouteSummary(available=False)
```

- [ ] **Step 2: Run the route tests to verify they fail**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_cost_quality_report.py -k result_routes -q
```

Expected: collection FAIL because route types and functions do not exist.

- [ ] **Step 3: Add the route summary type and pure aggregator**

In `cost_quality_report.py`:

```python
@dataclass
class RouteSummary:
    available: bool = True
    by_route: dict[str, CountCost] = field(default_factory=dict)
```

Add a defaulted field to `CostQualityReport`:

```python
routes: RouteSummary = field(default_factory=RouteSummary)
```

Add the pure aggregator:

```python
def summarize_result_routes(rows: Iterable[dict], *, available: bool = True) -> RouteSummary:
    by_route: dict[str, CountCost] = {}
    for row in rows:
        status = _status(_get(row, "status", "apply_status"))
        route = str(_get(row, "route") or "unknown").strip().lower()
        cost = _money(_get(row, "cost_usd", "est_cost_usd"))
        is_applied = status == "applied"
        is_terminal = status in TERMINAL_STATUSES
        _add_count_cost(
            by_route,
            route,
            cost=cost,
            is_applied=is_applied,
            is_terminal=is_terminal,
        )
    return RouteSummary(available=available, by_route=dict(sorted(by_route.items())))
```

- [ ] **Step 4: Run route aggregation tests**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Add a schema-compatible event fetch**

Use a distinct connection so an undefined-column error cannot poison the queue fetch transaction:

```python
def fetch_fleet_result_event_rows(pg_dsn: str) -> tuple[list[dict], bool]:
    from psycopg import errors
    from psycopg.rows import dict_row
    import psycopg

    try:
        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT status, route, COALESCE(est_cost_usd, 0) AS est_cost_usd
                FROM apply_result_events
                """
            ).fetchall()
    except (errors.UndefinedColumn, errors.UndefinedTable):
        return [], False
    return list(rows), True
```

In `build_report`, call it once and pass both values:

```python
    event_rows, route_metrics_available = fetch_fleet_result_event_rows(pg_dsn)
    return CostQualityReport(
        fleet=summarize_fleet_queue(fetch_fleet_queue_rows(pg_dsn)),
        local=summarize_local_jobs(fetch_local_job_rows(local_path)),
        routes=summarize_result_routes(
            event_rows,
            available=route_metrics_available,
        ),
    )
```

- [ ] **Step 6: Add fetch compatibility tests**

Add these local fakes and tests in `tests/test_cost_quality_report.py`:

```python
class _FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _FakeEventConnection:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql):
        if self.error is not None:
            raise self.error
        return _FakeRows(self.rows)


def test_fetch_fleet_result_event_rows_returns_rows(monkeypatch):
    expected = {
        "status": "applied",
        "route": "agent",
        "est_cost_usd": Decimal("0.80"),
    }
    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: _FakeEventConnection([expected]),
    )

    assert fetch_fleet_result_event_rows("dsn") == ([expected], True)


def test_fetch_fleet_result_event_rows_tolerates_missing_route_column(monkeypatch):
    from psycopg import errors

    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: _FakeEventConnection(
            error=errors.UndefinedColumn("column route does not exist")
        ),
    )

    assert fetch_fleet_result_event_rows("dsn") == ([], False)
```

- [ ] **Step 7: Run the cost-quality test file**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_cost_quality_report.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit route aggregation**

```powershell
git add src/applypilot/fleet/cost_quality_report.py tests/test_cost_quality_report.py
git commit -m "feat(fleet): report apply route economics"
```

---

### Task 5: Render Route Economics Without Breaking Old Schemas

**Files:**
- Modify: `src/applypilot/fleet/cost_quality_report.py`
- Modify: `tests/test_cost_quality_report.py`
- Modify if fixture construction requires it: `tests/test_apply_cost_report_cli.py`

- [ ] **Step 1: Add failing rendering tests**

```python
def test_render_report_includes_append_only_route_table():
    report = CostQualityReport(
        fleet=FleetQueueSummary(),
        local=LocalJobsSummary(),
        routes=RouteSummary(
            by_route={
                "preflight": CountCost(count=4, terminal=4, cost=0),
                "agent": CountCost(count=2, terminal=2, applied=1, cost=0.75),
            }
        ),
    )

    rendered = render_report_markdown(report)

    assert "## Result Event Routes" in rendered
    assert "Append-only result events" in rendered
    assert "| agent | 2 | 1 | $0.7500 | $0.7500 |" in rendered
    assert "| preflight | 4 | 0 | $0.0000 | n/a |" in rendered
```

```python
def test_render_report_marks_route_metrics_unavailable_before_migration():
    report = CostQualityReport(
        fleet=FleetQueueSummary(),
        local=LocalJobsSummary(),
        routes=RouteSummary(available=False),
    )
    assert "Route metrics unavailable until the home schema migration runs." in render_report_markdown(report)
```

- [ ] **Step 2: Run the rendering tests to verify they fail**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_cost_quality_report.py -k route -q
```

Expected: FAIL because the renderer has no route section.

- [ ] **Step 3: Render a separately labeled append-only table**

Append this section after failure buckets:

```python
    lines.extend(
        [
            "",
            "## Result Event Routes",
            "",
            "Append-only result events; this table is not the canonical queue all-in denominator.",
            "",
        ]
    )
    if not report.routes.available:
        lines.append("Route metrics unavailable until the home schema migration runs.")
    else:
        lines.extend(
            [
                "| Route | Events | Applied | Cost | Cost/applied |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for route, item in report.routes.by_route.items():
            lines.append(
                f"| {route} | {item.count} | {item.applied} | "
                f"{_fmt_money(item.cost)} | "
                f"{_fmt_money(item.cost_per_applied) if item.applied else 'n/a'} |"
            )
        if not report.routes.by_route:
            lines.append("| n/a | 0 | 0 | $0.0000 | n/a |")
```

- [ ] **Step 4: Run report and CLI tests**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest tests/test_cost_quality_report.py tests/test_apply_cost_report_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the live report against the current pre-migration database**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m applypilot.cli apply-cost-report
```

Expected: existing fleet/local tables render, followed by
`Route metrics unavailable until the home schema migration runs.` The command must not fail.

- [ ] **Step 6: Commit rendering and compatibility**

```powershell
git add src/applypilot/fleet/cost_quality_report.py tests/test_cost_quality_report.py tests/test_apply_cost_report_cli.py
git commit -m "feat(cli): show append-only apply route costs"
```

---

### Task 6: Phase 2A Regression and Completion Audit

**Files:**
- Verify only; modify a file only to fix a demonstrated regression.

- [ ] **Step 1: Run focused Phase 2A tests**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest `
  tests/test_fleet_v3_worker.py `
  tests/test_apply_worker_preflight.py `
  tests/test_apply_preflight_liveness.py `
  tests/test_cost_quality_report.py `
  tests/test_apply_cost_report_cli.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the affected fleet/apply regression suite**

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
..\..\.conda-env\python.exe -m pytest `
  tests/test_fleet_v3_schema.py `
  tests/test_fleet_v3_governor_queue.py `
  tests/test_fleet_v3_sync.py `
  tests/test_fleet_v3_worker.py `
  tests/test_apply_failure_classification.py `
  tests/test_apply_usage_limit_requeue.py `
  tests/test_host_policy.py `
  tests/test_greenhouse_submit.py -q
```

Expected: PASS.

- [ ] **Step 3: Run static checks**

```powershell
..\..\.conda-env\python.exe -m ruff check `
  src/applypilot/fleet/worker.py `
  src/applypilot/fleet/apply_worker_main.py `
  src/applypilot/fleet/cost_quality_report.py `
  tests/test_apply_worker_preflight.py `
  tests/test_cost_quality_report.py
git diff --check
```

Expected: no errors.

- [ ] **Step 4: Re-run the live read-only cost report**

Use the command from Task 5 Step 5.

Expected: command exits zero and preserves the canonical queue baseline while route metrics are
either displayed or explicitly unavailable according to migration state.

- [ ] **Step 5: Audit each Phase 2A requirement**

Confirm from current files and test output:

- dead preflight never invokes `apply_fn`;
- non-dead and probe failures invoke `apply_fn`;
- the production builder injects `probe_url`;
- dead outcomes cost zero and persist route/failure metadata;
- route event metrics never replace the queue all-in denominator;
- a pre-migration database does not break `apply-cost-report`;
- no new external dependency was added;
- remote workers still perform no DDL.

- [ ] **Step 6: Commit any final demonstrated fix**

Only if Step 1-5 required a code change:

```powershell
git add <only-the-files-changed-for-the-demonstrated-fix>
git commit -m "fix(fleet): complete phase 2a cost gate"
```

If no fix was required, do not create an empty commit.
