import pytest

from applypilot.discovery import hiringcafe


class _RateLimitedResponse:
    status_code = 429
    headers = {"Retry-After": "12"}
    text = ""

    def raise_for_status(self):
        raise AssertionError("429 should be handled before raise_for_status")


class _RateLimitedSession:
    def get(self, url, timeout):
        return _RateLimitedResponse()


class _Session:
    headers = {}


def test_fetch_page_raises_rate_limit_error_with_retry_after() -> None:
    with pytest.raises(hiringcafe.HiringCafeRateLimitError) as exc:
        hiringcafe._fetch_page(_RateLimitedSession(), "https://hiring.cafe/jobs/test")

    assert exc.value.retry_after_seconds == 12
    assert "429" in str(exc.value)


def test_discovery_backs_off_and_stops_after_repeated_rate_limits(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    sleeps: list[float] = []

    def fake_run_one_search(session, query, location, *args, **kwargs):
        calls.append((query, location))
        return {
            "new": 0,
            "existing": 0,
            "seen": 0,
            "filtered": 0,
            "errors": 1,
            "rate_limited": True,
            "retry_after_seconds": 12,
        }

    monkeypatch.setattr(hiringcafe, "init_db", lambda: None)
    monkeypatch.setattr(hiringcafe.requests, "Session", lambda: _Session())
    monkeypatch.setattr(hiringcafe, "_load_company_watchlist", lambda cfg: [])
    monkeypatch.setattr(hiringcafe, "_run_one_search", fake_run_one_search)
    monkeypatch.setattr(hiringcafe.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = hiringcafe.run_hiringcafe_discovery(
        {
            "hiring_cafe": {
                "enabled": True,
                "company_watchlist_enabled": False,
                "rate_limit_backoff_seconds": 5,
                "max_consecutive_rate_limits": 2,
                "request_delay_seconds": 0,
            },
            "queries": [{"query": "Chief of Staff"}],
            "locations": [
                {"location": "San Francisco"},
                {"location": "New York"},
                {"location": "Boston"},
            ],
        }
    )

    assert calls == [("Chief of Staff", "San Francisco"), ("Chief of Staff", "New York")]
    assert sleeps == [12]
    assert result["errors"] == 2
    assert result["rate_limited"] is True
    assert result["rate_limit_hits"] == 2
    assert result["queries"] == 2
