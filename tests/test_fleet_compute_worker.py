# tests/test_fleet_compute_worker.py
from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def _factory(dsn):
    return lambda: pgqueue.connect(dsn)


def test_compute_worker_routes_audit_task_and_records_provider(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c-audit", "task": "audit",
                                        "payload": {"title": "Chief of Staff", "company": "Acme",
                                                    "full_description": "ops", "fit_score": 8}}])
        queue.push_compute_jobs(conn, [{"url": "c-score", "task": "score",
                                        "payload": {"title": "COS", "company": "Acme", "full_description": "ops"}}])

    fns = {
        "audit": lambda payload: ({"task": "audit", "research_decision": "qualified", "status": "done"}, 0.0),
        "score": lambda payload: ({"task": "score", "research_fit_score": 9, "model": "deepseek-v4-flash",
                                   "provider": "deepseek", "status": "done"}, 0.0003),
    }
    loop = WorkerLoop(_factory(fleet_db), "w-c", home_ip="1.1.1.1", role="compute", compute_fns=fns)
    a1 = loop.run_once(); a2 = loop.run_once()
    assert {a1["action"], a2["action"]} == {"compute_done"}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, result FROM compute_queue WHERE url='c-audit'")
        r = cur.fetchone(); assert r["status"] == "done" and r["result"]["research_decision"] == "qualified"
        cur.execute("SELECT provider, model FROM llm_usage WHERE task='score'")
        u = cur.fetchone(); assert u["provider"] == "deepseek" and u["model"] == "deepseek-v4-flash"
