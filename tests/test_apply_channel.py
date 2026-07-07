"""Apply-time channel recorder: classify a LinkedIn apply as Easy-Apply (stayed on
linkedin.com) vs external (redirected to an off-LinkedIn ATS) from the browser tabs
the agent ended on. Zero LinkedIn scraping -- it reads tabs the apply already opened.
"""
import datetime as dt

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


def test_make_apply_fn_falls_back_to_transcript_channel_on_auth_required(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()

    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id: proc)
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


def test_make_apply_fn_retries_auth_gated_browser_failure_with_prearmed_otp(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    cleaned = []
    calls = []
    prearm_worker_ids = []

    monkeypatch.delenv("FLEET_WORKER_ID", raising=False)
    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id: proc)
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
        monkeypatch.setattr(
            launcher,
            "_last_run_stats",
            {3: {"application_tool_calls": 1}},
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
    assert cleaned == [(3, proc)]


def test_make_apply_fn_retries_generic_failure_when_prearmed_otp_arrived(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    proc = object()
    calls = []

    monkeypatch.delenv("FLEET_WORKER_ID", raising=False)
    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: None)
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: True)
    monkeypatch.setattr(launcher, "_prearm_inbox_auth_request", lambda job: 99)
    monkeypatch.setattr(
        launcher,
        "_consume_prearmed_inbox_auth_hint",
        lambda request_id: "magic_link=https://oracle.test/verify?t=abc\nsource=fleet_relay",
    )

    def fake_run_job(job, port, worker_id, model, agent, inbox_auth_hint=None):
        calls.append(inbox_auth_hint)
        monkeypatch.setattr(launcher, "_last_run_stats", {2: {}}, raising=False)
        if len(calls) == 1:
            return "failed:no_result_line", 1.0
        return "applied", 1.0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)

    out = awm.make_apply_fn("sonnet", "codex", slot=2, fleet_worker_id="m4-2")(
        {
            "url": "https://www.indeed.com/viewjob?jk=oracle",
            "application_url": "https://fa-ewji-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/preview/83866",
        }
    )

    assert calls == [
        None,
        "magic_link=https://oracle.test/verify?t=abc\nsource=fleet_relay",
    ]
    assert out["run_status"] == "applied"


def test_make_apply_fn_oracle_relay_retry_is_db_backed(fleet_db, monkeypatch):
    from applypilot.apply import chrome, launcher, pgqueue
    from applypilot.fleet import apply_worker_main as awm, otp_relay, schema as fleet_schema
    from applypilot.mail_source import MailMessage

    class _FakeSource:
        def fetch(self, *, since_days, max_messages):
            when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
            return [
                MailMessage(
                    id="oracle-message-1",
                    thread_id="oracle-thread-1",
                    subject="Confirm your identity",
                    sender="Oracle <no-reply@oracle.com>",
                    date=_rfc(when),
                    body=(
                        "You must confirm your identity using the one-time pass code : "
                        "246810. This code will expire in 10 minutes."
                    ),
                )
            ]

    proc = object()
    calls = []

    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_POSTRUN_TIMEOUT", "2")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_POLL_SECONDS", "0.01")
    monkeypatch.setattr("applypilot.mail_source.get_mail_source", lambda: _FakeSource())
    monkeypatch.setattr(chrome, "launch_chrome", lambda worker_id, **kwargs: proc)
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: None)
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])

    def fake_run_job(job, port, worker_id, model, agent, inbox_auth_hint=None):
        calls.append(inbox_auth_hint)
        monkeypatch.setattr(launcher, "_last_run_stats", {1: {}}, raising=False)
        if len(calls) == 1:
            with pgqueue.connect(fleet_db) as conn:
                fleet_schema.ensure_schema_v3(conn)
                assert otp_relay.answer_pending(conn) == 1
            return "failed:no_result_line", 1.0
        return "applied", 1.0

    monkeypatch.setattr(launcher, "run_job", fake_run_job)
    job = {
        "url": "https://www.indeed.com/viewjob?jk=oracle",
        "application_url": "https://fa-ewji-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/preview/83866",
    }

    out = awm.make_apply_fn("sonnet", "codex", slot=1, fleet_worker_id="m4-1")(job)

    assert calls == [None, "code=246810\nsource=fleet_relay"]
    assert out["run_status"] == "applied"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, sender_hint, matched_email_ts, answered_at, consumed_at, "
                "code IS NULL AS code_cleared "
                "FROM otp_request WHERE worker_id='m4-1'"
            )
            row = cur.fetchone()
    assert row["sender_hint"].endswith("oraclecloud.com")
    assert row["matched_email_ts"] is not None
    assert row["answered_at"] is not None
    assert row["consumed_at"] is not None
    assert row["code_cleared"] is True


def _rfc(when: dt.datetime) -> str:
    from email.utils import format_datetime

    return format_datetime(when)
