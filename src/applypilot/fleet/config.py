"""fleet_config v3 helpers: approval policy, cost caps, version pins, kill switch.

fleet_config is the single-row (id=1) control table. v3 adds the approval gate
policy (R11), the LLM cost caps (R14), and the worker version pins (R12).
"""
from __future__ import annotations

import json
from typing import Any


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
    conn, version: str, *, canary_version: str | None = None, canary_worker_id: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET pinned_worker_version=%s, canary_version=%s, "
            "canary_worker_id=%s, updated_at=now() WHERE id=1",
            (version, canary_version, canary_worker_id),
        )
    conn.commit()


def version_for_worker(conn, worker_id: str) -> str | None:
    """The worker version this machine should run: the canary build if it is the
    canary target, else the fleet-pinned version (R12 staged rollout)."""
    cfg = get_config(conn)
    if cfg.get("canary_worker_id") == worker_id and cfg.get("canary_version"):
        return cfg["canary_version"]
    return cfg.get("pinned_worker_version")
