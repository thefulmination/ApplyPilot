"""Pure, transparent lane insights: response/positive rates by coarse segment vs
a baseline, with a sample-size floor and Wilson confidence intervals. NO learned
model -- every number is inspectable."""

from __future__ import annotations

import math
import re

_SENIORITY = [
    ("intern", ("intern", "internship")),
    ("lead", ("principal", "staff", "lead", "head of", "director", "vp", "chief")),
    ("senior", ("senior", "sr.", "sr ")),
    ("junior", ("junior", "jr.", "jr ", "associate", "entry")),
]
_ROLE_FAMILY = [
    ("quant", ("quant", "quantitative")),
    ("data", ("data scientist", "data analyst", "data engineer", "analytics")),
    ("software", ("software", "engineer", "developer", "swe")),
    ("research", ("research",)),
    ("product", ("product manager", "product owner")),
    ("trading", ("trader", "trading")),
    ("risk", ("risk",)),
]


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _score_band(job: dict) -> str:
    score = job.get("audit_score")
    if score is None:
        score = job.get("fit_score")
    if score is None:
        return "unknown"
    score = float(score)
    if score >= 8:
        return "8+"
    if score >= 7:
        return "7"
    if score >= 5:
        return "5-6"
    return "<5"


def _seniority(title: str) -> str:
    for label, kws in _SENIORITY:
        if any(k in title for k in kws):
            return label
    return "mid"


def _role_family(title: str) -> str:
    for label, kws in _ROLE_FAMILY:
        if any(k in title for k in kws):
            return label
    return "other"


def _location_bucket(location: str) -> str:
    if not location:
        return "unknown"
    if "remote" in location:
        return "remote"
    return "onsite"


def _salary_band(job: dict) -> str:
    raw = _norm(job.get("salary"))
    if not raw:
        return "unknown"
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$?\s*([\d,]{4,})", raw)]
    if not nums:
        return "unknown"
    top = max(nums)
    if top >= 200000:
        return "200k+"
    if top >= 150000:
        return "150-200k"
    if top >= 100000:
        return "100-150k"
    return "<100k"


def derive_segments(job: dict) -> dict[str, str]:
    """Coarse segment values for one applied job. High-cardinality fields
    (company, raw site) are intentionally excluded."""
    title = _norm(job.get("title"))
    return {
        "source_board": _norm(job.get("source_board")) or "unknown",
        "role_family": _role_family(title),
        "seniority": _seniority(title),
        "score_band": _score_band(job),
        "fit_gap_category": _norm(job.get("fit_gap_category")) or "unknown",
        "location_bucket": _location_bucket(_norm(job.get("location"))),
        "salary_band": _salary_band(job),
    }


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def compute_lane_insights(apps: list[dict], *, floor: int = 8) -> dict:
    """Aggregate response/positive rates by segment vs the overall baseline."""
    n = len(apps)
    base_resp = (sum(1 for a in apps if a["responded"]) / n) if n else 0.0
    base_pos = (sum(1 for a in apps if a["positive"]) / n) if n else 0.0

    # dimension -> value -> [n_applied, n_responded, n_positive]
    cells: dict[tuple[str, str], list[int]] = {}
    for a in apps:
        for dim, val in a["segments"].items():
            key = (dim, val)
            c = cells.setdefault(key, [0, 0, 0])
            c[0] += 1
            c[1] += 1 if a["responded"] else 0
            c[2] += 1 if a["positive"] else 0

    segments = []
    for (dim, val), (n_applied, n_responded, n_positive) in sorted(cells.items()):
        rate = n_responded / n_applied if n_applied else 0.0
        lo, hi = wilson_interval(n_responded, n_applied)
        if n_applied < floor:
            flag = "insufficient"
        elif lo > base_resp:
            flag = "warm"
        elif hi < base_resp:
            flag = "cold"
        else:
            flag = "none"
        segments.append({
            "dimension": dim, "value": val,
            "n_applied": n_applied, "n_responded": n_responded,
            "response_rate": round(rate, 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4),
            "n_positive": n_positive,
            "positive_rate": round(n_positive / n_applied, 4) if n_applied else 0.0,
            "flag": flag,
        })

    return {
        "n": n,
        "baseline_response_rate": round(base_resp, 4),
        "baseline_positive_rate": round(base_pos, 4),
        "segments": segments,
    }
