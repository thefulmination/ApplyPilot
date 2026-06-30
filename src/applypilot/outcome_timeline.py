"""Pure per-application timeline + latency metrics over email_events rows."""

from __future__ import annotations

from datetime import datetime

RESPONSE_STAGES = ("screen", "assessment", "interview", "offer", "rejected")
POSITIVE_STAGES = ("screen", "assessment", "interview", "offer")


def _dt(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _days(a: str | None, b: str | None) -> int | None:
    da, db = _dt(a), _dt(b)
    if da is None or db is None:
        return None
    return int((db - da).total_seconds() // 86400)


def build_timeline(applied_at: str | None, events: list[dict], now_iso: str) -> dict:
    """Build the ordered timeline and derived metrics for one application.

    responded = at least one event in RESPONSE_STAGES (a receipt alone does not count).
    positive  = reached any POSITIVE_STAGE or got an offer.
    """
    ordered = sorted(events, key=lambda e: e.get("occurred_at") or "")

    responses = [e for e in ordered if e.get("stage") in RESPONSE_STAGES]
    decisions = [e for e in ordered if e.get("outcome") in ("offer", "rejected")]
    positive = any(e.get("stage") in POSITIVE_STAGES for e in ordered) or \
        any(e.get("outcome") == "offer" for e in ordered)

    first_response_days = _days(applied_at, responses[0]["occurred_at"]) if responses else None
    decision = decisions[0] if decisions else None
    decision_days = _days(applied_at, decision["occurred_at"]) if decision else None

    current_stage = ordered[-1]["stage"] if ordered else "applied"
    last_at = ordered[-1]["occurred_at"] if ordered else applied_at
    silent_days = _days(last_at, now_iso)

    return {
        "ordered": ordered,
        "responded": bool(responses),
        "positive": positive,
        "current_stage": current_stage,
        "outcome": decision["outcome"] if decision else None,
        "decision_stage": decision["stage"] if decision else None,
        "first_response_days": first_response_days,
        "decision_days": decision_days,
        "silent_days": silent_days,
        "last_at": last_at,
    }
