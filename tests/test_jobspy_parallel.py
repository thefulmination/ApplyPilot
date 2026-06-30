from __future__ import annotations

import threading
import time


class _FakeCursor:
    def fetchone(self) -> tuple[int]:
        return (123,)


class _FakeConn:
    def execute(self, _sql: str) -> _FakeCursor:
        return _FakeCursor()


def _search_cfg(search_count: int = 4) -> dict:
    return {
        "queries": [{"query": f"Role {idx}", "tier": 1} for idx in range(search_count)],
        "locations": [{"label": "sf", "location": "San Francisco"}],
        "defaults": {},
    }


def test_full_crawl_runs_jobspy_searches_in_parallel_when_workers_requested(monkeypatch) -> None:
    from applypilot.discovery import jobspy

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run_one_search(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"new": 1, "existing": 2, "errors": 0, "filtered": 0, "total": 3, "label": "fake"}

    monkeypatch.setattr(jobspy, "init_db", lambda: _FakeConn())
    monkeypatch.setattr(jobspy, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(jobspy, "_run_one_search", fake_run_one_search)

    result = jobspy._full_crawl(_search_cfg(), sites=["indeed"], workers=3)

    assert result["new"] == 4
    assert result["existing"] == 8
    assert result["errors"] == 0
    assert max_active >= 2


def test_full_crawl_defaults_to_serial_jobspy_searches(monkeypatch) -> None:
    from applypilot.discovery import jobspy

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run_one_search(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {"new": 1, "existing": 0, "errors": 0, "filtered": 0, "total": 1, "label": "fake"}

    monkeypatch.setattr(jobspy, "init_db", lambda: _FakeConn())
    monkeypatch.setattr(jobspy, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(jobspy, "_run_one_search", fake_run_one_search)

    result = jobspy._full_crawl(_search_cfg(), sites=["indeed"])

    assert result["queries"] == 4
    assert result["new"] == 4
    assert max_active == 1
