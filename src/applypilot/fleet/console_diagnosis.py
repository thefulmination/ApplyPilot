"""Read-only diagnosis helpers for the LAN fleet console.

No live actions are performed here. Every function receives an existing PG connection,
uses parameterized SQL, and rolls back its read transaction before returning.
"""
from __future__ import annotations

from typing import Any


def _scalar(row: Any, key: str, default=0):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _queue_counts(cur, table: str) -> dict[str, int]:
    cur.execute(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")
    return {r["status"]: int(r["n"]) for r in cur.fetchall()}


def _approved_count(cur, table: str) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} "
        "WHERE status='queued' AND approved_batch IS NOT NULL"
    )
    return int(cur.fetchone()["n"])


def _dedup_blocked_count(cur, table: str) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {table} q "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
    )
    return int(cur.fetchone()["n"])


def _leaseable_count(
    cur,
    table: str,
    *,
    canary_column: str | None = None,
    canary_enabled_column: str | None = None,
) -> int:
    canary_predicate = ""
    if canary_column and canary_enabled_column:
        canary_predicate = (
            f"AND (NOT COALESCE(cfg.{canary_enabled_column}, FALSE) "
            f"     OR COALESCE(cfg.{canary_column}, 0) > 0) "
        )
    cur.execute(
        f"WITH cfg AS (SELECT * FROM fleet_config WHERE id=1) "
        f"SELECT COUNT(*) AS n FROM {table} q, cfg "
        "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
        f"{canary_predicate}"
        "AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
    )
    return int(cur.fetchone()["n"])


def queue_diagnosis(conn) -> dict:
    """Return queue eligibility and a plain-English fleet state.

    This intentionally starts with the high-signal guards that explain the current
    fleet confusion: queued, approved, leaseable, dedup-blocked, and canary exhaustion.
    Later tasks add host/governor/browser/recommendation detail on top of this shape.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT paused, ats_paused, canary_enabled, canary_remaining, "
            "linkedin_canary_enabled, linkedin_canary_remaining, spend_cap_usd "
            "FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone() or {}

        ats_depth = _queue_counts(cur, "apply_queue")
        li_depth = _queue_counts(cur, "linkedin_queue")

        ats = {
            "queued": ats_depth.get("queued", 0),
            "leased": ats_depth.get("leased", 0),
            "applied": ats_depth.get("applied", 0),
            "failed": ats_depth.get("failed", 0),
            "blocked": ats_depth.get("blocked", 0),
            "crash_unconfirmed": ats_depth.get("crash_unconfirmed", 0),
            "approved": _approved_count(cur, "apply_queue"),
            "dedup_blocked": _dedup_blocked_count(cur, "apply_queue"),
            "leaseable": _leaseable_count(
                cur,
                "apply_queue",
                canary_enabled_column="canary_enabled",
                canary_column="canary_remaining",
            ),
            "canary_enabled": bool(cfg.get("canary_enabled")),
            "canary_remaining": cfg.get("canary_remaining"),
            "canary_exhausted": bool(cfg.get("canary_enabled"))
            and int(cfg.get("canary_remaining") or 0) <= 0,
            "paused": bool(cfg.get("paused")),
            "ats_paused": bool(cfg.get("ats_paused")),
        }
        linkedin = {
            "queued": li_depth.get("queued", 0),
            "leased": li_depth.get("leased", 0),
            "applied": li_depth.get("applied", 0),
            "failed": li_depth.get("failed", 0),
            "approved": _approved_count(cur, "linkedin_queue"),
            "dedup_blocked": _dedup_blocked_count(cur, "linkedin_queue"),
            "leaseable": _leaseable_count(
                cur,
                "linkedin_queue",
                canary_enabled_column="linkedin_canary_enabled",
                canary_column="linkedin_canary_remaining",
            ),
            "canary_enabled": bool(cfg.get("linkedin_canary_enabled")),
            "canary_remaining": cfg.get("linkedin_canary_remaining"),
            "canary_exhausted": bool(cfg.get("linkedin_canary_enabled"))
            and int(cfg.get("linkedin_canary_remaining") or 0) <= 0,
        }
    conn.rollback()

    if ats["paused"]:
        state = {
            "code": "paused",
            "severity": "halted",
            "reason": "Fleet is paused by the shared kill switch.",
        }
    elif ats["ats_paused"]:
        state = {"code": "ats_paused", "severity": "halted", "reason": "ATS lane is paused."}
    elif ats["canary_exhausted"]:
        state = {
            "code": "ats_canary_exhausted",
            "severity": "halted",
            "reason": "ATS canary is exhausted.",
        }
    elif ats["leaseable"] > 0:
        state = {
            "code": "ready_to_apply",
            "severity": "ok",
            "reason": "Leaseable ATS jobs are available.",
        }
    elif ats["approved"] > 0 and ats["dedup_blocked"] == ats["approved"]:
        state = {
            "code": "idle_no_leasable_jobs",
            "severity": "warn",
            "reason": "Approved queued ATS rows are already protected by applied_set dedup guards.",
        }
    else:
        state = {
            "code": "idle_no_leasable_jobs",
            "severity": "warn",
            "reason": "No leaseable ATS jobs are available.",
        }

    return {"state": state, "ats": ats, "linkedin": linkedin}
