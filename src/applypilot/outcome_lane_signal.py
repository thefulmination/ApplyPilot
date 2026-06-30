"""Advisory, read-only lane signal over outcome rows. INERT: it describes which
coarse lanes respond above/below baseline; it is NEVER folded into fit_score /
audit_score / the apply gate. Pure (delegates to the existing build_insights)."""

from __future__ import annotations

from typing import Any

from applypilot.outcome_dashboard import build_insights

# Strength order so annotate_job_signal can pick the most actionable flag for a job.
_FLAG_RANK = {"warm": 3, "cold": 2, "none": 1, "insufficient": 0}


def compute_lane_report(rows: list[dict[str, Any]], *, floor: int = 8) -> dict[str, Any]:
    """Aggregate per-segment response rates vs baseline (delegates to build_insights),
    splitting the flagged segments into warm/cold for convenience."""
    insights = build_insights(rows, floor=floor)
    segments = insights["segments"]
    return {
        "n": insights["n"],
        "baseline_response_rate": insights["baseline_response_rate"],
        "baseline_positive_rate": insights["baseline_positive_rate"],
        "warm": [s for s in segments if s["flag"] == "warm"],
        "cold": [s for s in segments if s["flag"] == "cold"],
        "segments": segments,
    }


def annotate_job_signal(row: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """For one job row (with a `segments` dict), look up each of its lane values in
    the report and return the per-dimension flag + the single strongest flag. Pure;
    advisory only. Returns {flags: {dimension: flag}, top: flag}."""
    # index report segments by (dimension, value) -> flag
    flag_by_cell = {(s["dimension"], s["value"]): s["flag"] for s in report.get("segments", [])}
    segments = row.get("segments") or {}
    flags: dict[str, str] = {}
    for dim, val in segments.items():
        flags[dim] = flag_by_cell.get((dim, val), "insufficient")
    top = max(flags.values(), key=lambda f: _FLAG_RANK.get(f, 0)) if flags else "insufficient"
    return {"flags": flags, "top": top}
