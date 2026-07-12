"""Pre-launch liveness probe (APPLYPILOT_PREFLIGHT_LIVENESS) + apply-time closure feedback.

A closed-but-HTTP-200 posting (role filled/closed but the page still renders) used to
burn a full Chrome launch + agent run (~$1.50) before the agent reached RESULT:EXPIRED.
With the preflight on, the worker HTTP-probes each candidate first and skips a strong-
DEAD posting before launching anything. The probe MUST be conservative: an anonymous
GET to LinkedIn (999) / Cloudflare (403) / a rate-limited host (429) is UNCERTAIN, never
DEAD -- otherwise the preflight would false-skip live jobs. Apply-time closures
(RESULT:EXPIRED) are also fed back as a liveness fact.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L
from applypilot.apply import liveness


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def test_preflight_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_PREFLIGHT_LIVENESS", raising=False)
    assert L._preflight_liveness_enabled() is True
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "0")
    assert L._preflight_liveness_enabled() is False
    monkeypatch.setenv("APPLYPILOT_PREFLIGHT_LIVENESS", "on")
    assert L._preflight_liveness_enabled() is True


def test_stamp_dead_excludes_from_acquire_and_is_retained(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score) VALUES ('https://x/dead', 'Chief of Staff', 'X', 'Acme', "
        "'https://jobs.lever.co/acme/1', 'x', 8, 9.0)")
    conn.commit()
    assert L.acquire_job(min_score=7) is not None  # acquirable before
    L.release_lock("https://x/dead")               # drop the lease acquire just took
    L._stamp_liveness_dead("https://x/dead", "preflight_text:'position has been filled'")
    row = conn.execute(
        "SELECT liveness_status, liveness_reason FROM jobs WHERE url='https://x/dead'").fetchone()
    assert row[0] == "dead" and "filled" in row[1]
    # excluded by the liveness != 'dead' acquire filter, but the row still exists (retained)
    assert L.acquire_job(min_score=7) is None
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE url='https://x/dead'").fetchone()[0] == 1


def test_liveness_write_rejects_verdict_for_replaced_application_url(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score) VALUES ('https://source/job', 'Chief of Staff', 'X', 'Acme', "
        "'https://old.example/job', 'x', 8, 9.0)"
    )
    conn.commit()
    conn.execute(
        "UPDATE jobs SET application_url='https://new.example/job' WHERE url='https://source/job'"
    )
    conn.commit()

    wrote = liveness._write_liveness_results(
        conn,
        [(
            "https://source/job",
            liveness.DEAD,
            "http_404",
            {"_probed_url": "https://old.example/job"},
        )],
    )

    row = conn.execute(
        "SELECT liveness_status, liveness_reason FROM jobs WHERE url='https://source/job'"
    ).fetchone()
    assert wrote == 0
    assert row[0] is None
    assert row[1] is None


def test_verify_jobs_normalizes_zero_workers_to_one(conn, monkeypatch):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, audit_label) VALUES "
        "('https://source/zero-workers', 'Chief of Staff', 'X', 'Acme', "
        "'https://jobs.example/zero-workers', 'x', 8, 9.0, 'priority')"
    )
    conn.commit()
    monkeypatch.setattr(liveness, "probe_url", lambda _url, meta=None: (liveness.LIVE, "ok_200"))

    summary = liveness.verify_jobs(conn, workers=0, max_age_days=0)

    assert summary["checked"] == 1
    assert summary["wrote"] == 1


def test_dead_on_visit_reasons_membership():
    assert "expired" in L.DEAD_ON_VISIT_REASONS
    assert "page_error" not in L.DEAD_ON_VISIT_REASONS  # transient (500/blank), not a closure


# --- probe conservatism: the property that makes the preflight safe -----------

def _patch_fetch(monkeypatch, status, body="", final=None):
    monkeypatch.setattr(liveness, "_fetch",
                        lambda url, accept=None: (status, final or url, body))


def test_probe_dead_on_http_404(monkeypatch):
    _patch_fetch(monkeypatch, 404)
    assert liveness.probe_url("https://careers.example.com/job/1")[0] == liveness.DEAD


def test_probe_dead_on_closure_text(monkeypatch):
    _patch_fetch(monkeypatch, 200, body="<html>This position has been filled. Thank you!</html>")
    assert liveness.probe_url("https://careers.example.com/job/2")[0] == liveness.DEAD


def test_probe_does_not_treat_script_translation_as_visible_closure(monkeypatch):
    body = (
        "<html><head><script>window.messages={missing:'job not found'}</script></head>"
        "<body><h1>Senior Analyst</h1><button>Apply now</button></body></html>"
        + "x" * 200
    )
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url("https://careers.example.com/job/active") == (
        liveness.LIVE,
        "ok_200",
    )


def test_probe_detects_closure_text_split_across_visible_elements(monkeypatch):
    body = "<html><body>This position has <strong>been filled</strong>.</body></html>"
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url("https://careers.example.com/job/closed")[0] == liveness.DEAD


def test_probe_uncertain_on_linkedin_999(monkeypatch):
    # LinkedIn answers anonymous GETs with 999 -> must be UNCERTAIN, never DEAD, or the
    # preflight would false-skip every LinkedIn job.
    _patch_fetch(monkeypatch, 999)
    assert liveness.probe_url("https://www.linkedin.com/jobs/view/3")[0] != liveness.DEAD


def test_probe_uncertain_on_block_and_server_codes(monkeypatch):
    for code in (401, 403, 429, 500, 503):
        _patch_fetch(monkeypatch, code)
        st = liveness.probe_url("https://careers.example.com/job/4")[0]
        assert st == liveness.UNCERTAIN, f"{code} -> {st}, expected uncertain"


def test_probe_live_on_plain_200(monkeypatch):
    _patch_fetch(monkeypatch, 200, body="<html>Apply now for this great role</html>" + "x" * 2000)
    assert liveness.probe_url("https://careers.example.com/job/5")[0] == liveness.LIVE


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "empty_body"),
        ("<html>temporarily unavailable</html>", "thin_body"),
        ("<html><div>Checking your browser</div><span>CF-RAY: x</span></html>", "access_gate:cloudflare"),
        ("<html><div id='px-captcha'>Complete CAPTCHA</div></html>", "access_gate:captcha"),
        ("<html><h1>Access Denied</h1><p>Your request was blocked.</p></html>", "access_gate:access_denied"),
    ],
)
def test_generic_200_without_job_page_evidence_is_explicitly_uncertain(monkeypatch, body, reason):
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url("https://careers.example.com/job/unknown") == (liveness.UNCERTAIN, reason)


def test_generic_sign_in_copy_alone_does_not_trigger_access_gate(monkeypatch):
    body = "<html><h1>Senior Analyst</h1><button>Apply</button><footer>Employee sign in</footer></html>" + "x" * 200
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url("https://careers.example.com/job/valid") == (liveness.LIVE, "ok_200")


def test_background_recaptcha_script_does_not_make_live_page_uncertain(monkeypatch):
    body = (
        "<html><head><script src='https://google.com/recaptcha/api.js'></script></head>"
        "<body><h1>Senior Analyst</h1><button>Apply now</button></body></html>"
        + "x" * 200
    )
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url("https://careers.example.com/job/valid") == (
        liveness.LIVE,
        "ok_200",
    )


@pytest.mark.parametrize(
    ("final_url", "reason"),
    [
        ("https://careers.example.com/login?next=/jobs/123", "redirect_login"),
        ("https://careers.example.com/", "redirect_home"),
    ],
)
def test_job_redirect_to_non_posting_destination_is_uncertain(monkeypatch, final_url, reason):
    body = "<html><h1>Company careers</h1></html>" + "x" * 2000
    _patch_fetch(monkeypatch, 200, body=body, final=final_url)
    assert liveness.probe_url("https://careers.example.com/jobs/123") == (liveness.UNCERTAIN, reason)


def test_canonical_job_path_redirect_remains_classifiable(monkeypatch):
    body = "<html><h1>Senior Analyst</h1><button>Apply</button></html>" + "x" * 2000
    _patch_fetch(
        monkeypatch,
        200,
        body=body,
        final="https://jobs.example.com/job/123?utm_source=aggregator",
    )
    assert liveness.probe_url("https://careers.example.com/jobs/123") == (liveness.LIVE, "ok_200")


@pytest.mark.parametrize(
    ("url", "body", "expected"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/123",
            '{"id": 123, "title": "Analyst"}',
            (liveness.LIVE, "gh_api_200"),
        ),
        (
            "https://jobs.lever.co/acme/abc",
            '{"id": "abc", "text": "Analyst"}',
            (liveness.LIVE, "lever_api_200"),
        ),
        (
            "https://jobs.ashbyhq.com/acme/abc",
            '{"jobs": [{"id": "abc"}]}',
            (liveness.LIVE, "ashby_api_listed"),
        ),
    ],
)
def test_host_api_requires_matching_structured_job_identity(monkeypatch, url, body, expected):
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected_reason"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "gh_api_invalid_json"),
        ("https://jobs.lever.co/acme/abc", "lever_api_invalid_json"),
        ("https://jobs.ashbyhq.com/acme/abc", "ashby_api_invalid_json"),
    ],
)
def test_host_api_malformed_json_is_explicitly_uncertain(monkeypatch, url, expected_reason):
    _patch_fetch(monkeypatch, 200, body='<html>{"id":"abc"}</html>')
    assert liveness.probe_url(url) == (liveness.UNCERTAIN, expected_reason)


@pytest.mark.parametrize(
    ("url", "body", "expected_reason"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", '{"error": {"id": 123}}', "gh_api_invalid_payload"),
        ("https://jobs.lever.co/acme/abc", '{"error": {"id": "abc"}}', "lever_api_invalid_payload"),
        ("https://jobs.ashbyhq.com/acme/abc", '{"jobs": {"id": "abc"}}', "ashby_api_invalid_payload"),
    ],
)
def test_host_api_error_shapes_are_not_false_live(monkeypatch, url, body, expected_reason):
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url(url) == (liveness.UNCERTAIN, expected_reason)


@pytest.mark.parametrize(
    ("url", "body", "expected_reason"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", '{"id": 999}', "gh_api_id_mismatch"),
        ("https://jobs.lever.co/acme/abc", '{"id": "different"}', "lever_api_id_mismatch"),
    ],
)
def test_host_api_wrong_job_identity_is_uncertain(monkeypatch, url, body, expected_reason):
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url(url) == (liveness.UNCERTAIN, expected_reason)


def test_workday_cxs_requires_structured_job_posting(monkeypatch):
    _patch_fetch(
        monkeypatch,
        200,
        body='{"jobPostingInfo": {"title": "Senior Analyst", "jobReqId": "REF1"}}',
    )
    assert liveness.probe_url(
        "https://visa.wd5.myworkdayjobs.com/Visa/job/US---CA/Senior-Analyst_REF1"
    ) == (liveness.LIVE, "workday_cxs_200")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('<html>jobPostingInfo unavailable</html>', "workday_cxs_invalid_json"),
        ('{"error": {"jobPostingInfo": {"title": "not a posting"}}}', "workday_cxs_invalid_payload"),
        ('{"jobPostingInfo": "temporarily unavailable"}', "workday_cxs_invalid_payload"),
    ],
)
def test_workday_cxs_error_shapes_are_not_false_live(monkeypatch, body, reason):
    _patch_fetch(monkeypatch, 200, body=body)
    assert liveness.probe_url(
        "https://visa.wd5.myworkdayjobs.com/Visa/job/US---CA/Senior-Analyst_REF1"
    ) == (liveness.UNCERTAIN, reason)


def test_workday_cxs_403_falls_back_to_public_job_page(monkeypatch):
    calls = []

    def fetch(url, accept=None):
        calls.append((url, accept))
        if "/wday/cxs/" in url:
            return 403, url, ""
        return 200, url, (
            '<div data-automation-id="jobTitleHeading">Senior Analyst</div>'
            '<button>Apply</button>'
        )

    monkeypatch.setattr(liveness, "_fetch", fetch)

    status, reason = liveness.probe_url(
        "https://visa.wd5.myworkdayjobs.com/Visa/job/US---CA/Senior-Analyst_REF1"
    )

    assert status == liveness.LIVE
    assert reason == "workday_page_200_after_cxs_403"
    assert len(calls) == 2


def test_workday_cxs_block_does_not_trust_script_only_apply_marker(monkeypatch):
    def fetch(url, accept=None):
        if "/wday/cxs/" in url:
            return 403, url, ""
        return 200, url, (
            "<html><script>window.labels={apply:'Apply'}</script>"
            "<body><h1>Company careers</h1></body></html>" + "x" * 200
        )

    monkeypatch.setattr(liveness, "_fetch", fetch)

    assert liveness.probe_url(
        "https://visa.wd5.myworkdayjobs.com/Visa/job/US---CA/Senior-Analyst_REF1"
    ) == (liveness.UNCERTAIN, "workday_blocked_403")
