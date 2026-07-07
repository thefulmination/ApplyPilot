from decimal import Decimal
import sqlite3

from applypilot.fleet.cost_quality_report import (
    CostQualityReport,
    FailureBucket,
    FleetQueueSummary,
    LocalJobsSummary,
    classify_ats,
    classify_failure_bucket,
    default_sqlite_path,
    fetch_local_job_rows,
    render_report_markdown,
    summarize_fleet_queue,
    summarize_local_jobs,
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
