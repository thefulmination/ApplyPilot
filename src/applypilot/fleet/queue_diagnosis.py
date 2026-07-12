"""Read-only apply queue diagnosis helpers.

Raw status counts can be misleading: a row can be status='queued' while still
being unleaseable because it is unapproved or blocked by applied_set. This module
keeps that operator-facing accounting in one place and never mutates fleet state.
"""
from __future__ import annotations

import re
from typing import Any

_NONWORD = re.compile(r"[^a-z0-9]+")
_KNOWN_AGGREGATOR_COMPANIES = {
    "chiefofstaffjob com",
    "chief of staff job",
    "hiringcafe",
    "hiring cafe",
    "indeed",
    "linkedin",
}


def _norm_company(value: str | None) -> str:
    if not value:
        return ""
    return _NONWORD.sub(" ", value.strip().lower()).strip()


def _is_overbroad_company(value: str | None) -> bool:
    normalized = _norm_company(value)
    return not normalized or normalized in _KNOWN_AGGREGATOR_COMPANIES


def queue_diagnosis(conn, *, overbroad_limit: int = 25) -> dict[str, Any]:
    """Return a read-only diagnosis of why raw queued rows may not be leaseable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE q.status='queued') AS queued_total,
              COUNT(*) FILTER (WHERE q.status='queued' AND q.lane='ats') AS ats_queued,
              COUNT(*) FILTER (
                WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
              ) AS approved_ats,
              COUNT(*) FILTER (
                WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NULL
              ) AS unapproved_ats,
              COUNT(*) FILTER (
                WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
                  AND EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key=q.dedup_key)
              ) AS dedup_blocked,
              COUNT(*) FILTER (
                WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key=q.dedup_key)
              ) AS base_leaseable
            FROM apply_queue q
            """
        )
        q = cur.fetchone()

        cur.execute(
            """
            SELECT CASE
                     WHEN q.execution_route = 'exception'
                       THEN COALESCE(NULLIF(q.host_policy, ''), NULLIF(q.apply_error, ''))
                     ELSE COALESCE(NULLIF(q.apply_error, ''), NULLIF(q.apply_status, ''))
                   END AS reason,
                   COUNT(*) AS n
              FROM apply_queue q
             WHERE q.status='blocked'
             GROUP BY 1
             ORDER BY n DESC, reason
            """
        )
        blocked_reasons = {
            str(row["reason"] or "blocked_without_reason"): int(row["n"] or 0)
            for row in cur.fetchall()
        }

        cur.execute(
            """
            SELECT
              COUNT(*) AS queued_dedup_blocked,
              COUNT(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key AND s.status='applied'
                )
              ) AS has_applied_source,
              COUNT(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key AND s.status='crash_unconfirmed'
                )
              ) AS has_crash_source,
              COUNT(*) FILTER (
                WHERE NOT EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key
                    AND s.status IN ('applied','crash_unconfirmed')
                )
              ) AS no_current_queue_source,
              COUNT(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key AND s.status='crash_unconfirmed'
                    AND s.apply_error='failed:no_result_line'
                )
              ) AS has_no_result_source,
              COUNT(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key AND s.status='crash_unconfirmed'
                    AND s.apply_error='failed:timeout'
                )
              ) AS has_timeout_source,
              COUNT(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM apply_queue s
                  WHERE s.dedup_key=q.dedup_key AND s.status='crash_unconfirmed'
                    AND s.apply_error='crash_unconfirmed'
                )
              ) AS has_reclaim_source
            FROM apply_queue q
            JOIN applied_set a ON a.dedup_key=q.dedup_key
            WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
            """
        )
        sources = cur.fetchone()

        cur.execute(
            """
            SELECT q.dedup_key, a.company AS applied_set_company, COUNT(*) AS queued_rows
            FROM apply_queue q
            JOIN applied_set a ON a.dedup_key=q.dedup_key
            WHERE q.status='queued' AND q.lane='ats' AND q.approved_batch IS NOT NULL
            GROUP BY q.dedup_key, a.company
            ORDER BY queued_rows DESC, q.dedup_key
            LIMIT %s
            """,
            (max(int(overbroad_limit), 1) * 4,),
        )
        overbroad_groups = []
        for row in cur.fetchall():
            company = row["applied_set_company"]
            if not _is_overbroad_company(company):
                continue
            overbroad_groups.append(
                {
                    "dedup_key": row["dedup_key"],
                    "applied_set_company": company,
                    "queued_rows": int(row["queued_rows"] or 0),
                }
            )
            if len(overbroad_groups) >= overbroad_limit:
                break

    return {
        "queued": {
            "total": int(q["queued_total"] or 0),
            "ats": int(q["ats_queued"] or 0),
            "approved_ats": int(q["approved_ats"] or 0),
            "unapproved_ats": int(q["unapproved_ats"] or 0),
            "dedup_blocked": int(q["dedup_blocked"] or 0),
            "base_leaseable": int(q["base_leaseable"] or 0),
        },
        "blocked": {
            "total": sum(blocked_reasons.values()),
            "reasons": blocked_reasons,
        },
        "dedup": {
            "blocked_sources": {
                "total": int(sources["queued_dedup_blocked"] or 0),
                "has_applied_source": int(sources["has_applied_source"] or 0),
                "has_crash_source": int(sources["has_crash_source"] or 0),
                "no_current_queue_source": int(sources["no_current_queue_source"] or 0),
                "has_no_result_source": int(sources["has_no_result_source"] or 0),
                "has_timeout_source": int(sources["has_timeout_source"] or 0),
                "has_reclaim_source": int(sources["has_reclaim_source"] or 0),
            },
            "overbroad_groups": overbroad_groups,
        },
    }
