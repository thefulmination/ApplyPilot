"""Deterministic import of historical research and reviewed outcome artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_json_records(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix.lower() == ".jsonl":
            values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                values = value
            elif isinstance(value, dict):
                values = next(
                    (value[key] for key in ("records", "events", "items", "rows") if isinstance(value.get(key), list)),
                    [value],
                )
            else:
                values = []
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in values if isinstance(row, dict)]


def _digest(rows: Iterable[dict[str, Any]]) -> str:
    payload = "\n".join(sorted(_canonical(row) for row in rows)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _files(root: Path, token: str, *, exclude: tuple[str, ...] = ()) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
        and token in path.name.lower() and not any(x in path.name.lower() for x in exclude)
    )


def _import_rows(
    conn: sqlite3.Connection,
    paths: list[Path],
    writer: Callable[[sqlite3.Connection, dict[str, Any]], bool],
) -> dict[str, Any]:
    rows = [row for path in paths for row in _read_json_records(path)]
    written = 0
    skipped = 0
    for row in sorted(rows, key=_canonical):
        if writer(conn, row):
            written += 1
        else:
            skipped += 1
    return {"read": len(rows), "written": written, "skipped": skipped, "sha256": _digest(rows)}


def _score(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    url = row.get("job_url") or row.get("jobUrl") or row.get("url")
    if not url or conn.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone() is None:
        return False
    scored_at = row.get("scored_at") or row.get("scoredAt")
    provider = row.get("provider")
    model = row.get("model")
    if not scored_at or not provider or not model:
        return False
    conn.execute(
        """
        INSERT INTO research_scores (
            job_url,item_id,provider,model,research_fit_score,research_decision,
            confidence,reason,positive_signals_json,gaps_json,evidence_node_ids_json,
            score_source,raw_fit_score,kg_version,scored_at,ingested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(job_url,provider,model,scored_at) DO UPDATE SET
            research_fit_score=excluded.research_fit_score,
            research_decision=excluded.research_decision,
            confidence=excluded.confidence,reason=excluded.reason,
            positive_signals_json=excluded.positive_signals_json,
            gaps_json=excluded.gaps_json,evidence_node_ids_json=excluded.evidence_node_ids_json,
            score_source=excluded.score_source,raw_fit_score=excluded.raw_fit_score,
            kg_version=excluded.kg_version
        """,
        (
            url, row.get("item_id") or row.get("itemId"), provider, model,
            row.get("research_fit_score", row.get("researchFitScore")),
            row.get("research_decision") or row.get("researchDecision"), row.get("confidence"),
            row.get("reason"), _canonical(row.get("positive_signals", row.get("positiveSignals", []))),
            _canonical(row.get("gaps", [])),
            _canonical(row.get("evidence_node_ids", row.get("evidenceNodeIds", []))),
            row.get("score_source") or row.get("scoreSource"), row.get("raw_fit_score"),
            row.get("kg_version") or row.get("kgVersion"), scored_at,
        ),
    )
    return True


def _label(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    event_id = row.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return False
    conn.execute(
        """
        INSERT INTO research_labels (
            id,job_url,item_id,source_project_id,decision,rating,reason,cleaned_reason,
            tags_json,method,fit_map_feedback_json,review_queue_json,item_status_at_review,
            created_at,raw_event_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET raw_event_json=excluded.raw_event_json
        """,
        (
            event_id, row.get("job_url") or row.get("jobUrl"), row.get("item_id") or row.get("itemId"),
            row.get("source_project_id") or row.get("sourceProjectId"), row.get("decision"),
            row.get("rating"), row.get("reason"), row.get("cleaned_reason") or row.get("cleanedReason"),
            _canonical(row.get("tags", [])), row.get("method"),
            _canonical(row.get("fit_map_feedback", row.get("fitMapFeedback", {}))),
            _canonical(row.get("review_queue", row.get("reviewQueue", {}))),
            row.get("item_status_at_review") or row.get("itemStatusAtReview"),
            row.get("created_at") or row.get("createdAt"), _canonical(row),
        ),
    )
    return True


def _pairwise(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    event_id = row.get("id")
    result = row.get("winner") or row.get("result")
    winner = {"a": "left", "b": "right"}.get(result, result)
    if not isinstance(event_id, str) or winner not in {"left", "right", "tie", "unclear"}:
        return False
    left_item_id = row.get("left_item_id") or row.get("leftItemId") or row.get("jobAItemId")
    right_item_id = row.get("right_item_id") or row.get("rightItemId") or row.get("jobBItemId")
    left_job_url = row.get("left_job_url") or row.get("leftJobUrl")
    right_job_url = row.get("right_job_url") or row.get("rightJobUrl")
    if not left_job_url and left_item_id:
        match = conn.execute(
            "SELECT job_url FROM research_labels WHERE item_id=? AND job_url IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (left_item_id,),
        ).fetchone()
        left_job_url = match[0] if match else None
    if not right_job_url and right_item_id:
        match = conn.execute(
            "SELECT job_url FROM research_labels WHERE item_id=? AND job_url IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (right_item_id,),
        ).fetchone()
        right_job_url = match[0] if match else None
    if not left_job_url or not right_job_url:
        return False
    conn.execute(
        """
        INSERT INTO research_pairwise_labels (
            id,left_job_url,right_job_url,left_item_id,right_item_id,winner,method,
            source_project_id,created_at,raw_event_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET raw_event_json=excluded.raw_event_json
        """,
        (
            event_id, left_job_url, right_job_url, left_item_id, right_item_id,
            winner, row.get("method"),
            row.get("source_project_id") or row.get("sourceProjectId"),
            row.get("created_at") or row.get("createdAt"), _canonical(row),
        ),
    )
    return True


def _outcome(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    from applypilot.outcome_review import classify_review_candidate

    event_id = row.get("event_id") or row.get("eventId") or row.get("message_id")
    job_url = row.get("job_url") or row.get("jobUrl")
    if not event_id or not job_url:
        return False
    if conn.execute("SELECT 1 FROM email_events WHERE message_id=?", (event_id,)).fetchone() is None:
        return False
    if conn.execute("SELECT 1 FROM jobs WHERE url=?", (job_url,)).fetchone() is None:
        return False
    latest_review = conn.execute(
        "SELECT resolution FROM email_event_reviews WHERE message_id=? ORDER BY id DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    existing = conn.execute(
        "SELECT review_status FROM reviewed_outcomes WHERE event_id=? AND job_url=?",
        (event_id, job_url),
    ).fetchone()
    resolution = latest_review[0] if latest_review else None
    if resolution in {"trusted", "corrected"} or (existing and existing[0] == "accepted"):
        review_status = "accepted"
    elif resolution == "ignored" or classify_review_candidate(
        sender=row.get("sender"), subject=row.get("subject")
    ) == "rejected":
        review_status = "rejected"
    else:
        review_status = "needs_review"
    conn.execute(
        """
        INSERT INTO reviewed_outcomes (
            event_id,job_url,attribution_json,review_status,normalized_stage,weight,
            reviewer,reason,created_at,reviewed_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id,job_url) DO UPDATE SET
            attribution_json=excluded.attribution_json,review_status=excluded.review_status,
            normalized_stage=excluded.normalized_stage,weight=excluded.weight,
            reviewer=excluded.reviewer,reason=excluded.reason,reviewed_at=excluded.reviewed_at,
            updated_at=excluded.updated_at
        """,
        (
            event_id, job_url, _canonical(row.get("attribution", row)), review_status,
            row.get("normalized_stage") or row.get("normalizedStage") or row.get("stage"),
            row.get("weight"), row.get("reviewer"), row.get("reason"),
            row.get("created_at") or row.get("createdAt") or row.get("occurred_at")
            or datetime.now(timezone.utc).isoformat(),
            row.get("reviewed_at") or row.get("reviewedAt"),
            row.get("updated_at") or row.get("updatedAt"),
        ),
    )
    return True


def _kg(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    version = (
        row.get("kg_version") or row.get("kgVersion") or row.get("version")
        or row.get("schemaVersion")
    )
    if not isinstance(version, str) or not version.strip():
        return False
    conn.execute(
        """
        INSERT INTO research_kg_artifacts (
            kg_version,compact_kg_json,built_at,input_label_count,inputs_sha
        ) VALUES (?,?,?,?,?)
        ON CONFLICT(kg_version) DO UPDATE SET
            compact_kg_json=excluded.compact_kg_json,built_at=excluded.built_at,
            input_label_count=excluded.input_label_count,inputs_sha=excluded.inputs_sha
        """,
        (
            version, _canonical(row), row.get("built_at") or row.get("generatedAt"),
            row.get("input_label_count") or row.get("inputLabelCount"),
            row.get("inputs_sha") or row.get("inputsSha") or hashlib.sha256(_canonical(row).encode()).hexdigest(),
        ),
    )
    return True


def backfill_research_artifacts(conn: sqlite3.Connection, fixture_dir: str | Path) -> dict[str, Any]:
    """Import every supported artifact deterministically and idempotently."""
    root = Path(fixture_dir)
    reports = {
        "scores": _import_rows(conn, _files(root, "score", exclude=("pairwise",)), _score),
        "labels": _import_rows(conn, _files(root, "label", exclude=("pairwise",)), _label),
        "pairwise": _import_rows(conn, _files(root, "pairwise"), _pairwise),
        "kg": _import_rows(conn, _files(root, "knowledge_graph") + _files(root, "compact_kg"), _kg),
        "outcomes": _import_rows(conn, _files(root, "outcome"), _outcome),
    }
    conn.commit()
    reports["combined_sha256"] = hashlib.sha256(
        _canonical({name: report["sha256"] for name, report in reports.items()}).encode("utf-8")
    ).hexdigest()
    return reports


def accepted_reviewed_outcomes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The only outcome rows permitted as model input."""
    rows = conn.execute(
        "SELECT * FROM reviewed_outcomes WHERE review_status='accepted' "
        "ORDER BY event_id, job_url"
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]
