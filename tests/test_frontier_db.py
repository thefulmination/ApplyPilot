import sqlite3
from applypilot.fleet import frontier_db as fdb


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Create minimal jobs table for the LEFT JOIN in disagreement_report
    c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT)")
    fdb.ensure_frontier_schema(c)
    return c


def test_upsert_and_disagreement_report():
    c = _conn()
    fdb.upsert_frontier_score(c, url="a", cheap_score=8.0, frontier_score=8.0, provider="gpt-5.5", agreement=1.0)
    fdb.upsert_frontier_score(c, url="b", cheap_score=7.0, frontier_score=3.0, provider="gpt-5.5", agreement=0.56)
    fdb.upsert_frontier_score(c, url="c", cheap_score=9.0, frontier_score=5.0, provider="gpt-5.5", agreement=0.55)
    # upsert is idempotent on url
    fdb.upsert_frontier_score(c, url="a", cheap_score=8.0, frontier_score=8.0, provider="gpt-5.5", agreement=1.0)
    assert c.execute("SELECT COUNT(*) FROM frontier_scores").fetchone()[0] == 3
    rep = fdb.disagreement_report(c, max_agreement=0.8)
    assert [r["url"] for r in rep] == ["c", "b"]  # ordered by agreement asc, only < 0.8
