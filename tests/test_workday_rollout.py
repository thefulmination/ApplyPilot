import sqlite3
from types import SimpleNamespace

from applypilot.fleet.rollout import (
    ExpansionThresholds, RolloutRecord, evaluate_expansion, fresh_workday_candidates,
    consume_canary_approval, issue_canary_approval, latest_review_ready_urls, persist_record,
    run_canary_prepare, run_supervised_canary, run_workday_shadow,
)
from applypilot.fleet.workday_rollout_main import _enter_application


def _jobs(n):
    return [{"url": f"https://acme.myworkdayjobs.com/job/{i}",
             "target_host": "acme.myworkdayjobs.com", "tenant_validated": True}
            for i in range(n)]


def test_shadow_is_capped_at_ten_and_never_submits():
    calls = []
    records = run_workday_shadow(
        _jobs(15), lambda job, submit: calls.append(submit) or
        SimpleNamespace(status="dry_run", reason="review_ready", metadata={})
    )
    assert len(records) == 10
    assert calls == [False] * 10


def test_canary_is_capped_at_five_and_requires_observer_and_validated_tenant():
    jobs = _jobs(7)
    jobs[0]["tenant_validated"] = False
    calls = []
    records = run_supervised_canary(
        jobs, lambda job, submit: calls.append(submit) or
        SimpleNamespace(status="applied", reason="positive_confirmation", metadata={}),
        observer_approve=lambda job: not job["url"].endswith("/1"),
    )
    assert len(records) == 5
    assert records[0].reason == "tenant_not_validated"
    assert records[1].reason == "observer_not_present"
    assert calls == [True, True, True]


def test_canary_prepare_uses_validated_profiles_and_never_submits():
    jobs = _jobs(6)
    jobs[0]["tenant_validated"] = False
    calls = []
    records = run_canary_prepare(
        jobs, lambda job, submit: calls.append(submit) or
        SimpleNamespace(status="dry_run", reason="review_ready", metadata={}),
    )
    assert len(records) == 5
    assert records[0].reason == "tenant_not_validated"
    assert calls == [False] * 4


def test_expansion_requires_counts_observation_quality_and_cost():
    good = [RolloutRecord("workday_shadow", f"s{i}", "t", "dry_run", "review_ready",
                          cost_usd=.05) for i in range(10)]
    good += [RolloutRecord("workday_canary", f"c{i}", "t", "applied",
                           "positive_confirmation", cost_usd=.10, observed=True) for i in range(5)]
    assert evaluate_expansion(good).allowed is True
    bad = good + [RolloutRecord("workday_canary", "bad", "t", "applied", "claimed",
                                false_success=True, observed=True)]
    decision = evaluate_expansion(bad)
    assert decision.allowed is False
    assert "false_success_threshold" in decision.reasons


def test_expansion_denies_before_required_runs_exist():
    decision = evaluate_expansion([], ExpansionThresholds())
    assert decision.allowed is False
    assert "insufficient_shadow_count" in decision.reasons
    assert "insufficient_canary_count" in decision.reasons


def test_expansion_uses_latest_unique_job_record_and_counts_auth_as_exception():
    rows = [
        RolloutRecord("workday_shadow", "same", "t", "parked", "old"),
        RolloutRecord("workday_shadow", "same", "t", "auth_required", "login"),
    ]
    decision = evaluate_expansion(rows)
    assert decision.metrics["shadow_count"] == 1
    assert decision.metrics["exception_rate"] == 1.0


def test_shadow_executor_enters_application_from_job_detail():
    calls = []

    class Control:
        def wait_for(self, **kwargs): calls.append(("wait", kwargs))
        def count(self): return 1
        def is_visible(self): return True
        def click(self): calls.append("click")
        @property
        def first(self): return self

    class Page:
        def get_by_role(self, role, name):
            calls.append(role)
            return type("Match", (), {"first": Control()})()
        def wait_for_load_state(self, state): calls.append(state)
        def wait_for_url(self, pattern, timeout): calls.append(("url", timeout))
        def locator(self, selector):
            calls.append(("locator", selector))
            return Control()

    assert _enter_application(Page()) is True
    assert calls == [
        "button", ("wait", {"state": "visible", "timeout": 10000}), "click",
        "domcontentloaded", "button",
        ("wait", {"state": "visible", "timeout": 10000}), "click", ("url", 15000),
        "domcontentloaded", ("locator", '[data-automation-id="signInContent"], '
                             '[data-automation-id*="resume" i], input[type="file"]'),
        ("wait", {"state": "attached", "timeout": 20000}),
    ]


def test_fresh_candidate_filter_rejects_stale_live_labels(monkeypatch):
    candidates = _jobs(3)
    monkeypatch.setattr(
        "applypilot.fleet.rollout.select_workday_candidates",
        lambda *_a, **_k: candidates,
    )
    result = fresh_workday_candidates(
        object(), limit=2,
        probe_fn=lambda url: ("dead", "gone") if url.endswith("/0") else ("live", "api"),
    )
    assert [row["url"] for row in result] == [candidates[1]["url"], candidates[2]["url"]]


def test_canary_approval_is_exact_single_use_and_expiring():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    token = issue_canary_approval(conn, ["https://job/1", "https://job/2"], ttl_minutes=10)
    assert consume_canary_approval(conn, token, "https://job/1") is True
    assert consume_canary_approval(conn, token, "https://job/1") is False
    assert consume_canary_approval(conn, token, "https://job/not-approved") is False
    assert consume_canary_approval(conn, token, "https://job/2") is True


def test_canary_approval_rejects_empty_job_batch():
    conn = sqlite3.connect(":memory:")
    try:
        issue_canary_approval(conn, [])
    except ValueError as exc:
        assert str(exc) == "no_review_ready_jobs"
    else:
        raise AssertionError("empty approval batch was accepted")


def test_authorization_only_includes_latest_review_ready_jobs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    persist_record(conn, RolloutRecord(
        "workday_canary_prepare", "https://job/1", "one", "dry_run", "review_ready"
    ))
    persist_record(conn, RolloutRecord(
        "workday_canary_prepare", "https://job/2", "two", "parked", "exception"
    ))
    jobs = [{"url": "https://job/1"}, {"url": "https://job/2"}]
    assert latest_review_ready_urls(conn, jobs) == ["https://job/1"]
