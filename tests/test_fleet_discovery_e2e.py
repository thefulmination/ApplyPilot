# tests/test_fleet_discovery_e2e.py
from applypilot.apply import pgqueue
from applypilot.fleet import scheduler, sync as sync_mod, discovery_adapter as da
from applypilot.fleet.worker import WorkerLoop
import pandas as pd


def test_discovery_end_to_end(fleet_db, monkeypatch):
    monkeypatch.setattr(da, "_scrape_with_retry",
                        lambda kwargs, **k: pd.DataFrame([{"job_url": "u1", "title": "COS", "location": "Remote"}]))
    monkeypatch.setattr(da, "_location_ok", lambda loc, a, r: True)
    captured = {}
    monkeypatch.setattr(sync_mod, "store_jobspy_results",
                        lambda conn, df, label: (captured.setdefault("urls", list(df["job_url"])), (len(df), 0))[1])
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, {"searches": [{"query": "chief of staff", "boards": ["indeed"],
                                                            "locations": ["Remote"]}]})
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="discovery",
                      search_fn=da.make_search_fn())
    assert loop.run_once()["action"] == "search_done"
    with pgqueue.connect(fleet_db) as pg:
        assert sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg) == 1
    assert captured["urls"] == ["u1"]
