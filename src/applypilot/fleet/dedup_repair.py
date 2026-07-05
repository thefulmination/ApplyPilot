"""Narrow repair for overbroad fleet dedup keys.

Some imported board rows can inherit an aggregator-level dedup key such as
``HiringCafe``. If that key reaches ``applied_set``, unrelated queued postings
can be suppressed even though they are distinct ATS URLs. This module only
repairs that overbroad-key case and leaves true company/role dedup keys alone.
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from applypilot.fleet.queue_diagnosis import _is_overbroad_company


def _effective_url(row: dict[str, Any]) -> str:
    app = str(row.get("application_url") or "")
    if app.startswith(("http://", "https://")):
        return app
    return str(row.get("url") or "")


def _source_specific_key(row: dict[str, Any]) -> str:
    effective = _effective_url(row)
    parsed = urlparse(effective)
    identity = parsed.netloc.lower() + parsed.path.rstrip("/").lower()
    if not identity.strip():
        identity = str(row.get("url") or "")
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]
    return f"url:{digest}"


def _applied_set_company(conn, dedup_key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT company FROM applied_set WHERE dedup_key = %s", (dedup_key,))
        row = cur.fetchone()
    return row["company"] if row else None


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_audit_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dedup_repair_actions (
                id             BIGSERIAL PRIMARY KEY,
                url            TEXT NOT NULL,
                old_dedup_key  TEXT NOT NULL,
                new_dedup_key  TEXT NOT NULL,
                reason         TEXT,
                how_to_reverse TEXT,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("ALTER TABLE dedup_repair_actions ADD COLUMN IF NOT EXISTS how_to_reverse TEXT")


def plan_repair(conn, *, dedup_key: str, limit: int = 25) -> dict[str, Any]:
    """Return a dry-run repair plan for one overbroad dedup key."""
    company = _applied_set_company(conn, dedup_key)
    if company is None:
        return {
            "dedup_key": dedup_key,
            "safe_to_apply": False,
            "refused_reason": "dedup_key_not_in_applied_set",
            "applied_set_company": None,
            "candidates": [],
        }
    if not _is_overbroad_company(company):
        return {
            "dedup_key": dedup_key,
            "safe_to_apply": False,
            "refused_reason": "dedup_key_is_not_overbroad",
            "applied_set_company": company,
            "candidates": [],
        }

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT url, application_url, company, title, dedup_key
            FROM apply_queue
            WHERE dedup_key = %s
              AND status = 'queued'
              AND lane = 'ats'
              AND approved_batch IS NOT NULL
            ORDER BY updated_at, url
            LIMIT %s
            """,
            (dedup_key, max(int(limit), 1)),
        )
        rows = [dict(r) for r in cur.fetchall()]

    candidates = []
    for row in rows:
        new_key = _source_specific_key(row)
        if new_key == dedup_key:
            continue
        candidates.append(
            {
                "url": row["url"],
                "application_url": row["application_url"],
                "company": row["company"],
                "title": row["title"],
                "old_dedup_key": dedup_key,
                "new_dedup_key": new_key,
                "reason": "source_specific_overbroad_key",
            }
        )

    return {
        "dedup_key": dedup_key,
        "safe_to_apply": bool(candidates),
        "refused_reason": None if candidates else "no_queued_candidates",
        "applied_set_company": company,
        "candidates": candidates,
    }


def execute_repair(
    conn,
    *,
    dedup_key: str,
    max_rows: int = 25,
    reason: str = "source_specific_overbroad_key",
) -> dict[str, Any]:
    """Apply a previously dry-runnable overbroad dedup repair."""
    plan = plan_repair(conn, dedup_key=dedup_key, limit=max_rows)
    if not plan["safe_to_apply"]:
        return {
            "dedup_key": dedup_key,
            "updated": 0,
            "refused_reason": plan.get("refused_reason"),
            "candidates": plan.get("candidates", []),
        }

    _ensure_audit_table(conn)
    updated = 0
    try:
        with conn.cursor() as cur:
            for candidate in plan["candidates"][: max(int(max_rows), 0)]:
                cur.execute(
                    """
                    UPDATE apply_queue
                       SET dedup_key = %s, updated_at = now()
                     WHERE url = %s
                       AND dedup_key = %s
                       AND status = 'queued'
                       AND lane = 'ats'
                       AND approved_batch IS NOT NULL
                    """,
                    (candidate["new_dedup_key"], candidate["url"], dedup_key),
                )
                if cur.rowcount != 1:
                    continue
                updated += 1
                how_to_reverse = (
                    f"UPDATE apply_queue SET dedup_key={_sql_literal(dedup_key)} "
                    f"WHERE url={_sql_literal(candidate['url'])} "
                    f"AND dedup_key={_sql_literal(candidate['new_dedup_key'])};"
                )
                cur.execute(
                    """
                    INSERT INTO dedup_repair_actions
                        (url, old_dedup_key, new_dedup_key, reason, how_to_reverse)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (candidate["url"], dedup_key, candidate["new_dedup_key"], reason, how_to_reverse),
                )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return {
        "dedup_key": dedup_key,
        "updated": updated,
        "refused_reason": None,
        "candidates": plan["candidates"],
    }
