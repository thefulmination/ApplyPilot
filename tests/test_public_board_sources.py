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
