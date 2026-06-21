from __future__ import annotations

import requests

from applypilot.discovery import public_boards


def test_job_query_matching_accepts_all_words_without_exact_phrase() -> None:
    job = {"title": "Director, Strategy & Operations", "description": ""}

    assert public_boards._job_matches_queries(job, ["Strategy Operations"])


def test_builtin_static_parser_extracts_matching_remote_jobs(monkeypatch) -> None:
    html = """
    <html><body>
      <a href="/job/chief-staff-global-operations-services-go-s/9546544">
        Chief-of-Staff, Global Operations & Services
      </a>
      <a href="/job/retail-store-manager/111">Retail Store Manager</a>
    </body></html>
    """
    monkeypatch.setattr(public_boards, "_fetch_html", lambda *_args, **_kwargs: html)

    jobs = public_boards._discover_builtin(
        requests.Session(),
        {
            "queries": [{"query": "Chief of Staff"}],
            "public_boards": {"builtin_paths": ["/jobs/remote/operations"], "results_per_source": 20},
        },
        ["Remote"],
        [],
    )

    assert len(jobs) == 1
    assert jobs[0]["source_board"] == "builtin"
    assert jobs[0]["url"] == "https://builtin.com/job/chief-staff-global-operations-services-go-s/9546544"


def test_yc_jobs_static_parser_extracts_company_job_links(monkeypatch) -> None:
    html = """
    <html><body>
      <a href="/companies/acme-ai/jobs/abc123-business-development-lead">
        Business Development Lead
      </a>
      <a href="/companies/acme-ai">Acme AI</a>
    </body></html>
    """
    monkeypatch.setattr(public_boards, "_fetch_html", lambda *_args, **_kwargs: html)

    jobs = public_boards._discover_yc_jobs(
        requests.Session(),
        {"queries": [{"query": "Business Development"}], "public_boards": {"results_per_source": 20}},
        ["Remote"],
        [],
    )

    assert len(jobs) == 1
    assert jobs[0]["source_board"] == "yc_jobs"
    assert jobs[0]["company"] == "Acme Ai"


def test_chief_of_staff_jobs_parser_dedupes_job_links(monkeypatch) -> None:
    html = """
    <html><body>
      <a href="/jobs/chief-of-staff-trail-of-bits">Chief of Staff Remote Trail of Bits United States</a>
      <a href="/jobs/chief-of-staff-trail-of-bits">Chief of Staff Remote Trail of Bits United States</a>
    </body></html>
    """
    monkeypatch.setattr(public_boards, "_fetch_html", lambda *_args, **_kwargs: html)

    jobs = public_boards._discover_chief_of_staff_jobs(
        requests.Session(),
        {"public_boards": {"results_per_source": 20}},
        ["Remote", "United States"],
        [],
    )

    assert len(jobs) == 1
    assert jobs[0]["source_board"] == "chief_of_staff_jobs"
    assert jobs[0]["url"] == "https://www.chiefofstaffjob.com/jobs/chief-of-staff-trail-of-bits"


def test_remoteok_parser_skips_legal_notice_and_normalizes(monkeypatch) -> None:
    payload = [
        {"legal": "RemoteOK API usage terms"},  # header element, no id/position
        {
            "id": "123", "position": "Director of Operations", "company": "Acme",
            "description": "<p>Lead operations. SQL and strategy.</p>",
            "location": "Remote", "url": "https://remoteok.com/remote-jobs/123",
            "apply_url": "https://acme.com/apply", "salary_min": 150000, "salary_max": 200000,
        },
    ]
    monkeypatch.setattr(public_boards, "_fetch_json", lambda *a, **k: payload)
    jobs = public_boards._discover_remoteok(requests.Session(), {"public_boards": {}}, ["Remote"], [])
    assert len(jobs) == 1
    j = jobs[0]
    assert j["source_board"] == "remoteok"
    assert j["title"] == "Director of Operations"
    assert j["company"] == "Acme"
    assert j["application_url"] == "https://acme.com/apply"
    assert "Lead operations" in (j["full_description"] or "")
    assert "<p>" not in (j["full_description"] or "")  # HTML stripped by _plain_text
    assert j["salary"] == "$150,000-$200,000"


def test_himalayas_parser_normalizes(monkeypatch) -> None:
    payload = {"jobs": [{
        "title": "Chief of Staff", "companyName": "Globex",
        "guid": "https://himalayas.app/jobs/cos", "applicationLink": "https://globex.com/apply",
        "description": "<div>Own company strategy and operating cadence.</div>",
        "locationRestrictions": ["USA", "Canada"], "minSalary": 180000, "maxSalary": 220000,
    }]}
    monkeypatch.setattr(public_boards, "_fetch_json", lambda *a, **k: payload)
    jobs = public_boards._discover_himalayas(requests.Session(), {"public_boards": {}}, ["USA"], [])
    assert len(jobs) == 1
    j = jobs[0]
    assert j["source_board"] == "himalayas"
    assert j["url"] == "https://himalayas.app/jobs/cos"
    assert j["application_url"] == "https://globex.com/apply"
    assert j["location"] == "USA, Canada"
    assert j["company"] == "Globex"


def test_jobicy_parser_normalizes(monkeypatch) -> None:
    payload = {"jobs": [{
        "url": "https://jobicy.com/jobs/strategy-lead", "jobTitle": "Strategy Lead",
        "companyName": "Initech", "jobGeo": "Anywhere",
        "jobDescription": "<p>Run strategic initiatives.</p>",
        "annualSalaryMin": 140000, "annualSalaryMax": 170000, "salaryCurrency": "USD",
    }]}
    monkeypatch.setattr(public_boards, "_fetch_json", lambda *a, **k: payload)
    jobs = public_boards._discover_jobicy(requests.Session(), {"public_boards": {}}, [], [])
    assert len(jobs) == 1
    j = jobs[0]
    assert j["source_board"] == "jobicy"
    assert j["title"] == "Strategy Lead"
    assert j["company"] == "Initech"
    assert j["salary"] == "$140,000-$170,000"


def test_weworkremotely_rss_parser(monkeypatch) -> None:
    rss = """<?xml version="1.0"?><rss><channel>
      <item>
        <title>Globex: VP of Operations</title>
        <link>https://weworkremotely.com/remote-jobs/globex-vp-ops</link>
        <region>Anywhere in the World</region>
        <description><![CDATA[<p>Own global operations and strategy.</p>]]></description>
      </item>
      <item>
        <title>Acme: Sales Director</title>
        <link>https://weworkremotely.com/remote-jobs/acme-sales</link>
        <region>USA Only</region>
        <description>Lead the sales org.</description>
      </item>
    </channel></rss>"""
    monkeypatch.setattr(public_boards, "_fetch_html", lambda *a, **k: rss)
    jobs = public_boards._discover_weworkremotely(
        requests.Session(),
        {"public_boards": {"weworkremotely_categories": ["remote-business-jobs"]}},
        ["USA"], [],
    )
    assert len(jobs) == 2
    vp = next(j for j in jobs if j["title"] == "VP of Operations")
    assert vp["company"] == "Globex"
    assert vp["url"] == "https://weworkremotely.com/remote-jobs/globex-vp-ops"
    assert vp["source_board"] == "weworkremotely"
    assert "global operations" in (vp["full_description"] or "")
    assert "<p>" not in (vp["full_description"] or "")
