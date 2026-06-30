"""Pure mapping from a per-application outcome row to the tracker status it WOULD
imply -- WITHOUT writing anything. INERT by construction: no DB, no I/O, no
mutation. A future activation could route these decisions through
applications.record_application; that is deliberately NOT here."""

from __future__ import annotations

from typing import Any

_RESPONSE_STAGES = ("screen", "assessment", "interview", "offer", "rejected")
# stages that imply an active "recruiter_screen"-or-beyond advancement (no terminal outcome yet)
_ADVANCING_STAGES = ("screen", "assessment", "interview")


def _last_event_with_outcome(events: list[dict], outcome: str) -> dict | None:
    matches = [e for e in events if e.get("outcome") == outcome]
    return matches[-1] if matches else None


def _first_response_event(events: list[dict]) -> dict | None:
    ordered = sorted(events, key=lambda e: e.get("occurred_at") or "")
    for e in ordered:
        if e.get("stage") in _RESPONSE_STAGES:
            return e
    return None


def implied_status(row: dict[str, Any]) -> dict[str, Any] | None:
    """Given a build_application_rows row, return the tracker status it implies,
    or None for acknowledged/receipt-only/no-signal rows. Pure: returns a
    description; writes nothing."""
    events = row.get("events") or []
    outcome = row.get("outcome")
    stage = row.get("current_stage")

    if outcome == "offer":
        implied, src = "offer", _last_event_with_outcome(events, "offer")
    elif outcome == "rejected":
        implied, src = "rejected", _last_event_with_outcome(events, "rejected")
    elif stage in _ADVANCING_STAGES:
        implied, src = "recruiter_screen", _first_response_event(events)
    else:
        return None

    return {
        "job_url": row.get("job_url"),
        "implied_status": implied,
        "source_message_id": src.get("message_id") if src else None,
        "occurred_at": src.get("occurred_at") if src else None,
        "confidence": src.get("confidence") if src else None,
    }
