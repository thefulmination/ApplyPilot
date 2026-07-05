"""Apply-time channel recorder: classify a LinkedIn apply as Easy-Apply (stayed on
linkedin.com) vs external (redirected to an off-LinkedIn ATS) from the browser tabs
the agent ended on. Zero LinkedIn scraping -- it reads tabs the apply already opened.
"""
from applypilot.fleet.apply_worker_main import classify_apply_channel


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


def test_make_apply_fn_records_external_channel_on_auth_required(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    cleaned = []

    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id: proc)
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
