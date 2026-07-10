"""Apply-time channel recorder: classify a LinkedIn apply as Easy-Apply (stayed on
linkedin.com) vs external (redirected to an off-LinkedIn ATS) from the browser tabs
the agent ended on. Zero LinkedIn scraping -- it reads tabs the apply already opened.
"""
import os

import pytest

from applypilot.fleet.apply_worker_main import classify_apply_channel, classify_apply_channel_from_text


def test_all_linkedin_tabs_is_easy_apply():
    urls = ["https://www.linkedin.com/jobs/view/123", "https://www.linkedin.com/feed/"]
    assert classify_apply_channel(urls) == {"apply_channel": "easy_apply", "apply_external_host": None}


def test_external_ats_tab_is_external_with_base_host():
    # the Ambience case: a linkedin job tab + the Ashby apply tab it redirected to
    urls = ["https://www.linkedin.com/jobs/view/4371659280",
            "https://jobs.ashbyhq.com/ambiencehealthcare/e3689211-e008-48f7-b82d-c2cea4e283d3"]
    assert classify_apply_channel(urls) == {"apply_channel": "external", "apply_external_host": "ashbyhq.com"}


def test_greenhouse_only_is_external_base_host():
    assert classify_apply_channel(["https://boards.greenhouse.io/acme/jobs/1"]) == {
        "apply_channel": "external", "apply_external_host": "greenhouse.io"}


def test_linkedin_safety_redirect_is_not_external():
    # LinkedIn wraps external links in linkedin.com/safety/go -- that host is still linkedin
    urls = ["https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.ashbyhq.com%2Fx"]
    assert classify_apply_channel(urls)["apply_channel"] == "easy_apply"


def test_junk_tabs_ignored():
    urls = ["about:blank", "chrome://newtab/", "", "https://www.linkedin.com/jobs/view/1"]
    assert classify_apply_channel(urls) == {"apply_channel": "easy_apply", "apply_external_host": None}


def test_no_informative_tabs_is_unknown():
    assert classify_apply_channel(["about:blank", "chrome://newtab/"]) == {
        "apply_channel": None, "apply_external_host": None}
    assert classify_apply_channel([]) == {"apply_channel": None, "apply_external_host": None}


def test_transcript_external_ats_host_is_external():
    transcript = """
    Amazon opened in a new tab. The Amazon application requires an Amazon.jobs
    login at `passport.amazon.jobs`.
    RESULT:AUTH_REQUIRED
    """

    assert classify_apply_channel_from_text(transcript) == {
        "apply_channel": "external",
        "apply_external_host": "amazon.jobs",
    }


def test_transcript_linkedin_sso_wall_is_not_external():
    transcript = """
    The LinkedIn job page triggered a Google SSO login redirect, and I'm now on
    `accounts.google.com`; LinkedIn requires authentication to apply.
    RESULT:AUTH_REQUIRED
    """

    assert classify_apply_channel_from_text(transcript) == {
        "apply_channel": None,
        "apply_external_host": None,
    }


def test_make_apply_fn_records_external_channel_on_auth_required(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    cleaned = []

    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: cleaned.append((worker_id, process)))
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda job, port, worker_id, model, agent: ("auth_required", 1.0),
    )
    monkeypatch.setattr(launcher, "_last_run_stats", {3: {}}, raising=False)
    monkeypatch.setattr(
        awm,
        "_cdp_page_urls",
        lambda port: [
            "https://www.linkedin.com/jobs/view/4402796040",
            "https://wd5.myworkdaysite.com/pimco/d/job",
        ],
    )

    out = awm.make_apply_fn("sonnet", "codex", slot=3)({"url": "li"})

    assert out["run_status"] == "auth_required"
    assert out["apply_channel"] == "external"
    assert out["apply_external_host"] == "myworkdaysite.com"
    assert cleaned == [(3, proc)]


def test_make_apply_fn_falls_back_to_transcript_channel_on_auth_required(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()

    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: None)
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda job, port, worker_id, model, agent: ("auth_required", 1.0),
    )
    monkeypatch.setattr(
        launcher,
        "_last_run_stats",
        {
            3: {
                "transcript": (
                    "Amazon's posting is active. The Amazon application requires "
                    "an Amazon.jobs login at `passport.amazon.jobs`. RESULT:AUTH_REQUIRED"
                )
            }
        },
        raising=False,
    )
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])

    out = awm.make_apply_fn("sonnet", "codex", slot=3)({"url": "li"})

    assert out["run_status"] == "auth_required"
    assert out["apply_channel"] == "external"
    assert out["apply_external_host"] == "amazon.jobs"


def test_make_apply_fn_launches_headless_on_linux_without_display(monkeypatch):
    import platform

    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    launch_kwargs = []

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def fake_launch(worker_id, **kwargs):
        launch_kwargs.append(kwargs)
        return proc

    monkeypatch.setattr(chrome, "launch_chrome", fake_launch)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: None)
    monkeypatch.setattr(launcher, "run_job", lambda job, port, worker_id, model, agent: ("expired", 1.0))
    monkeypatch.setattr(launcher, "_last_run_stats", {3: {}}, raising=False)
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: False)

    out = awm.make_apply_fn("sonnet", "codex", slot=3)({"url": "https://example.test/job"})

    assert out["run_status"] == "expired"
    assert launch_kwargs == [{"headless": True}]


def test_make_apply_fn_retries_auth_gated_browser_failure_with_prearmed_otp(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    cleaned = []
    calls = []
    prearm_worker_ids = []

    monkeypatch.delenv("FLEET_WORKER_ID", raising=False)
    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: cleaned.append((worker_id, process)))
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: True)

    def fake_prearm(job):
        prearm_worker_ids.append(__import__("os").environ.get("FLEET_WORKER_ID"))
        return 42

    monkeypatch.setattr(launcher, "_prearm_inbox_auth_request", fake_prearm)
    monkeypatch.setattr(
        launcher,
        "_consume_prearmed_inbox_auth_hint",
        lambda request_id: "code=246810\nsource=fleet_relay" if request_id == 42 else None,
    )

    def fake_run_job(job, port, worker_id, model, agent, inbox_auth_hint=None):
        calls.append(inbox_auth_hint)
        attempt = len(calls)
        monkeypatch.setattr(
            launcher,
            "_last_run_stats",
            {
                3: {
                    "application_tool_calls": 0 if attempt == 1 else 1,
                    "tool_calls_total": 0 if attempt == 1 else 1,
                    "safe_requeue": True if attempt == 1 else None,
                    "cost_usd": 1.25 if attempt == 1 else 0.50,
                    "route": "agent",
                }
            },
            raising=False,
        )
        if len(calls) == 1:
            return "failed:browser_tool_unavailable", 1.0
        return "applied", 1.0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    out = awm.make_apply_fn("sonnet", "codex", slot=3, fleet_worker_id="mint-0")(
        {
            "url": "https://www.indeed.com/viewjob?jk=933e088d7448c68e",
            "application_url": "https://fa-ewji-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/preview/83866",
        }
    )

    assert prearm_worker_ids == ["mint-0"]
    assert calls == [None, "code=246810\nsource=fleet_relay"]
    assert out["run_status"] == "applied"
    assert out["est_cost_usd"] == 1.75
    assert out["application_tool_calls"] == 1
    assert out["tool_calls_total"] == 1
    assert out["result_metadata"]["attempt_count"] == 2
    assert out["result_metadata"]["prior_attempt_cost_usd"] == 1.25
    assert cleaned == [(3, proc)]
    assert "FLEET_WORKER_ID" not in os.environ


def test_make_apply_fn_does_not_otp_retry_after_application_touch(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    calls = []
    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: None)
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: True)
    monkeypatch.setattr(launcher, "_prearm_inbox_auth_request", lambda job: 42)
    monkeypatch.setattr(
        launcher,
        "_consume_prearmed_inbox_auth_hint",
        lambda request_id: "code=246810\nsource=fleet_relay",
    )

    def fake_run_job(job, port, worker_id, model, agent, inbox_auth_hint=None):
        calls.append(inbox_auth_hint)
        monkeypatch.setattr(
            launcher,
            "_last_run_stats",
            {
                3: {
                    "application_tool_calls": 1,
                    "tool_calls_total": 1,
                    "safe_requeue": False,
                    "cost_usd": 1.25,
                    "route": "agent",
                }
            },
            raising=False,
        )
        return "failed:timeout", 1.0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    out = awm.make_apply_fn("sonnet", "codex", slot=3, fleet_worker_id="mint-0")(
        {"url": "https://example.test/job"}
    )

    assert calls == [None]
    assert out["run_status"] == "failed:timeout"
    assert out["est_cost_usd"] == 1.25


def test_make_apply_fn_restores_worker_id_when_chrome_launch_fails(monkeypatch):
    from applypilot.apply import chrome
    from applypilot.fleet import apply_worker_main as awm

    monkeypatch.setenv("FLEET_WORKER_ID", "prior-worker")
    monkeypatch.setattr(
        chrome,
        "launch_chrome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")),
    )

    apply_fn = awm.make_apply_fn(
        "sonnet",
        "codex",
        slot=3,
        fleet_worker_id="mint-0",
    )

    with pytest.raises(RuntimeError, match="launch failed"):
        apply_fn({"url": "https://example.test/job"})

    assert os.environ["FLEET_WORKER_ID"] == "prior-worker"
