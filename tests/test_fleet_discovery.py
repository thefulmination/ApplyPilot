# tests/test_fleet_discovery.py
import os

from applypilot.apply import pgqueue
from applypilot.fleet import queue


def test_build_discovery_loop_wires_search_fn(fleet_db):
    from applypilot.fleet import discovery_main as dm
    loop = dm.build_discovery_loop(dsn=fleet_db, worker_id="w1", home_ip="1.1.1.1",
                                   results_per_site=25, hours_old=48, proxy=None)
    assert loop.role == "discovery" and loop.search_fn is not None


def test_main_worker_no_dsn_raises_system_exit(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    from applypilot.fleet import discovery_main as dm
    import pytest
    with pytest.raises(SystemExit):
        dm.main_worker(["--worker-id", "w1"])


def test_worker_discovery_stages_postings(fleet_db):
    from applypilot.fleet.worker import WorkerLoop
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO search_tasks (task_id, query, board, location, cadence_seconds) "
                    "VALUES ('t1','chief of staff','indeed','Remote',3600)")
        conn.commit()
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w-disc", home_ip="1.1.1.1", role="discovery",
                      search_fn=lambda task: [{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
    res = loop.run_once()
    assert res["action"] == "search_done" and res["staged"] == 2
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM discovered_postings WHERE task_id='t1'")
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT status, next_due_at > now() AS future FROM search_tasks WHERE task_id='t1'")
        r = cur.fetchone(); assert r["status"] == "queued" and r["future"] is True  # rescheduled


def test_push_discovered_stages_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        n = queue.push_discovered(conn, task_id="t1", source_label="chief of staff", worker_id="w1",
                                  postings=[{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
        assert n == 2
        with conn.cursor() as cur:
            cur.execute("SELECT task_id, source_label, worker_id, posting, synced_to_home_at "
                        "FROM discovered_postings ORDER BY posting->>'job_url'")
            rows = cur.fetchall()
        assert [r["posting"]["job_url"] for r in rows] == ["u1", "u2"]
        assert rows[0]["task_id"] == "t1" and rows[0]["source_label"] == "chief of staff"
        assert rows[0]["synced_to_home_at"] is None


def test_pull_discovered_ingests_and_marks_synced(fleet_db, monkeypatch):
    import applypilot.fleet.sync as sync_mod
    captured = {}
    def fake_store(conn, df, source_label):
        captured["urls"] = list(df["job_url"]); captured["label"] = source_label
        return (len(df), 0)
    monkeypatch.setattr(sync_mod, "store_jobspy_results", fake_store)
    with pgqueue.connect(fleet_db) as conn:
        queue.push_discovered(conn, task_id="t1", source_label="cos", worker_id="w1",
                              postings=[{"job_url": "u1", "title": "COS"}, {"job_url": "u2", "title": "PM"}])
    with pgqueue.connect(fleet_db) as pg:
        n = sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg)  # brain conn unused by the stub
    assert n == 2 and captured["urls"] == ["u1", "u2"] and captured["label"] == "cos"
    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM discovered_postings WHERE synced_to_home_at IS NULL")
        assert cur.fetchone()["n"] == 0
    # re-pull is a no-op
    with pgqueue.connect(fleet_db) as pg:
        assert sync_mod.pull_discovered(sqlite_conn=object(), pg_conn=pg) == 0
