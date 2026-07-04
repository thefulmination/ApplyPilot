"""LLM extraction of application-outcome fields from a single email, with a
deterministic heuristic fallback. Pure given an injected `client` (a duck-typed
object with a .chat(messages, **kw) -> str method, e.g. applypilot.llm.LLMClient)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from applypilot.gmail_outcomes import classify_email_outcome

STAGES = (
    "acknowledged", "screen", "assessment", "interview", "offer", "rejected", "position_filled", "other"
)
_OUTCOMES = ("offer", "rejected", "position_filled")
_CONFIDENCE = ("high", "medium", "low")

# classify_email_outcome label -> our stage vocabulary
_HEURISTIC_STAGE = {
    "offer": "offer", "interview": "interview", "rejected": "rejected",
    "position_filled": "position_filled",
    "acknowledged": "acknowledged", "ambiguous": "other", "not_job": "other",
}

_SYSTEM = (
    "You read one recruiting email about a job application and return STRICT JSON only. "
    "Fields: stage (one of acknowledged, screen, assessment, interview, offer, rejected, position_filled, other), "
    "outcome (offer, rejected, position_filled, or null), reason (short plain-text reason for a rejection/offer or null), "
    "title (the job title or null), company (the company or null), confidence (high, medium, low). "
    "acknowledged = an application-received receipt. screen = recruiter screen. assessment = a test/HackerRank. "
    "Return ONLY the JSON object, no prose."
)


@dataclass
class ExtractedOutcome:
    stage: str
    outcome: str | None
    reason: str | None
    title: str | None
    company: str | None
    confidence: str
    extracted_by: str


def _parse_json(text: str) -> dict | None:
    """Parse a JSON object out of a model reply (tolerant of surrounding prose)."""
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _heuristic(subject: str, body: str, sender: str) -> ExtractedOutcome:
    label, confidence, _ = classify_email_outcome(subject, body, sender)
    stage = _HEURISTIC_STAGE.get(label, "other")
    outcome = stage if stage in _OUTCOMES else None
    return ExtractedOutcome(
        stage=stage, outcome=outcome, reason=None, title=None, company=None,
        confidence=confidence if confidence in _CONFIDENCE else "low",
        extracted_by="heuristic_fallback",
    )


def extract_outcome(
    subject: str,
    body: str,
    sender: str = "",
    *,
    client=None,
) -> ExtractedOutcome:
    """Extract structured outcome fields from one email. Falls back to the
    deterministic heuristic classifier on any model/parse failure."""
    if client is None:
        try:
            from applypilot.llm import get_client
            client = get_client(stage="outcome_extract")
        except Exception:
            return _heuristic(subject, body, sender)

    user = f"SUBJECT: {subject}\nFROM: {sender}\nBODY:\n{body[:6000]}"
    try:
        reply = client.chat(
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=400,
            stage="outcome_extract",
        )
    except Exception:
        return _heuristic(subject, body, sender)

    obj = _parse_json(reply)
    if obj is None:
        return _heuristic(subject, body, sender)

    stage = str(obj.get("stage") or "").strip().lower()
    if stage not in STAGES:
        stage = "other"
    outcome = str(obj.get("outcome") or "").strip().lower()
    outcome = outcome if outcome in _OUTCOMES else None
    confidence = str(obj.get("confidence") or "").strip().lower()
    if confidence not in _CONFIDENCE:
        confidence = "medium"

    return ExtractedOutcome(
        stage=stage,
        outcome=outcome,
        reason=_clean(obj.get("reason")),
        title=_clean(obj.get("title")),
        company=_clean(obj.get("company")),
        confidence=confidence,
        extracted_by="llm",
    )
