"""Deterministic score audit and reranking.

The LLM fit score is useful, but it can over-reward generic management words.
This audit layer adds a role-lane check so target roles rank above unrelated
retail, service, clinical, logistics, and trade roles.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.config import SCORE_AUDIT_DIR
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)


TARGET_TITLE_PATTERNS: list[tuple[str, str, int]] = [
    (r"\bchief of staff\b|\bcos\b", "chief_of_staff", 55),
    (r"\bfounding operator\b|\bfounder'?s associate\b|\bfounders associate\b|\boperator\b", "operator", 35),
    (r"\bstrategy\b.*\boperations\b|\boperations\b.*\bstrategy\b|\bbusiness operations\b|\bbizops\b", "strategy_ops", 45),
    (r"\bstrategic initiatives?\b|\bcorporate strategy\b|\bbusiness strategy\b", "strategy", 35),
    (r"\bgtm\b|\bgo[- ]to[- ]market\b|\bsales strategy\b|\bsales operations\b|\brevenue operations\b|\brevops\b", "gtm_ops", 35),
    (r"\bstrategic finance\b|\bcorporate finance\b|\bfp&a\b|\bfinancial analyst\b|\bfinance data\b", "finance_analytics", 25),
    (r"\bbusiness development\b|\bpartnerships?\b|\bdeal architect\b", "business_development", 28),
    (r"\bproduct manager\b|\bprogram manager\b|\bproject manager\b|\bproject lead\b", "product_program", 26),
    (r"\bdata science analyst\b|\bdata analyst\b|\bbusiness analyst\b|\banalytics\b", "analytics", 22),
    (r"\bsales engineer\b|\bsolutions engineer\b|\bsolution engineer\b", "solutions", 22),
    (r"\bcoo\b|\bhead of operations\b|\boperations lead\b|\boperations manager\b", "operations_leadership", 24),
]

DESCRIPTION_SIGNAL_PATTERNS: list[tuple[str, int]] = [
    (r"\boperating cadence\b|\bexec(?:utive)? team\b|\bboard materials?\b|\binvestor\b", 8),
    (r"\bokrs?\b|\bkpis?\b|\bstrategic planning\b|\bplanning cycle\b", 7),
    (r"\bfinancial model(?:ing)?\b|\bforecast(?:ing)?\b|\bpricing strategy\b", 6),
    (r"\bgtm\b|\bgo[- ]to[- ]market\b|\brevenue\b|\bsalesforce\b", 6),
    (r"\bpipeline\b|\bprospect(?:ing)?\b|\bdiscovery\b|\baccount plan(?:ning)?\b|\benterprise sales\b", 6),
    (r"\bsolution consulting\b|\bsolutions consultant\b|\bvalue engineer\b|\bbusiness case\b|\broi\b", 6),
    (r"\bpartnerships?\b|\bchannel\b|\balliances?\b|\baccount executive\b|\bstakeholder management\b", 5),
    (r"\bdata analysis\b|\bsql\b|\bpython\b|\bmachine learning\b|\banalytics\b", 5),
    (r"\bstartup\b|\bfounder\b|\b0 to 1\b|\bhigh[- ]growth\b", 5),
]

HARD_NEGATIVE_TITLE_PATTERNS: list[tuple[str, str]] = [
    (r"\bassistant store manager\b|\bstore manager\b|\bretail store\b|\bretail manager\b", "retail_store"),
    (r"\bgrocery\b|\bcashier\b|\bbarista\b|\bdeli\b|\bbakery\b|\bliquor store\b", "frontline_retail"),
    (r"\brestaurant\b|\bcafe\b|\bserver\b|\bcook\b|\bhospitality\b|\bhotel\b", "hospitality"),
    (r"\bwarehouse\b|\bforklift\b|\bdriver\b|\bdelivery\b|\bmechanic\b|\btechnician\b", "logistics_trade"),
    (r"\bnurse\b|\bphysician\b|\bmedical assistant\b|\bclinical\b|\btherapist\b", "clinical"),
    (r"\bteacher\b|\btutor\b|\bprofessor\b|\binstructor\b", "education"),
    (r"\bsecurity guard\b|\bpolice\b|\bcorrectional\b", "security"),
]

SOFT_NEGATIVE_TITLE_PATTERNS: list[tuple[str, str, int]] = [
    (r"\bintern\b|\binternship\b|\bstudent\b|\btrainee\b|\bentry[- ]level\b", "too_junior", 25),
    (r"\bexecutive assistant\b|\badministrative assistant\b|\boffice assistant\b", "admin_assistant", 22),
    (r"\bcustomer support\b|\btechnical support\b|\bcall center\b", "support_lane", 14),
    (r"\baccount executive\b|\bsales representative\b|\bsdr\b|\bbdr\b", "pure_sales", 12),
]


@dataclass
class ScoreAudit:
    role_fit_score: int
    audit_score: float
    audit_label: str
    flags: list[str]
    reason: str


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _query_terms(search_cfg: dict) -> list[str]:
    terms: list[str] = []
    for item in search_cfg.get("queries", []) or []:
        query = item.get("query") if isinstance(item, dict) else item
        if query:
            terms.append(str(query).lower())
    return terms


def _custom_patterns(search_cfg: dict, key: str) -> list[str]:
    audit_cfg = search_cfg.get("score_audit", {}) or {}
    values = audit_cfg.get(key) or []
    return [str(v) for v in values if str(v).strip()]


def audit_job(job: dict[str, Any], search_cfg: dict | None = None) -> ScoreAudit:
    """Audit one scored job and compute an adjusted score."""
    search_cfg = search_cfg or {}
    title = _norm(job.get("title"))
    description = _norm(job.get("full_description") or job.get("description"))
    location = _norm(job.get("location"))
    combined = f"{title} {description}"
    base_score = float(job.get("fit_score") or 0)

    role_fit = 35
    flags: list[str] = []
    reasons: list[str] = []
    priority_title = False
    hard_negative = False

    for pattern, flag, points in TARGET_TITLE_PATTERNS:
        if _matches(pattern, title):
            role_fit += points
            flags.append(flag)
            reasons.append(f"title matches {flag.replace('_', ' ')}")
            if flag in {"chief_of_staff", "strategy_ops", "gtm_ops"}:
                priority_title = True

    for term in _query_terms(search_cfg):
        if len(term) >= 4 and term in title:
            role_fit += 12
            flags.append("query_title_match")
            reasons.append(f"title matches query '{term}'")
            break

    for pattern in _custom_patterns(search_cfg, "target_title_patterns"):
        if _matches(pattern, title):
            role_fit += 35
            flags.append("custom_target_title")
            reasons.append("title matches custom target pattern")

    signal_points = 0
    for pattern, points in DESCRIPTION_SIGNAL_PATTERNS:
        if _matches(pattern, combined):
            signal_points += points
    if signal_points:
        role_fit += min(signal_points, 20)
        flags.append("description_signal")
        reasons.append("description has strategy/analytics/finance signals")

    for pattern, flag in HARD_NEGATIVE_TITLE_PATTERNS:
        if _matches(pattern, title):
            role_fit -= 80
            hard_negative = True
            flags.extend([flag, "wrong_role_lane", "likely_false_positive"])
            reasons.append(f"title is outside target lane: {flag.replace('_', ' ')}")

    for pattern in _custom_patterns(search_cfg, "reject_title_patterns"):
        if _matches(pattern, title):
            role_fit -= 80
            hard_negative = True
            flags.extend(["custom_reject_title", "wrong_role_lane", "likely_false_positive"])
            reasons.append("title matches custom reject pattern")

    for pattern, flag, penalty in SOFT_NEGATIVE_TITLE_PATTERNS:
        if _matches(pattern, title):
            role_fit -= penalty
            flags.append(flag)
            reasons.append(f"title has weak-lane signal: {flag.replace('_', ' ')}")

    is_remote = any(token in location for token in ("remote", "anywhere", "work from home", "wfh"))
    for reject in ((search_cfg.get("location") or {}).get("reject_patterns") or []):
        reject_text = str(reject).lower()
        if reject_text and reject_text in location and not is_remote:
            role_fit -= 80
            hard_negative = True
            flags.extend(["location_rejected", "wrong_location", "likely_false_positive"])
            reasons.append(f"location matches reject pattern '{reject}'")
            break

    role_fit = max(0, min(100, role_fit))
    adjusted = base_score + ((role_fit - 50) / 18.0)

    if hard_negative:
        adjusted = min(adjusted, 3.0)
    elif priority_title:
        adjusted = max(adjusted, min(8.0, base_score + 0.8))
    elif "query_title_match" in flags:
        adjusted = max(adjusted, min(7.5, base_score + 0.4))

    adjusted = round(max(0.0, min(10.0, adjusted)), 1)

    if hard_negative or adjusted < 3.5:
        label = "exclude"
    elif priority_title and adjusted >= 6:
        label = "priority"
    elif adjusted >= 6.5:
        label = "recommended"
    elif adjusted >= 5.0:
        label = "review"
    else:
        label = "low"

    if base_score >= 5 and label == "exclude":
        flags.append("false_positive")
    if priority_title and base_score < 7:
        flags.append("missed_priority_role")
    if not reasons:
        reasons.append("no strong target or reject signal")

    deduped_flags = list(dict.fromkeys(flags))
    return ScoreAudit(
        role_fit_score=role_fit,
        audit_score=adjusted,
        audit_label=label,
        flags=deduped_flags,
        reason="; ".join(reasons[:5]),
    )


AUDIT_COLUMNS = [
    "audit_rank",
    "audit_score",
    "role_fit_score",
    "audit_label",
    "audit_flags",
    "fit_score",
    "title",
    "site",
    "location",
    "url",
    "application_url",
    "audit_reason",
    "score_reasoning",
]


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in AUDIT_COLUMNS})


def run_score_audit(
    output_dir: str | Path | None = None,
    min_score: int | None = None,
    limit: int = 0,
    write_reports: bool = True,
    reaudit: bool = False,
) -> dict[str, Any]:
    """Audit scored jobs, write audit fields to DB, and optionally export reports."""
    init_db()
    search_cfg = config.load_search_config()
    conn = get_connection()

    # Skip rows decided by the external recommendation engine: their audit_score
    # is authoritative and must not be overwritten by ApplyPilot's own audit.
    # (The score stage still runs on them, preserving fit_score as a benchmark.)
    where = "fit_score IS NOT NULL AND duplicate_of_url IS NULL AND decision_source IS NULL"
    params: list[Any] = []
    if not reaudit:
        where += " AND (audited_at IS NULL OR (scored_at IS NOT NULL AND audited_at < scored_at))"
    if min_score is not None:
        where += " AND fit_score >= ?"
        params.append(min_score)

    sql = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC, discovered_at DESC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    jobs = [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
    audited_rows: list[dict[str, Any]] = []
    for job in jobs:
        audit = audit_job(job, search_cfg=search_cfg)
        flags_json = json.dumps(audit.flags)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs
               SET role_fit_score = ?,
                   audit_score = ?,
                   audit_label = ?,
                   audit_flags = ?,
                   audit_reason = ?,
                   audited_at = ?
             WHERE url = ?
            """,
            (
                audit.role_fit_score,
                audit.audit_score,
                audit.audit_label,
                flags_json,
                audit.reason,
                now,
                job["url"],
            ),
        )
        conn.commit()
        audited_rows.append({
            "audit_score": audit.audit_score,
            "role_fit_score": audit.role_fit_score,
            "audit_label": audit.audit_label,
            "audit_flags": ", ".join(audit.flags),
            "fit_score": job.get("fit_score"),
            "title": job.get("title"),
            "site": job.get("site"),
            "location": job.get("location"),
            "url": job.get("url"),
            "application_url": job.get("application_url"),
            "audit_reason": audit.reason,
            "score_reasoning": job.get("score_reasoning"),
        })

    audited_rows.sort(key=lambda r: (r.get("audit_score") or 0, r.get("fit_score") or 0), reverse=True)
    for idx, row in enumerate(audited_rows, start=1):
        row["audit_rank"] = idx

    labels = Counter(row["audit_label"] for row in audited_rows)
    false_positives = [
        row for row in audited_rows
        if "false_positive" in str(row.get("audit_flags") or "")
    ]
    missed_priority = [
        row for row in audited_rows
        if "missed_priority_role" in str(row.get("audit_flags") or "")
    ]
    recommended = [
        row for row in audited_rows
        if row.get("audit_label") in {"priority", "recommended", "review"}
    ]

    result: dict[str, Any] = {
        "audited": len(audited_rows),
        "labels": dict(labels),
        "false_positives": len(false_positives),
        "missed_priority_roles": len(missed_priority),
        "recommended_review": len(recommended),
        "output_dir": None,
    }

    if not write_reports:
        return result

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else SCORE_AUDIT_DIR / timestamp
    destination.mkdir(parents=True, exist_ok=True)

    all_path = destination / "score_audit.csv"
    false_path = destination / "false_positives.csv"
    missed_path = destination / "missed_priority_roles.csv"
    recommended_path = destination / "recommended_review.csv"
    summary_path = destination / "summary.json"

    _write_csv(all_path, audited_rows)
    _write_csv(false_path, false_positives)
    _write_csv(missed_path, missed_priority)
    _write_csv(recommended_path, recommended)

    result.update({
        "output_dir": str(destination),
        "score_audit_csv": str(all_path),
        "false_positives_csv": str(false_path),
        "missed_priority_roles_csv": str(missed_path),
        "recommended_review_csv": str(recommended_path),
        "summary_json": str(summary_path),
    })
    summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info(
        "Score audit complete: %d audited, %d recommended/review, %d false positives, %d missed priority roles",
        result["audited"],
        result["recommended_review"],
        result["false_positives"],
        result["missed_priority_roles"],
    )
    return result
