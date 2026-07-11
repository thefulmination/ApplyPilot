"""Trusted-only learning exports for the outcome-intelligence loop."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import APPLICATION_EXPORT_DIR
from applypilot.lane_insights import derive_segments
from applypilot.outcome_operator import build_operator_payload
from applypilot.outcome_dashboard import build_insights


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


def _score_band_report(rows: list[dict]) -> list[dict]:
    by_band: dict[str, dict] = defaultdict(lambda: {"n": 0, "responded": 0, "positive": 0})
    for row in rows:
        band = (row.get("segments") or {}).get("score_band", "unknown")
        by_band[band]["n"] += 1
        by_band[band]["responded"] += 1 if row.get("responded") else 0
        by_band[band]["positive"] += 1 if row.get("positive") else 0
    return [
        {
            "score_band": band,
            "applications": bucket["n"],
            "responded": bucket["responded"],
            "positive": bucket["positive"],
        }
        for band, bucket in sorted(by_band.items())
    ]


def _latency_report(rows: list[dict]) -> dict:
    first = [row["first_response_days"] for row in rows if row.get("first_response_days") is not None]
    decision = [row["decision_days"] for row in rows if row.get("decision_days") is not None]
    return {
        "applications": len(rows),
        "responded": sum(1 for row in rows if row.get("responded")),
        "positive": sum(1 for row in rows if row.get("positive")),
        "first_response_days": first,
        "decision_days": decision,
    }


def _recommendations(rows: list[dict], lane_report: dict) -> list[dict]:
    recommendations = []
    for seg in lane_report.get("warm", []):
        recommendations.append({
            "kind": "warm_lane_review",
            "dimension": seg["dimension"],
            "value": seg["value"],
            "response_rate": seg["response_rate"],
            "applications": seg["n_applied"],
        })
    for seg in lane_report.get("cold", []):
        recommendations.append({
            "kind": "cold_lane_review",
            "dimension": seg["dimension"],
            "value": seg["value"],
            "response_rate": seg["response_rate"],
            "applications": seg["n_applied"],
        })
    return recommendations


def export_learning_bundle(output_dir: str | Path | None = None, *, conn=None, now_iso: str | None = None) -> dict:
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    now_iso = now_iso or _now_iso()
    destination = Path(output_dir) if output_dir else APPLICATION_EXPORT_DIR / f"learning-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    destination.mkdir(parents=True, exist_ok=True)

    payload = build_operator_payload(conn, now_iso=now_iso)
    rows = payload["rows"]
    insights = build_insights(rows)
    lane_report = {
        "n": insights["n"],
        "baseline_response_rate": insights["baseline_response_rate"],
        "baseline_positive_rate": insights["baseline_positive_rate"],
        "segments": insights["segments"],
        "warm": [segment for segment in insights["segments"] if segment["flag"] == "warm"],
        "cold": [segment for segment in insights["segments"] if segment["flag"] == "cold"],
    }
    score_band_report = _score_band_report(rows)
    latency_report = _latency_report(rows)
    recommendations = _recommendations(rows, lane_report)

    _write_jsonl(destination / "trusted_outcome_timelines.jsonl", rows)
    _write_json(destination / "lane_report.json", lane_report)
    _write_json(destination / "score_band_report.json", score_band_report)
    _write_json(destination / "latency_report.json", latency_report)
    _write_jsonl(destination / "recommendations.jsonl", recommendations)

    summary = {
        "generated_at": now_iso,
        "trusted_rows": len(rows),
        "review_queue_count": payload["summary"]["review_queue"],
        "action_queue_count": payload["summary"]["action_queue"],
        "output_dir": str(destination),
    }
    _write_json(destination / "learning_summary.json", summary)
    return summary
