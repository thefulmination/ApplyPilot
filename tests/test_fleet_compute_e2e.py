# tests/test_fleet_compute_e2e.py
import sqlite3
import pytest
from applypilot.apply import pgqueue
from applypilot.fleet import compute_adapters as ca, compute_context as cc, sync
from applypilot.fleet.worker import WorkerLoop

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT);"""


def test_compute_lane_end_to_end_advisory(fleet_db, tmp_path, monkeypatch):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, fit_score, full_description) "
               "VALUES ('u1','Acme','Chief of Staff','https://x',8.0,8,'operations leadership')"); sq.commit()

    monkeypatch.setattr(ca, "score_job", lambda *a, **k: {"score": 9, "keywords": "ops",
                        "reasoning": "strong", "model": "deepseek-v4-flash", "provider": "deepseek"})
    monkeypatch.setattr(ca, "get_client", lambda **k: type("C", (), {"model": "deepseek-v4-flash",
                        "last_usage": {"prompt_tokens": 50, "completion_tokens": 10}})())
    monkeypatch.setattr(ca, "estimate_cost", lambda m, u: 0.0002)

    with pgqueue.connect(fleet_db) as pg:
        cc.publish_context(pg, resume_text="R", preference_profile={}, kg_prompt="KG",
                           search_cfg={}, version="v1")
        sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7)
        ctx, _ = cc.load_context(pg, providers=["deepseek"])

    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w-e2e", home_ip="1.1.1.1", role="compute",
                      compute_fns={"score": ca.make_score_fn(ctx)})
    assert loop.run_once()["action"] == "compute_done"

    with pgqueue.connect(fleet_db) as pg:
        n = sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg)
    assert n == 1
    row = sq.execute("SELECT research_fit_score, fit_score, audit_score FROM jobs WHERE url='u1'").fetchone()
    assert row["research_fit_score"] == 9          # advisory written
    assert row["fit_score"] == 8 and row["audit_score"] == 8.0   # NEVER auto-promoted

    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT provider, cost_usd FROM llm_usage WHERE worker_id='w-e2e'")
        u = cur.fetchone(); assert u["provider"] == "deepseek" and float(u["cost_usd"]) == 0.0002


@pytest.mark.skipif("not __import__('os').environ.get('DEEPSEEK_API_KEY')",
                    reason="live smoke needs a real key")
def test_live_smoke_scores_one_real_job():
    from applypilot.fleet.compute_adapters import ComputeContext, make_score_fn
    fn = make_score_fn(ComputeContext(resume_text="Operations leader, 9 yrs.", providers=["deepseek"]))
    result, cost = fn({"title": "Chief of Staff", "company": "Acme", "full_description": "Lead ops."})
    assert result["status"] in {"done", "failed"} and cost >= 0
