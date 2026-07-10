from decimal import Decimal
import sqlite3

import pytest

from applypilot.fleet import cost_quality_report
from applypilot.fleet.cost_quality_report import (
    build_report,
    CountCost,
    CostQualityReport,
    FailureBucket,
    FleetQueueSummary,
    LocalJobsSummary,
    RouteSummary,
    classify_ats,
    classify_failure_bucket,
    default_sqlite_path,
    fetch_fleet_result_event_rows,
    fetch_local_job_rows,
    render_report_markdown,
    summarize_fleet_queue,
    summarize_local_jobs,
    summarize_result_routes,
)


def test_classify_ats_from_application_url():
    assert classify_ats("https://jobs.ashbyhq.com/example") == "ashby"
    assert classify_ats("https://boards.greenhouse.io/example/jobs/1") == "greenhouse"
    assert classify_ats("https://grnh.se/abcd1234") == "greenhouse"
    assert classify_ats("https://jobs.lever.co/example/abc") == "lever"
    assert classify_ats("https://adobe.wd5.myworkdayjobs.com/external/job/1") == "workday"
    assert classify_ats("https://company.workdayjobs.com/external/job/1") == "workday"
    assert classify_ats("https://jobs.smartrecruiters.com/example/123") == "smartrecruiters"
    assert classify_ats("https://apply.workable.com/example/j/123") == "workable"
    assert classify_ats("https://example.com/apply") == "other"


def test_classify_failure_bucket_is_stable():
    assert (
        classify_failure_bucket("crash_unconfirmed", "failed:no_result_line")
        == "agent_browser_runtime"
    )
    assert (
        classify_failure_bucket("failed", "failed:browser_unavailable")
        == "agent_browser_runtime"
    )
    assert classify_failure_bucket("failed", "expired") == "preflight_or_policy"
    assert (
        classify_failure_bucket("failed", "failed:not_eligible_location")
        == "preflight_or_policy"
    )
    assert (
        classify_failure_bucket("failed", "failed:email_verification_required")
        == "email_auth_related"
    )
    assert classify_failure_bucket("failed", "otp_required") == "email_auth_related"
    assert classify_failure_bucket("failed", "auth_required") == "email_auth_related"
    assert classify_failure_bucket("failed", "login_required") == "email_auth_related"
    assert classify_failure_bucket("blocked", "challenge_pending") == "challenge_related"
    assert classify_failure_bucket("blocked", "captcha_required") == "challenge_related"
    assert classify_failure_bucket("failed", "failed:timeout") == "agent_browser_runtime"
    assert classify_failure_bucket("failed", "already_applied") == "preflight_or_policy"
    assert classify_failure_bucket("failed", "excluded_company") == "preflight_or_policy"
    assert classify_failure_bucket("failed", "failed:no_confirmation") == "other"


def test_summarize_fleet_queue_computes_all_in_cost_per_apply():
    rows = [
        {
            "application_url": "https://jobs.ashbyhq.com/example",
            "status": "applied",
            "cost_usd": Decimal("0.50"),
        },
        {
            "application_url": "https://boards.greenhouse.io/example/jobs/1",
            "status": "applied",
            "cost_usd": Decimal("0.70"),
        },
        {
            "application_url": "https://boards.greenhouse.io/example/jobs/2",
            "status": "failed",
            "apply_error": "expired",
            "cost_usd": Decimal("0.20"),
        },
        {
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "status": "crash_unconfirmed",
            "apply_error": "failed:no_result_line",
            "cost_usd": Decimal("1.10"),
        },
        {
            "application_url": "https://jobs.ashbyhq.com/queued",
            "status": "queued",
            "cost_usd": Decimal("0"),
        },
    ]

    summary = summarize_fleet_queue(rows)

    assert summary.applied == 2
    assert summary.terminal_attempts == 4
    assert summary.queued_or_leased == 1
    assert summary.total_cost_usd == 2.5
    assert summary.cost_per_applied_all_in == 1.25
    assert summary.by_ats["greenhouse"].applied == 1
    assert summary.by_failure_bucket["agent_browser_runtime"].count == 1


def test_summarize_local_jobs_computes_historical_success_rate():
    rows = [
        {
            "url": "https://jobs.ashbyhq.com/example",
            "apply_status": "applied",
        },
        {
            "url": "https://jobs.ashbyhq.com/example-failed",
            "apply_status": "failed",
            "apply_error": "expired",
        },
        {
            "url": "https://boards.greenhouse.io/example/jobs/1",
            "apply_status": "applied",
        },
        {
            "url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "apply_status": "failed",
            "apply_error": "failed:no_confirmation",
        },
    ]

    summary = summarize_local_jobs(rows)

    assert summary.touched == 4
    assert summary.applied == 2
    assert summary.by_ats["ashby"].success_pct == 50.0
    assert summary.by_ats["workday"].success_pct == 0.0


def test_summarize_local_jobs_counts_non_terminal_touched_rows_in_success_rate():
    rows = [
        {
            "url": "https://jobs.ashbyhq.com/example",
            "apply_status": "applied",
        },
        {
            "url": "https://jobs.ashbyhq.com/manual-review",
            "apply_status": "manual",
        },
    ]

    summary = summarize_local_jobs(rows)

    assert summary.by_ats["ashby"].success_pct == 50.0


def test_summarize_result_routes_compares_verified_cost():
    rows = [
        {"status": "failed", "route": "preflight", "cost_usd": 0},
        {
            "status": "applied",
            "route": "adapter_submit:greenhouse",
            "cost_usd": 0.05,
        },
        {
            "status": "failed",
            "route": "adapter_submit:greenhouse",
            "cost_usd": 0.03,
        },
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


def test_summarize_result_routes_can_report_pre_migration_unavailable():
    summary = summarize_result_routes([], available=False)

    assert summary == RouteSummary(available=False)


def test_summarize_result_routes_accepts_grouped_weighted_rows():
    rows = [
        {
            "status": "applied",
            "route": "agent",
            "event_count": 3,
            "est_cost_usd": Decimal("0.30"),
        },
        {
            "status": "failed",
            "route": "agent",
            "event_count": 2,
            "est_cost_usd": Decimal("0.20"),
        },
        {
            "status": "crash_unconfirmed",
            "route": "unknown",
            "event_count": 4,
            "est_cost_usd": Decimal("0.40"),
        },
    ]

    summary = summarize_result_routes(rows)

    assert summary.by_route["agent"].count == 5
    assert summary.by_route["agent"].applied == 3
    assert summary.by_route["agent"].terminal == 5
    assert summary.by_route["agent"].cost == 0.5
    assert summary.by_route["unknown"].count == 4
    assert summary.by_route["unknown"].terminal == 4


def test_summarize_result_routes_rounds_accumulated_cost_to_report_precision():
    summary = summarize_result_routes(
        [
            {"status": "applied", "route": "agent", "cost_usd": 0.10},
            {"status": "failed", "route": "agent", "cost_usd": 0.20},
        ]
    )

    assert summary.by_route["agent"].cost == 0.30


def test_summarize_result_routes_normalizes_whitespace_route_to_unknown():
    summary = summarize_result_routes(
        [{"status": "failed", "route": " \t ", "cost_usd": 0}]
    )

    assert summary.by_route["unknown"].count == 1


class _FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _FakeEventConnection:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.executed_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        self.executed_sql = sql
        if self.error is not None:
            raise self.error
        return _FakeRows(self.rows)


def test_fetch_fleet_result_event_rows_returns_rows(monkeypatch):
    expected = {
        "status": "applied",
        "route": "agent",
        "event_count": 2,
        "est_cost_usd": Decimal("0.80"),
    }
    connection = _FakeEventConnection([expected])
    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    assert fetch_fleet_result_event_rows("dsn") == ([expected], True)
    sql = " ".join(connection.executed_sql.lower().split())
    assert "lower(btrim(status::text)) as status" in sql
    assert "nullif(lower(btrim(route)), '')" in sql
    assert "count(*) as event_count" in sql
    assert "sum(est_cost_usd)" in sql
    assert "group by 1, 2" in sql


def test_fetch_fleet_result_event_rows_tolerates_missing_route_column(monkeypatch):
    from psycopg import errors

    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: _FakeEventConnection(
            error=errors.UndefinedColumn("column route does not exist")
        ),
    )

    assert fetch_fleet_result_event_rows("dsn") == ([], False)


def test_fetch_fleet_result_event_rows_tolerates_missing_table(monkeypatch):
    from psycopg import errors

    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: _FakeEventConnection(
            error=errors.UndefinedTable("table apply_result_events does not exist")
        ),
    )

    assert fetch_fleet_result_event_rows("dsn") == ([], False)


def test_fetch_fleet_result_event_rows_propagates_other_errors(monkeypatch):
    monkeypatch.setattr(
        "psycopg.connect",
        lambda *_args, **_kwargs: _FakeEventConnection(error=RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        fetch_fleet_result_event_rows("dsn")


def test_build_report_fetches_each_source_once_and_includes_routes(monkeypatch):
    calls = {"fleet": 0, "local": 0, "routes": 0}

    def fetch_fleet(_dsn):
        calls["fleet"] += 1
        return [{"status": "applied", "est_cost_usd": 0.5}]

    def fetch_local(_path):
        calls["local"] += 1
        return [{"apply_status": "applied"}]

    def fetch_routes(_dsn):
        calls["routes"] += 1
        return (
            [{"status": "applied", "route": "agent", "est_cost_usd": 0.5}],
            True,
        )

    monkeypatch.setattr(cost_quality_report, "fetch_fleet_queue_rows", fetch_fleet)
    monkeypatch.setattr(cost_quality_report, "fetch_local_job_rows", fetch_local)
    monkeypatch.setattr(
        cost_quality_report, "fetch_fleet_result_event_rows", fetch_routes
    )

    report = build_report(pg_dsn="dsn", sqlite_path="local.db")

    assert calls == {"fleet": 1, "local": 1, "routes": 1}
    assert report.fleet.applied == 1
    assert report.local.applied == 1
    assert report.routes.by_route["agent"].cost_per_applied == 0.5


def test_default_sqlite_path_prefers_applypilot_db_path(monkeypatch, tmp_path):
    db_path = tmp_path / "custom.db"

    monkeypatch.setenv("APPLYPILOT_DB_PATH", str(db_path))

    assert default_sqlite_path() == db_path


def test_fetch_local_job_rows_opens_missing_database_read_only(tmp_path):
    missing_db = tmp_path / "missing.db"

    try:
        fetch_local_job_rows(missing_db)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("missing read-only SQLite database should fail")

    assert not missing_db.exists()


def test_render_report_markdown_includes_local_only_failure_bucket():
    report = CostQualityReport(
        fleet=FleetQueueSummary(),
        local=LocalJobsSummary(
            by_failure_bucket={"email_auth_related": FailureBucket(count=3)}
        ),
    )

    rendered = render_report_markdown(report)

    assert "| email_auth_related | 0 | n/a | 3 |" in rendered


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
    assert "not the canonical queue all-in denominator" in rendered
    assert "| Route | Events | Applied | Cost | Cost/applied |" in rendered
    assert "| agent | 2 | 1 | $0.7500 | $0.7500 |" in rendered
    assert "| preflight | 4 | 0 | $0.0000 | n/a |" in rendered
    assert rendered.index("## Result Event Routes") > rendered.index("## Failure Buckets")


def test_render_report_marks_route_metrics_unavailable_before_migration():
    report = CostQualityReport(
        fleet=FleetQueueSummary(),
        local=LocalJobsSummary(),
        routes=RouteSummary(available=False),
    )

    assert (
        "Route metrics unavailable until the home schema migration runs."
        in render_report_markdown(report)
    )


def test_render_report_includes_na_route_row_when_no_events_exist():
    report = CostQualityReport(
        fleet=FleetQueueSummary(),
        local=LocalJobsSummary(),
        routes=RouteSummary(),
    )

    assert (
        "| n/a | 0 | 0 | $0.0000 | n/a |" in render_report_markdown(report)
    )
