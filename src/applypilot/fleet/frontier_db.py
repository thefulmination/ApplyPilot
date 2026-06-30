"""Advisory frontier-score side-table in the brain (no jobs migration). The frontier
quality lane writes here ONLY -- jobs.fit_score/audit_score/research_fit_score are
never touched."""
from __future__ import annotations

from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier_scores (
  url            TEXT PRIMARY KEY,
  cheap_score    REAL,
  frontier_score REAL,
  opus_score     REAL,
  frontier_decision TEXT,
  provider       TEXT,
  agreement      REAL,
  reasoning      TEXT,
  scored_at      TEXT
);
"""


def ensure_frontier_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def upsert_frontier_score(conn, *, url, cheap_score, frontier_score, provider, agreement,
                          frontier_decision=None, opus_score=None, reasoning=None) -> None:
    conn.execute(
        "INSERT INTO frontier_scores "
        "(url, cheap_score, frontier_score, opus_score, frontier_decision, provider, agreement, reasoning, scored_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET cheap_score=excluded.cheap_score, frontier_score=excluded.frontier_score, "
        "opus_score=excluded.opus_score, frontier_decision=excluded.frontier_decision, provider=excluded.provider, "
        "agreement=excluded.agreement, reasoning=excluded.reasoning, scored_at=excluded.scored_at",
        (url, cheap_score, frontier_score, opus_score, frontier_decision, provider, agreement, reasoning,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def disagreement_report(conn, *, max_agreement=0.8) -> list[dict]:
    rows = conn.execute(
        "SELECT f.url, j.company, j.title, f.cheap_score, f.frontier_score, f.opus_score, f.agreement, f.provider "
        "FROM frontier_scores f LEFT JOIN jobs j ON j.url = f.url "
        "WHERE f.agreement < ? ORDER BY f.agreement ASC",
        (max_agreement,),
    ).fetchall()
    return [dict(r) for r in rows]
