from __future__ import annotations

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


def test_skip_result_shape_contains_reason() -> None:
    result = smartextract.make_skip_result("SlowBoard", "health cooldown: 3 failures")

    assert result["name"] == "SlowBoard"
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "health cooldown: 3 failures"
    assert result["jobs"] == []
