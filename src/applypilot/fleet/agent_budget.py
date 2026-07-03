"""Fleet-wide apply-agent availability + predictive spend soft-blocks.

Two mechanisms share ONE channel, the ``agent_availability`` table:

  * Reactive, fleet-wide: a worker that hits a usage/session wall records the parsed
    reset time here, so EVERY worker skips that agent until it resets -- not just the one
    that discovered the wall (the fleet-wide upgrade over per-worker in-memory state).

  * Predictive: ``evaluate_soft_blocks`` sums each agent's rolling apply spend from
    ``llm_usage`` (attributed by ``provider`` = agent name) and, when it crosses that
    agent's configured soft cap, pre-emptively blocks it BEFORE it walls -- shifting new
    leases to the next agent in the chain. There is no published quota ceiling to anchor
    on, so the cap is an operator-set heuristic; this layers ON TOP of the reactive switch
    and never replaces it (spend is a proxy; the wall is still the ground truth).

All time lives in the DB (now()) so independent workers/monitors never disagree on a clock.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def record_block(conn, agent: str, blocked_until: datetime, reason: str) -> None:
    """Upsert an agent's block. Never SHORTENS an existing block -- GREATEST keeps the later
    of the existing and new times (a reactive wall's real reset outranks a shorter predictive
    cooldown), and the reason follows whichever time wins."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_availability (agent, blocked_until, reason, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (agent) DO UPDATE SET
                reason = CASE
                    WHEN EXCLUDED.blocked_until >= agent_availability.blocked_until
                    THEN EXCLUDED.reason ELSE agent_availability.reason END,
                blocked_until = GREATEST(EXCLUDED.blocked_until, agent_availability.blocked_until),
                updated_at = now()
            """,
            (agent, blocked_until, reason),
        )
    conn.commit()


def get_blocks(conn) -> dict[str, datetime]:
    """Currently-active blocks: {agent: blocked_until} for every agent still blocked at
    the DB's now(). Expired rows are omitted (self-healing -- no cleanup needed)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent, blocked_until FROM agent_availability "
            "WHERE blocked_until IS NOT NULL AND blocked_until > now()"
        )
        rows = cur.fetchall()
    try:
        conn.rollback()  # read-only
    except Exception:
        pass
    out: dict[str, datetime] = {}
    for row in rows:
        agent = row["agent"] if hasattr(row, "get") or _is_mapping(row) else row[0]
        bu = row["blocked_until"] if _is_mapping(row) else row[1]
        out[agent] = bu
    return out


def get_block_reason(conn, agent: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM agent_availability WHERE agent = %s", (agent,))
        row = cur.fetchone()
    try:
        conn.rollback()
    except Exception:
        pass
    if row is None:
        return None
    return row["reason"] if _is_mapping(row) else row[0]


def rolling_spend(conn, agent: str, *, window_seconds: float) -> float:
    """Total apply-agent spend attributed to ``agent`` (llm_usage.provider) within the
    trailing window. 0.0 when the agent has no rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_usage "
            "WHERE provider = %s AND ts > now() - make_interval(secs => %s)",
            (agent, window_seconds),
        )
        row = cur.fetchone()
    try:
        conn.rollback()
    except Exception:
        pass
    val = row["s"] if _is_mapping(row) else row[0]
    return float(val or 0)


def evaluate_soft_blocks(conn, *, soft_caps: dict[str, float], window_seconds: float,
                         cooldown_seconds: float) -> list[tuple[str, datetime]]:
    """For each agent with a POSITIVE soft cap whose rolling window spend has reached it,
    record a predictive block until now()+cooldown. Returns [(agent, blocked_until)] for the
    agents blocked this pass. A cap of 0 (or missing) disables the check for that agent."""
    actions: list[tuple[str, datetime]] = []
    for agent, cap in soft_caps.items():
        if not cap or cap <= 0:
            continue
        if rolling_spend(conn, agent, window_seconds=window_seconds) >= cap:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_availability (agent, blocked_until, reason, updated_at) "
                    "VALUES (%s, now() + make_interval(secs => %s), 'predictive_spend', now()) "
                    "ON CONFLICT (agent) DO UPDATE SET "
                    "  reason = CASE WHEN EXCLUDED.blocked_until >= agent_availability.blocked_until "
                    "           THEN EXCLUDED.reason ELSE agent_availability.reason END, "
                    "  blocked_until = GREATEST(EXCLUDED.blocked_until, agent_availability.blocked_until), "
                    "  updated_at = now() "
                    "RETURNING blocked_until",
                    (agent, cooldown_seconds),
                )
                row = cur.fetchone()
            conn.commit()
            bu = row["blocked_until"] if _is_mapping(row) else row[0]
            actions.append((agent, bu))
    return actions


def _is_mapping(row) -> bool:
    try:
        return hasattr(row, "keys")
    except Exception:
        return False
