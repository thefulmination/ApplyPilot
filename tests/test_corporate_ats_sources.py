from __future__ import annotations

from applypilot.discovery import corporate_ats


def test_smartrecruiters_fetcher_normalizes(monkeypatch):
    payload = {"content": [
        {
            "id": "743999", "name": "Director of Strategy",
            "location": {"city": "San Francisco", "region": "CA", "country": "US", "remote": False},
            "department": {"label": "Strategy"},
        },
        {
            "id": "744000", "name": "Remote Ops Lead",
            "location": {"country": "US", "remote": True},
        },
    ]}
    monkeypatch.setattr(corporate_ats, "_request_json", lambda *a, **k: (200, payload))

    found, jobs = corporate_ats._fetch_smartrecruiters("Acme", "acme", {}, 50)

    assert found is True and len(jobs) == 2
    j = jobs[0]
    assert j["strategy"] == "smartrecruiters_api"
    assert j["title"] == "Director of Strategy"
    assert j["location"] == "San Francisco, CA, US"
    assert j["url"] == "https://jobs.smartrecruiters.com/acme/743999"
    assert j["department"] == "Strategy"
    assert "Remote" in (jobs[1]["location"] or "")


def test_smartrecruiters_fetcher_handles_not_found(monkeypatch):
    monkeypatch.setattr(corporate_ats, "_request_json", lambda *a, **k: (404, None))
    found, jobs = corporate_ats._fetch_smartrecruiters("Acme", "acme", {}, 50)
    assert found is False and jobs == []


def test_workable_fetcher_normalizes(monkeypatch):
    payload = {"name": "Globex", "jobs": [
        {
            "title": "Chief of Staff", "shortcode": "ABC123",
            "url": "https://globex.workable.com/j/ABC123",
            "application_url": "https://apply.workable.com/globex/j/ABC123/apply",
            "location": {"city": "NYC", "region": "NY", "country": "US", "telecommuting": True},
            "description": "<p>Own strategy and operating cadence.</p>",
            "requirements": "<p>5+ years in strategy.</p>",
            "department": "Operations",
        },
    ]}
    monkeypatch.setattr(corporate_ats, "_request_json", lambda *a, **k: (200, payload))

    found, jobs = corporate_ats._fetch_workable("Globex", "globex", {}, 50)

    assert found is True and len(jobs) == 1
    j = jobs[0]
    assert j["strategy"] == "workable_api"
    assert j["title"] == "Chief of Staff"
    assert "Own strategy" in (j["full_description"] or "")
    assert "5+ years" in (j["full_description"] or "")
    assert "<p>" not in (j["full_description"] or "")
    assert "Remote" in (j["location"] or "")
    assert j["application_url"] == "https://apply.workable.com/globex/j/ABC123/apply"


def test_new_ats_providers_are_dispatched():
    # the dispatcher must route the new sources, and they must be allowed by default
    assert "smartrecruiters" in corporate_ats.DEFAULT_SOURCES
    assert "workable" in corporate_ats.DEFAULT_SOURCES
