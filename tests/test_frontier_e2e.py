# tests/test_frontier_e2e.py
"""End-to-end frontier pass: advisory-only + disagreement report surface correctly."""
import sqlite3
from applypilot.fleet import frontier_pass as fp, frontier_db as fdb

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, audit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def test_frontier_pass_end_to_end_advisory_and_report(monkeypatch, tmp_path):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c)
    c.executemany("INSERT INTO jobs (url, company, title, full_description, research_fit_score, fit_score, audit_score) VALUES (?,?,?,?,?,?,?)",
                  [("agree", "C", "COS", "d", 8.0, 8, 8), ("disagree", "C", "PM", "d", 9.0, 9, 9)])
    c.commit()
    scores = {"agree": 8, "disagree": 3}
    monkeypatch.setattr(fp, "score_via_codex",
                        lambda prompt, **k: {"score": scores["disagree"] if "PM" in prompt else scores["agree"], "reasoning": "x"})
    res = fp.run_frontier_pass(c, resume_text="R", schema_path=str(tmp_path / "s.json"), min_gap_seconds=0)
    assert res["scored"] == 2 and res["by_subscription"] == 2
    # advisory only
    assert c.execute("SELECT fit_score FROM jobs WHERE url='disagree'").fetchone()["fit_score"] == 9
    # the divergent one shows in the report; the agreeing one doesn't
    urls = [r["url"] for r in fdb.disagreement_report(c, max_agreement=0.8)]
    assert "disagree" in urls and "agree" not in urls


# ---------------------------------------------------------------------------
# Governor-deny test (Task-6 review gap coverage)
# ---------------------------------------------------------------------------

def test_governor_deny_fails_over_to_metered(monkeypatch, tmp_path):
    """When the governor denies the subscription path, the metered provider is used
    (failed_over==1, by_subscription==0) and the upserted provider is the metered one."""

    class _DenyGov:
        def allow(self):
            return False

        def record(self, outcome, **k):
            pass

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c)
    c.execute(
        "INSERT INTO jobs (url, company, title, full_description, research_fit_score, fit_score, audit_score) "
        "VALUES ('g1','X','Analyst','desc', 8.0, 8, 8)"
    )
    c.commit()

    # score_via_codex must NOT be called when governor denies
    def _should_not_be_called(*a, **k):
        raise AssertionError("score_via_codex called despite governor deny")

    monkeypatch.setattr(fp, "score_via_codex", _should_not_be_called)
    monkeypatch.setattr(fp, "score_job", lambda *a, **k: {"score": 5, "reasoning": "metered"})

    res = fp.run_frontier_pass(
        c,
        resume_text="R",
        schema_path=str(tmp_path / "s.json"),
        use_subscription=True,
        metered_provider="gpt-5.5",
        governor=_DenyGov(),
        min_gap_seconds=0,
    )
    assert res["failed_over"] == 1
    assert res["by_subscription"] == 0
    row = c.execute("SELECT provider FROM frontier_scores WHERE url='g1'").fetchone()
    assert row["provider"] == "gpt-5.5"
