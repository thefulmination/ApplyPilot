# tests/test_frontier_pass.py
import sqlite3
from applypilot.fleet import frontier_pass as fp, frontier_db as fdb, cli_providers as clp

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, audit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def _brain():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c)
    c.execute("INSERT INTO jobs (url, company, title, full_description, research_fit_score, fit_score, audit_score) "
              "VALUES ('u1','Acme','Chief of Staff','ops', 9.0, 9, 9)")
    c.commit(); return c


def test_subscription_path_writes_advisory_and_picks_top_model(monkeypatch, tmp_path):
    seen = {}
    def fake_codex(prompt, *, schema_path, model=None, **k):
        seen["model"] = model
        return {"score": 7, "reasoning": "frontier view"}
    monkeypatch.setattr(fp, "score_via_codex", fake_codex)
    c = _brain()
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"),
                               top_tier_floor=8.5, top_model="gpt-5.5", backlog_model="gpt-5.5-mini",
                               min_gap_seconds=0)
    assert res["scored"] == 1 and res["by_subscription"] == 1
    assert seen["model"] == "gpt-5.5"  # cheap_score 9.0 >= top_tier_floor -> top model
    row = c.execute("SELECT frontier_score, agreement, provider FROM frontier_scores WHERE url='u1'").fetchone()
    assert row["frontier_score"] == 7 and row["provider"] == "codex-subscription"
    assert abs(row["agreement"] - round(1 - abs(7 - 9) / 9.0, 3)) < 1e-6
    # ADVISORY ONLY: jobs canonical scores untouched
    j = c.execute("SELECT fit_score, audit_score, research_fit_score FROM jobs WHERE url='u1'").fetchone()
    assert j["fit_score"] == 9 and j["audit_score"] == 9 and j["research_fit_score"] == 9.0


def test_failover_to_metered_on_subscription_unavailable(monkeypatch, tmp_path):
    def boom(*a, **k): raise clp.SubscriptionUnavailable("limit")
    monkeypatch.setattr(fp, "score_via_codex", boom)
    monkeypatch.setattr(fp, "score_job", lambda *a, **k: {"score": 6, "reasoning": "api"})
    c = _brain()
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"),
                               metered_provider="gpt-5.5", min_gap_seconds=0)
    assert res["failed_over"] == 1 and res["by_subscription"] == 0
    row = c.execute("SELECT frontier_score, provider FROM frontier_scores WHERE url='u1'").fetchone()
    assert row["frontier_score"] == 6 and row["provider"] == "gpt-5.5"  # the metered model
