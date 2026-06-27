"""Priority backlog selector for the frontier quality lane: the highest cheap-scored
jobs not yet frontier-scored. Reads the brain; writes nothing."""
from __future__ import annotations

from applypilot.fleet import frontier_db

_BASE = """
SELECT j.url, j.company, j.title, j.full_description,
       COALESCE(j.research_fit_score, j.fit_score) AS cheap_score
FROM jobs j
LEFT JOIN frontier_scores f ON f.url = j.url
WHERE f.url IS NULL
  AND j.duplicate_of_url IS NULL
  AND COALESCE(j.research_fit_score, j.fit_score) >= ?
"""


def select_priority(conn, *, limit=200, floor=7.0, mode="backlog", hours=24, urls=None) -> list[dict]:
    frontier_db.ensure_frontier_schema(conn)
    if mode == "urls":
        marks = ",".join("?" * len(urls or []))
        sql = _BASE + f" AND j.url IN ({marks}) ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, *(urls or []), limit]
    elif mode == "new":
        sql = _BASE + " AND j.discovered_at >= datetime('now', ?) ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, f"-{int(hours)} hours", limit]
    else:  # backlog
        sql = _BASE + " ORDER BY cheap_score DESC LIMIT ?"
        params = [floor, limit]
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
