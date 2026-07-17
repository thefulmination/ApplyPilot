"""fleet_config v3 helpers: approval policy, cost caps, version pins, kill switch.

fleet_config is the single-row (id=1) control table. v3 adds the approval gate
policy (R11), the LLM cost caps (R14), and the worker version pins (R12).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MIN_APPROVAL_THRESHOLD = 5.8
_UNSET = object()


def get_config(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    return dict(row) if row else {}


def set_paused(conn, paused: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=%s, updated_at=now() WHERE id=1", (paused,))
    conn.commit()


def set_approval_policy(
    conn,
    *,
    min_fit: float | None = None,
    min_confidence: float | None = None,
    exclude_flags: list[str] | None = None,
    threshold: float | None = None,
    sampling_rate: float | None = None,
) -> None:
    """Set the auto-approve POLICY (R11). The auto-rule is multi-criteria:
    strong fit AND confident qualified-verdict AND no red-flags. A WIDE default
    band comes from the system *confidently qualifying many jobs*, not a low bar.
    """
    if threshold is not None and float(threshold) < MIN_APPROVAL_THRESHOLD:
        raise ValueError(f"approval threshold must be >= {MIN_APPROVAL_THRESHOLD:g}")
    policy = {
        "min_fit": min_fit,
        "min_confidence": min_confidence,
        "exclude_flags": list(exclude_flags or []),
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET approval_policy=%s, "
            "approval_threshold=COALESCE(%s, approval_threshold), "
            "approval_sampling_rate=COALESCE(%s, approval_sampling_rate), "
            "updated_at=now() WHERE id=1",
            (json.dumps(policy), threshold, sampling_rate),
        )
    conn.commit()


def get_approval_policy(conn) -> dict[str, Any]:
    cfg = get_config(conn)
    pol = cfg.get("approval_policy")
    if isinstance(pol, str):
        pol = json.loads(pol)
    return pol or {}


def set_cost_caps(conn, *, daily_usd: float | None = None, total_usd: float | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET cost_cap_daily_usd=COALESCE(%s, cost_cap_daily_usd), "
            "cost_cap_total_usd=COALESCE(%s, cost_cap_total_usd), updated_at=now() WHERE id=1",
            (daily_usd, total_usd),
        )
    conn.commit()


def set_pinned_version(
    conn,
    version: str,
    *,
    canary_version: str | None = None,
    canary_worker_id: str | None = None,
    ats_canary_version: str | None | object = _UNSET,
    ats_canary_worker_id: str | None | object = _UNSET,
    linkedin_canary_version: str | None | object = _UNSET,
    linkedin_canary_worker_id: str | None | object = _UNSET,
) -> None:
    assignments = [
        "pinned_worker_version=%s",
        "canary_version=%s",
        "canary_worker_id=%s",
    ]
    values: list[Any] = [version, canary_version, canary_worker_id]
    optional_pins = (
        ("ats_canary_version", ats_canary_version),
        ("ats_canary_worker_id", ats_canary_worker_id),
        ("linkedin_canary_version", linkedin_canary_version),
        ("linkedin_canary_worker_id", linkedin_canary_worker_id),
    )
    for column, value in optional_pins:
        if value is not _UNSET:
            assignments.append(f"{column}=%s")
            values.append(value)
    assignments.append("updated_at=now()")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE fleet_config SET {', '.join(assignments)} WHERE id=1", values)
    conn.commit()


def version_for_worker_config(
    cfg: Mapping[str, Any], worker_id: str, *, lane: str | None = None
) -> str | None:
    """Resolve a worker target from an already-loaded fleet config row.

    Application canaries are isolated by lane. The legacy canary remains the
    staged rollout target for callers without an application lane and for
    explicitly non-application lanes.
    """
    normalized_lane = "ats" if lane == "apply" else lane
    if normalized_lane in {"ats", "linkedin"}:
        worker_key = f"{normalized_lane}_canary_worker_id"
        version_key = f"{normalized_lane}_canary_version"
        if cfg.get(worker_key) == worker_id and cfg.get(version_key):
            return cfg[version_key]
        return cfg.get("pinned_worker_version")
    if cfg.get("canary_worker_id") == worker_id and cfg.get("canary_version"):
        return cfg["canary_version"]
    return cfg.get("pinned_worker_version")


def version_for_worker(conn, worker_id: str, *, lane: str | None = None) -> str | None:
    """Return the source version expected for this worker and optional lane."""
    return version_for_worker_config(get_config(conn), worker_id, lane=lane)


def reported_version_for_worker(conn, worker_id: str) -> str | None:
    """Return the last version this worker reported in its heartbeat, if any."""
    with conn.cursor() as cur:
        cur.execute("SELECT sw_version FROM worker_heartbeat WHERE worker_id=%s", (worker_id,))
        row = cur.fetchone()
    return row["sw_version"] if row else None


def version_status_for_worker(
    conn,
    worker_id: str,
    *,
    sw_version: str | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    """Compare a worker's running source version to its configured rollout target.

    A missing fleet pin means version gating is disabled for backwards-compatible
    local development. Once a pin is configured, a worker must report exactly the
    expected version for its identity and lane. Application lanes use their own
    canary pins; non-application workers use the generic staged rollout pin.
    """
    expected = version_for_worker(conn, worker_id, lane=lane)
    actual = sw_version if sw_version is not None else reported_version_for_worker(conn, worker_id)
    matches = not expected or actual == expected
    return {"expected_version": expected, "sw_version": actual, "matches": matches}


def worker_version_matches(
    conn, worker_id: str, *, sw_version: str | None = None, lane: str | None = None
) -> bool:
    """True when the worker may lease under the current source-version rollout gate."""
    return bool(
        version_status_for_worker(conn, worker_id, sw_version=sw_version, lane=lane)["matches"]
    )
