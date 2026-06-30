from __future__ import annotations

from datetime import datetime, timezone

from applypilot import config
from applypilot.discovery import smartextract


def test_validate_smart_jobs_rejects_junk_and_relative_urls() -> None:
    jobs = [
        {"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"},
        {"title": "Apply", "url": "https://example.com/apply", "location": "Remote"},
        {"title": "Strategy Lead", "url": "/jobs/2", "location": "Remote"},
        {"title": "", "url": "https://example.com/jobs/3", "location": "Remote"},
        {"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"},
    ]

    valid, counts = smartextract.validate_smart_jobs(jobs)

    assert valid == [
        {"title": "Chief of Staff", "url": "https://example.com/jobs/1", "location": "Remote"}
    ]
    assert counts["accepted"] == 1
    assert counts["invalid_title"] == 2
    assert counts["invalid_url"] == 1
    assert counts["duplicate_url"] == 1


def test_validate_smart_jobs_coerces_structured_salary_to_scalar() -> None:
    # JSON-LD baseSalary is often a nested object; storing it raw raised
    # sqlite3.InterfaceError and aborted the whole discovery run.
    jobs = [
        {
            "title": "Staff Engineer",
            "url": "https://example.com/jobs/9",
            "location": {"@type": "Place", "address": {"addressCountry": "US"}},
            "salary": {
                "@type": "MonetaryAmount",
                "currency": "USD",
                "value": {"minValue": 100000, "maxValue": 150000},
            },
        }
    ]

    valid, counts = smartextract.validate_smart_jobs(jobs)

    assert counts["accepted"] == 1
    assert isinstance(valid[0]["salary"], (str, int, float, type(None)))
    assert isinstance(valid[0]["location"], (str, int, float, type(None)))
    assert "100000" in valid[0]["salary"]


def test_health_cooldown_skips_after_repeated_failures(tmp_path, monkeypatch) -> None:
    health_path = tmp_path / "smartextract_health.json"
    monkeypatch.setattr(smartextract, "SMART_HEALTH_PATH", health_path)
    health = {}
    for _ in range(3):
        smartextract.record_source_health(
            health,
            "SlowBoard",
            url="https://example.com/jobs",
            status="COLLECT_ERROR",
            strategy=None,
            jobs_found=0,
            elapsed_seconds=30.0,
            hard_failure=True,
            timeout=True,
        )

    should_skip, reason = smartextract.should_skip_source(
        health,
        "SlowBoard",
        skip_after_failures=3,
        cooldown_hours=24,
    )

    assert should_skip
    assert "cooldown" in reason


def test_health_success_resets_consecutive_failures(tmp_path, monkeypatch) -> None:
    health_path = tmp_path / "smartextract_health.json"
    monkeypatch.setattr(smartextract, "SMART_HEALTH_PATH", health_path)
    health = {}
    smartextract.record_source_health(
        health,
        "Board",
        url="u",
        status="FAIL",
        strategy=None,
        jobs_found=0,
        elapsed_seconds=1,
        hard_failure=True,
    )
    smartextract.record_source_health(
        health,
        "Board",
        url="u",
        status="PASS",
        strategy="http_probe",
        jobs_found=2,
        elapsed_seconds=1,
        hard_failure=False,
    )

    assert health["Board"]["consecutive_failures"] == 0
    assert health["Board"]["last_successful_strategy"] == "http_probe"


def test_http_probe_extracts_json_ld_job(monkeypatch) -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"JobPosting","title":"Chief of Staff","description":"Run operations","url":"https://example.com/jobs/chief","jobLocation":{"address":{"addressLocality":"New York"}}}
      </script>
    </head></html>
    """

    class Response:
        text = html
        status_code = 200
        headers = {"content-type": "text/html"}
        url = "https://example.com/jobs"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(smartextract.requests, "get", lambda *args, **kwargs: Response())

    result = smartextract.http_probe_target("Example", "https://example.com/jobs")

    assert result["status"] == "PASS"
    assert result["strategy"] == "http_probe"
    assert result["jobs"][0]["title"] == "Chief of Staff"
    assert result["jobs"][0]["url"] == "https://example.com/jobs/chief"


def test_http_probe_classifies_cloudflare_challenge(monkeypatch) -> None:
    html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body>
        Enable JavaScript and cookies to continue
        <script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>
      </body>
    </html>
    """

    class Response:
        text = html
        status_code = 403
        headers = {"content-type": "text/html"}
        url = "https://www.trueup.io/jobs"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(smartextract.requests, "get", lambda *args, **kwargs: Response())

    result = smartextract.http_probe_target("TrueUp Jobs", "https://www.trueup.io/jobs")

    assert result["status"] == "CHALLENGE"
    assert result["issue_type"] == "cloudflare_challenge"
    assert result["challenge"] is True
    assert result["jobs"] == []


def test_record_source_health_tracks_challenge_issue_type() -> None:
    health = {}

    smartextract.record_source_health(
        health,
        "TrueUp Jobs",
        url="https://www.trueup.io/jobs",
        status="CHALLENGE",
        strategy="http_probe",
        jobs_found=0,
        elapsed_seconds=0.2,
        hard_failure=True,
        issue_type="cloudflare_challenge",
        challenge=True,
    )

    entry = health["TrueUp Jobs"]
    assert entry["last_issue_type"] == "cloudflare_challenge"
    assert entry["challenge_count"] == 1
    assert entry["consecutive_failures"] == 1


def test_summarize_smart_health_marks_cooling_down_challenge_sources() -> None:
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    health = {
        "TrueUp Jobs": {
            "source": "TrueUp Jobs",
            "last_url": "https://www.trueup.io/jobs",
            "last_status": "CHALLENGE",
            "last_issue_type": "cloudflare_challenge",
            "last_checked_at": now.isoformat(),
            "consecutive_failures": 3,
            "timeout_count": 0,
            "challenge_count": 2,
            "last_jobs_found": 0,
            "average_runtime_seconds": 0.4,
        }
    }

    rows = smartextract.summarize_smart_health(
        health,
        now=now,
        skip_after_failures=3,
        cooldown_hours=24,
    )

    assert rows == [
        {
            "source": "TrueUp Jobs",
            "status": "CHALLENGE",
            "issue_type": "cloudflare_challenge",
            "failures": 3,
            "timeouts": 0,
            "challenges": 2,
            "last_jobs_found": 0,
            "average_runtime_seconds": 0.4,
            "cooling_down": True,
            "cooldown_reason": "health cooldown: 3 failures",
            "last_url": "https://www.trueup.io/jobs",
        }
    ]


def test_skip_result_shape_contains_reason() -> None:
    result = smartextract.make_skip_result("SlowBoard", "health cooldown: 3 failures")

    assert result["name"] == "SlowBoard"
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "health cooldown: 3 failures"
    assert result["jobs"] == []


def test_trueup_registered_as_single_static_smart_extract_target() -> None:
    sites = smartextract.load_sites()
    trueup = next((site for site in sites if site.get("name") == "TrueUp Jobs"), None)

    assert trueup == {
        "name": "TrueUp Jobs",
        "url": "https://www.trueup.io/jobs",
        "type": "static",
    }
    assert config.load_base_urls()["TrueUp Jobs"] == "https://www.trueup.io"

    targets = smartextract.build_scrape_targets(
        sites=[trueup],
        search_cfg={
            "queries": [{"query": "Chief of Staff"}, {"query": "Strategy Operations"}],
            "locations": [{"location": "New York"}],
        },
        smart_cfg={},
    )

    assert targets == [
        {
            "name": "TrueUp Jobs",
            "url": "https://www.trueup.io/jobs",
            "query": None,
        }
    ]
