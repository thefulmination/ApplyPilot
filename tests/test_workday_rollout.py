import sqlite3
from types import SimpleNamespace

import pytest

from applypilot import config
from applypilot.fleet.rollout import (
    ExpansionThresholds, RolloutRecord, evaluate_expansion, fresh_workday_candidates,
    consume_canary_approval, issue_canary_approval, latest_review_ready_urls, persist_record,
    require_review_ready_canary,
    run_canary_prepare, run_supervised_canary, run_workday_shadow,
    select_workday_candidates,
)
from applypilot.fleet.workday_rollout_main import (
    _canonical_resume_for_job, _enter_application, _resume_pdf_path, _tenant_executor,
    validate_canary_requirements,
)


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
        confirm_submit=True,
    )
    assert len(records) == 5
    assert records[0].reason == "tenant_not_validated"
    assert records[1].reason == "observer_not_present"
    assert calls == [True, True, True]


def test_supervised_canary_never_submits_without_explicit_confirmation():
    jobs = _jobs(2)
    calls = []
    records = run_supervised_canary(
        jobs,
        lambda job, submit: calls.append(submit) or
        SimpleNamespace(status="applied", reason="positive_confirmation", metadata={}),
        observer_approve=lambda job: True,
    )
    assert calls == []
    assert [record.reason for record in records] == [
        "submit_confirmation_required", "submit_confirmation_required"
    ]


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


def test_workday_resume_resolution_requires_tailored_or_explicit_base_resume(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_BASE_RESUME", raising=False)
    assert _resume_pdf_path({}) is None

    resume_txt = tmp_path / "resume.txt"
    resume_pdf = tmp_path / "resume.pdf"
    resume_txt.write_text("resume", encoding="utf-8")
    resume_pdf.write_bytes(b"pdf")
    monkeypatch.setattr(config, "RESUME_PATH", resume_txt)
    monkeypatch.setattr(config, "RESUME_PDF_PATH", resume_pdf)
    monkeypatch.setenv("APPLYPILOT_BASE_RESUME", "1")
    assert _resume_pdf_path({}) == str(resume_pdf)


def test_workday_canonical_resume_prefers_base_facts_over_tailored_omissions(tmp_path, monkeypatch):
    base = tmp_path / "base.txt"
    tailored = tmp_path / "tailored.txt"
    base.write_text(
        "Work Experience\nAcme Capital, Analyst January 2020 - Present\n"
        "Led strategy.\nEDUCATION\nState University January 2016 - May 2020\n"
        "Bachelor of Science: Economics\n",
        encoding="utf-8",
    )
    tailored.write_text("Tailored summary only\n", encoding="utf-8")
    monkeypatch.setattr(config, "RESUME_PATH", base)
    monkeypatch.setattr(config, "resolve_resume_stem", lambda _path: str(tailored))
    profile = {
        "resume_facts": {
            "preserved_companies": ["Acme Capital"],
            "preserved_school": "State University",
            "work_locations": {"Acme Capital": "Hicksville, NY"},
            "education": {"school": "State University", "location": "Hoboken, NJ"},
        }
    }

    canonical = _canonical_resume_for_job(profile, {"tailored_resume_path": "tailored.pdf"})

    assert canonical["work_history"][0]["company"] == "Acme Capital"
    assert canonical["work_history"][0]["location"] == "Hicksville, NY"


def test_workday_tenant_executor_parks_before_browser_without_resume(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_BASE_RESUME", raising=False)
    monkeypatch.setattr(
        "applypilot.fleet.workday_rollout_main.launch_chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser launched")),
    )
    execute = _tenant_executor({}, headless=True)
    result = execute({"url": "https://example.myworkdayjobs.com/job/1"}, submit=False)
    assert result.status == "parked"
    assert result.reason == "missing_resume_pdf"


def test_canary_submission_requires_observer_token_and_explicit_confirmation():
    with pytest.raises(ValueError, match="observer-present"):
        validate_canary_requirements(
            observer_present=False, approval_token="token", confirm_submit=True
        )
    with pytest.raises(ValueError, match="approval-token"):
        validate_canary_requirements(
            observer_present=True, approval_token=None, confirm_submit=True
        )
    with pytest.raises(ValueError, match="confirm-submit"):
        validate_canary_requirements(
            observer_present=True, approval_token="token", confirm_submit=False
        )
    validate_canary_requirements(
        observer_present=True, approval_token="token", confirm_submit=True
    )


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


def test_prepare_records_do_not_dilute_rollout_quality_metrics():
    rows = [
        RolloutRecord("workday_shadow", f"s{i}", "t", "dry_run", "review_ready", cost_usd=.10)
        for i in range(10)
    ]
    rows += [
        RolloutRecord("workday_canary", f"c{i}", "t", "applied",
                      "positive_confirmation", cost_usd=.10, observed=True)
        for i in range(5)
    ]
    rows += [
        RolloutRecord("workday_canary_prepare", f"p{i}", "t", "parked", "unmapped",
                      cost_usd=0, false_success=True)
        for i in range(20)
    ]
    decision = evaluate_expansion(rows)
    assert decision.allowed is True
    assert decision.metrics["exception_rate"] == 0.0
    assert decision.metrics["average_cost_usd"] == pytest.approx(.10)
    assert decision.metrics["false_success"] == 0
    assert decision.metrics["prepare_count"] == 20
    assert decision.metrics["prepare_review_ready_count"] == 0


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
    monkeypatch.setattr(
        "applypilot.fleet.rollout.latest_review_ready_urls",
        lambda *_a, **_k: [],
    )
    result = fresh_workday_candidates(
        object(), limit=2,
        probe_fn=lambda url: ("dead", "gone") if url.endswith("/0") else ("live", "api"),
    )
    assert [row["url"] for row in result] == [candidates[1]["url"], candidates[2]["url"]]


def test_fresh_canary_candidates_prioritize_review_ready_evidence(monkeypatch):
    candidates = _jobs(3)
    ready_url = candidates[2]["url"]
    monkeypatch.setattr(
        "applypilot.fleet.rollout.select_workday_candidates",
        lambda *_a, **_k: candidates,
    )
    monkeypatch.setattr(
        "applypilot.fleet.rollout.latest_review_ready_urls",
        lambda *_a, **_k: [ready_url],
    )
    result = fresh_workday_candidates(
        object(), limit=1, canary=True, probe_fn=lambda url: ("live", "api")
    )
    assert [row["url"] for row in result] == [ready_url]


def test_fresh_canary_candidates_skip_unvalidated_tenants(monkeypatch):
    candidates = _jobs(2)
    candidates[0]["tenant_validated"] = False
    monkeypatch.setattr(
        "applypilot.fleet.rollout.select_workday_candidates",
        lambda *_a, **_k: candidates,
    )
    monkeypatch.setattr(
        "applypilot.fleet.rollout.latest_review_ready_urls",
        lambda *_a, **_k: [],
    )
    probed = []

    def probe(url):
        probed.append(url)
        return "live", "api"

    result = fresh_workday_candidates(object(), limit=1, canary=True, probe_fn=probe)

    assert [row["url"] for row in result] == [candidates[1]["url"]]
    assert probed == [candidates[1]["url"]]


def test_fresh_canary_candidates_allow_validated_workday_403_for_browser_prepare(monkeypatch):
    candidates = _jobs(2)
    candidates[0]["tenant_validated"] = True
    candidates[1]["tenant_validated"] = True
    monkeypatch.setattr(
        "applypilot.fleet.rollout.select_workday_candidates",
        lambda *_a, **_k: candidates,
    )
    monkeypatch.setattr(
        "applypilot.fleet.rollout.latest_review_ready_urls",
        lambda *_a, **_k: [],
    )

    result = fresh_workday_candidates(
        object(),
        limit=1,
        canary=True,
        probe_fn=lambda _url: ("uncertain", "workday_blocked_403"),
    )

    assert [row["url"] for row in result] == [candidates[0]["url"]]
    assert result[0]["liveness_reason"] == "workday_blocked_403"


def test_candidate_selection_keeps_unchecked_liveness_for_fresh_probe():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT, application_url TEXT, title TEXT, company TEXT,
            tailored_resume_path TEXT, source_board TEXT, site TEXT,
            liveness_status TEXT, apply_status TEXT, audit_score REAL,
            fit_score REAL, discovered_at TEXT
        )
    """)
    rows = [
        ("u-null", "https://a.myworkdayjobs.com/1", "A", "A", None, "hiringcafe", "A", None, None, 9, 9, ""),
        ("u-uncertain", "https://b.myworkdayjobs.com/2", "B", "B", None, "hiringcafe", "B", "uncertain", None, 8, 8, ""),
        ("u-dead", "https://c.myworkdayjobs.com/3", "C", "C", None, "hiringcafe", "C", "dead", None, 10, 10, ""),
    ]
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    result = select_workday_candidates(conn, limit=5)
    assert [row["url"] for row in result] == ["u-null", "u-uncertain"]


def test_candidate_selection_keeps_hiringcafe_workday_hosts_for_approximation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT, application_url TEXT, title TEXT, company TEXT,
            tailored_resume_path TEXT, source_board TEXT, site TEXT,
            liveness_status TEXT, apply_status TEXT, audit_score REAL,
            fit_score REAL, discovered_at TEXT
        )
    """)
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("u-mufg", "https://mufgub.wd3.myworkdayjobs.com/1", "A", "A", None,
         "hiringcafe", "A", None, None, 10, 10, ""),
        ("u-acme", "https://acme.myworkdayjobs.com/2", "B", "B", None,
         "hiringcafe", "B", None, None, 9, 9, ""),
    ])
    result = select_workday_candidates(conn, limit=5)
    assert [row["url"] for row in result] == ["u-mufg", "u-acme"]


def test_canary_candidate_selection_prioritizes_latest_review_ready_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT, application_url TEXT, title TEXT, company TEXT,
            tailored_resume_path TEXT, source_board TEXT, site TEXT,
            liveness_status TEXT, apply_status TEXT, audit_score REAL,
            fit_score REAL, discovered_at TEXT
        )
    """)
    conn.execute("CREATE TABLE ats_tenants (host TEXT, status TEXT, session_state TEXT)")
    conn.execute("INSERT INTO ats_tenants VALUES ('ready.myworkdayjobs.com', 'supervised', 'ready')")
    conn.execute("INSERT INTO ats_tenants VALUES ('high.myworkdayjobs.com', 'supervised', 'ready')")
    conn.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("u-ready", "https://ready.myworkdayjobs.com/1", "Ready", "Ready", None, "hiringcafe", "Ready", None, None, 1, 1, ""),
            ("u-high", "https://high.myworkdayjobs.com/2", "High", "High", None, "hiringcafe", "High", "live", None, 99, 99, ""),
        ],
    )
    persist_record(conn, RolloutRecord(
        "workday_canary_prepare", "https://ready.myworkdayjobs.com/1", "ready.myworkdayjobs.com",
        "dry_run", "review_ready",
    ))
    result = select_workday_candidates(conn, limit=2, canary=True)
    assert [row["url"] for row in result] == ["u-ready", "u-high"]


def test_canary_candidate_selection_excludes_jobs_with_prior_canary_runs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT, application_url TEXT, title TEXT, company TEXT,
            tailored_resume_path TEXT, source_board TEXT, site TEXT,
            liveness_status TEXT, apply_status TEXT, audit_score REAL,
            fit_score REAL, discovered_at TEXT
        )
    """)
    conn.execute("CREATE TABLE ats_tenants (host TEXT, status TEXT, session_state TEXT)")
    conn.execute("INSERT INTO ats_tenants VALUES ('ready.myworkdayjobs.com', 'supervised', 'ready')")
    conn.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("u-old", "https://ready.myworkdayjobs.com/old", "Old", "Old", None, "hiringcafe", "Old", None, None, 99, 99, ""),
            ("u-new", "https://ready.myworkdayjobs.com/new", "New", "New", None, "hiringcafe", "New", None, None, 1, 1, ""),
        ],
    )
    persist_record(conn, RolloutRecord(
        "workday_canary", "https://ready.myworkdayjobs.com/old",
        "ready.myworkdayjobs.com", "no_confirmation", "submit_clicked_without_confirmation",
        observed=True,
    ))

    result = select_workday_candidates(conn, limit=2, canary=True)

    assert [row["url"] for row in result] == ["u-new"]


def test_canary_candidate_selection_excludes_latest_failed_prepare():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT, application_url TEXT, title TEXT, company TEXT,
            tailored_resume_path TEXT, source_board TEXT, site TEXT,
            liveness_status TEXT, apply_status TEXT, audit_score REAL,
            fit_score REAL, discovered_at TEXT
        )
    """)
    conn.execute("CREATE TABLE ats_tenants (host TEXT, status TEXT, session_state TEXT)")
    conn.execute("INSERT INTO ats_tenants VALUES ('ready.myworkdayjobs.com', 'supervised', 'ready')")
    conn.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("u-failed", "https://ready.myworkdayjobs.com/failed", "Failed", "Failed", None, "hiringcafe", "Failed", None, None, 99, 99, ""),
            ("u-new", "https://ready.myworkdayjobs.com/new", "New", "New", None, "hiringcafe", "New", None, None, 1, 1, ""),
        ],
    )
    persist_record(conn, RolloutRecord(
        "workday_canary_prepare", "https://ready.myworkdayjobs.com/failed",
        "ready.myworkdayjobs.com", "parked", "unmapped_required_fields",
    ))

    result = select_workday_candidates(conn, limit=2, canary=True)

    assert [row["url"] for row in result] == ["u-new"]

    retried = select_workday_candidates(
        conn, limit=2, canary=True, retry_failed_prepares=True
    )
    assert [row["url"] for row in retried] == ["u-failed", "u-new"]


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


def test_canary_approval_requires_five_fresh_review_ready_jobs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    jobs = [{"url": f"https://job/{index}", "tenant_validated": True} for index in range(5)]
    for job in jobs[:4]:
        persist_record(conn, RolloutRecord(
            "workday_canary_prepare", job["url"], "tenant", "dry_run", "review_ready"
        ))
    with pytest.raises(ValueError, match=r"insufficient_review_ready_jobs:4/5"):
        require_review_ready_canary(conn, jobs)
    persist_record(conn, RolloutRecord(
        "workday_canary_prepare", jobs[4]["url"], "tenant", "dry_run", "review_ready"
    ))
    assert require_review_ready_canary(conn, jobs) == [job["url"] for job in jobs]


def test_canary_approval_rejects_unvalidated_ready_jobs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    jobs = [{"url": f"https://job/{index}", "tenant_validated": index != 4} for index in range(5)]
    for job in jobs:
        persist_record(conn, RolloutRecord(
            "workday_canary_prepare", job["url"], "tenant", "dry_run", "review_ready"
        ))
    with pytest.raises(ValueError, match=r"insufficient_review_ready_jobs:4/5"):
        require_review_ready_canary(conn, jobs)
