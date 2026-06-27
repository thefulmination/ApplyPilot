import sqlite3
from applypilot.fleet import frontier_select as fs, frontier_db as fdb

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, full_description TEXT,
  fit_score INTEGER, research_fit_score REAL, duplicate_of_url TEXT, discovered_at TEXT);"""


def _brain():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(_DDL); fdb.ensure_frontier_schema(c); return c


def test_urls_mode_empty_list_returns_empty():
    """Finding #7: mode='urls' with empty urls must not build an IN () syntax error."""
    c = _brain()
    assert fs.select_priority(c, mode="urls", urls=[]) == []


def test_backlog_orders_by_cheap_score_respects_floor_and_exclusions():
    c = _brain()
    c.executemany("INSERT INTO jobs (url, company, title, full_description, research_fit_score) VALUES (?,?,?,?,?)",
                  [("u9", "C", "COS", "d", 9.0), ("u7", "C", "Analyst", "d", 7.0), ("u5", "C", "PM", "d", 5.0)])
    c.execute("INSERT INTO jobs (url, company, title, fit_score, duplicate_of_url) VALUES ('udup','C','X',9,'u9')")
    fdb.upsert_frontier_score(c, url="u9", cheap_score=9.0, frontier_score=9.0, provider="m", agreement=1.0)  # already done
    c.commit()
    got = [r["url"] for r in fs.select_priority(c, floor=7.0, limit=10)]
    assert got == ["u7"]  # u9 already-frontier-scored, u5 below floor, udup is a duplicate
