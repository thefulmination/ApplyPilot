import pandas as pd
from applypilot.fleet import discovery_adapter as da


def test_search_fn_maps_kwargs_and_returns_records(monkeypatch):
    seen = {}
    def fake_scrape(kwargs, **k):
        seen["kwargs"] = kwargs
        return pd.DataFrame([{"job_url": "u1", "title": "COS", "location": "Remote"},
                             {"job_url": "u2", "title": "PM", "location": "Remote"}])
    monkeypatch.setattr(da, "_scrape_with_retry", fake_scrape)
    monkeypatch.setattr(da, "_location_ok", lambda loc, a, r: True)  # accept all
    fn = da.make_search_fn(results_per_site=25, hours_old=48)
    out = fn({"task_id": "t1", "query": "chief of staff", "board": "indeed",
              "location": "Remote", "params": {"remote": True}})
    assert [p["job_url"] for p in out] == ["u1", "u2"]
    assert seen["kwargs"]["search_term"] == "chief of staff"
    assert seen["kwargs"]["site_name"] == ["indeed"] and seen["kwargs"]["location"] == "Remote"
    assert seen["kwargs"]["results_wanted"] == 25 and seen["kwargs"]["hours_old"] == 48
    assert seen["kwargs"].get("is_remote") is True


def test_search_fn_returns_empty_on_scrape_error(monkeypatch):
    def boom(kwargs, **k): raise RuntimeError("blocked")
    monkeypatch.setattr(da, "_scrape_with_retry", boom)
    fn = da.make_search_fn()
    assert fn({"task_id": "t", "query": "q", "board": "indeed", "location": "NYC", "params": {}}) == []
