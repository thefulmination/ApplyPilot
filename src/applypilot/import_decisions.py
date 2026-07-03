"""Import apply decisions from the external recommendation engine ("brainstorm").

brainstorm reviews ApplyPilot's exported jobs (jobs.jsonl, keyed by `url`) and
decides which are worth applying to. This module ingests that curated apply list
and makes brainstorm's verdict AUTHORITATIVE for ApplyPilot's apply gate -- it
writes the decision into ``audit_score`` (which the gate prefers via
``COALESCE(audit_score, fit_score)``) and tags the row with ``decision_source``
so ApplyPilot's own audit stage skips it. ApplyPilot's LLM ``fit_score`` is left
untouched as a benchmark datapoint to compare against ``external_decision_score``.

Matching key: the exact job ``url`` (ApplyPilot's PRIMARY KEY), round-tripped
verbatim through the job export. URLs are never fuzzy-matched -- a non-match is
reported, not guessed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.database import get_connection, init_db, insert_discovered_job

log = logging.getLogger(__name__)

# Verdict strings that mean "apply to this job".
APPROVE_VERDICTS = {
    "approve", "approved", "apply", "yes", "strong_apply", "strong_yes",
    "shortlist", "shortlisted", "qualified",
}


def _rescale_score(value: Any, scale: str) -> float | None:
    """Rescale a brainstorm decision score into ApplyPilot's 1-10 audit band.

    scale: 'ten' (already 1-10), 'unit' (0-1), 'percent' (0-100), or 'auto'
    (infer from magnitude). Returns None if the value isn't numeric.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if scale == "auto":
        if v <= 1.0:
            scale = "unit"
        elif v <= 10.0:
            scale = "ten"
        else:
            scale = "percent"
    if scale == "unit":
        v *= 10.0
    elif scale == "percent":
        v /= 10.0
    # 'ten' -> already in band
    return max(0.0, min(10.0, v))


def _load_records(path: Path) -> list[dict]:
    """Load decision records from .jsonl (one object/line) or .json (array or an
    object with a records/decisions/jobs/approved array)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        records: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Skipping malformed JSONL line: %s", e)
                continue
            if isinstance(obj, dict):
                records.append(obj)
        return records
    data = json.loads(text)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("records", "decisions", "jobs", "approved", "items"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def _field(rec: dict, *names: str) -> Any:
    """First non-empty value among the given keys (tolerates brainstorm naming)."""
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return None


def _is_approved(rec: dict) -> tuple[bool, str]:
    verdict = _field(rec, "verdict", "decision", "label", "recommendation") or ""
    verdict_l = str(verdict).strip().lower().replace("-", "_").replace(" ", "_")
    approve_flag = _field(rec, "apply", "approved")
    approved = (approve_flag is True) or (verdict_l in APPROVE_VERDICTS)
    return approved, verdict_l


_DECISION_UPDATE = """
    UPDATE jobs
       SET audit_score = ?,
           audit_label = 'approved_external',
           audit_reason = ?,
           audited_at = ?,
           decision_source = ?,
           decision_verdict = ?,
           external_decision_score = ?,
           decision_at = ?,
           application_url = COALESCE(application_url, ?)
     WHERE url = ? AND applied_at IS NULL
"""


def import_decisions(
    path: str | Path,
    *,
    scale: str = "auto",
    default_source: str = "brainstorm",
) -> dict[str, Any]:
    """Ingest brainstorm's apply decisions. Returns a counts report.

    Args:
        path: A .jsonl or .json decision export.
        scale: Score scale of the input ('auto', 'ten', 'unit', 'percent').
        default_source: Tag written to decision_source when a record omits one.
    """
    init_db()
    conn = get_connection()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Decision file not found: {path}")

    records = _load_records(path)
    now = datetime.now(timezone.utc).isoformat()
    apply_threshold = config.get_min_score()

    counts: dict[str, Any] = {
        "records": len(records),
        "approved": 0,
        "updated": 0,
        "inserted": 0,
        "skipped_not_approved": 0,
        "skipped_no_url": 0,
        "skipped_no_score": 0,
        "skipped_duplicate": 0,
        "skipped_already_applied": 0,
        "not_found_insufficient_metadata": 0,
        "below_apply_threshold": 0,
        "manual_ats": 0,
    }
    not_found_urls: list[str] = []
    manual_ats_urls: list[str] = []

    for rec in records:
        approved, verdict_l = _is_approved(rec)
        if not approved:
            counts["skipped_not_approved"] += 1
            continue
        counts["approved"] += 1

        url = _field(rec, "url", "jobUrl", "job_url", "sourceUrl")
        if not url:
            counts["skipped_no_url"] += 1
            continue
        url = str(url).strip()

        gate_score = _rescale_score(
            _field(rec, "decision_score", "decisionScore", "score", "fitScore"), scale
        )
        if gate_score is None:
            counts["skipped_no_score"] += 1
            continue
        # external_decision_score always keeps the sender's raw (rescaled) score as the
        # benchmark datapoint; an explicit gate_score field may override what the apply
        # gate sees (e.g. resbuild_bridge floors approvals at the apply threshold).
        external_score = gate_score
        gate_override = _rescale_score(_field(rec, "gate_score", "gateScore"), scale)
        if gate_override is not None:
            gate_score = gate_override

        reason = str(_field(rec, "reason", "reasoning", "explanation", "notes") or "")[:2000]
        decided_at = _field(rec, "decided_at", "decidedAt", "decision_at") or now
        application_url = _field(rec, "application_url", "applicationUrl", "applyUrl")
        source = _field(rec, "source", "decision_source") or default_source

        cur = conn.execute(
            _DECISION_UPDATE,
            (gate_score, reason, decided_at, source, verdict_l, external_score,
             decided_at, application_url, url),
        )

        if cur.rowcount > 0:
            counts["updated"] += 1
        else:
            existing = conn.execute(
                "SELECT applied_at FROM jobs WHERE url = ?", (url,)
            ).fetchone()
            if existing is not None:
                # Row exists but the UPDATE matched 0 -> it was already applied.
                counts["skipped_already_applied"] += 1
                continue
            # ApplyPilot never discovered this URL -> insert from brainstorm
            # metadata (so dedupe_key/duplicate_of_url are computed), then decide.
            title = _field(rec, "title", "jobTitle")
            if not title:
                counts["not_found_insufficient_metadata"] += 1
                not_found_urls.append(url)
                continue
            full_description = _field(rec, "full_description", "fullDescription", "description")
            status = insert_discovered_job(
                conn,
                {
                    "url": url,
                    "title": title,
                    "company": _field(rec, "company", "site", "companyName"),
                    "application_url": application_url,
                    "full_description": full_description,
                    "detail_scraped_at": now if full_description else None,
                },
                source_board=source,
                strategy="brainstorm",
            )
            if status == "duplicate":
                # The gate excludes duplicate_of_url rows; don't decide on it.
                counts["skipped_duplicate"] += 1
                continue
            conn.execute(
                _DECISION_UPDATE,
                (gate_score, reason, decided_at, source, verdict_l, external_score,
                 decided_at, application_url, url),
            )
            counts["inserted"] += 1

        if gate_score < apply_threshold:
            counts["below_apply_threshold"] += 1

        apply_target = application_url or url
        if config.is_manual_ats(apply_target):
            counts["manual_ats"] += 1
            manual_ats_urls.append(str(apply_target))

    conn.commit()
    counts["not_found_urls"] = not_found_urls[:50]
    counts["manual_ats_urls"] = manual_ats_urls[:50]
    counts["apply_threshold"] = apply_threshold
    return counts
