"""Read-only agent/model routing view for the fleet console."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _chain_agents(chain: str | None) -> list[str]:
    if not chain:
        return []
    return [part.strip() for part in chain.replace(",", ">").split(">") if part.strip()]


def _num(v: Any) -> float:
    if isinstance(v, Decimal):
        return float(v)
    return float(v or 0)


def _verdict(workers: list[dict[str, Any]], availability: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not workers:
        return {"code": "unknown"}

    blocked_agents = {agent for agent, row in availability.items() if row["blocked"]}
    chain_agents = {
        agent
        for worker in workers
        for agent in _chain_agents(worker.get("agent_chain"))
    }
    if chain_agents and chain_agents.issubset(blocked_agents):
        return {"code": "all_agents_blocked"}

    if any((worker.get("last_agent_switch_reason") or "").startswith("switch:") for worker in workers):
        return {"code": "working"}
    if blocked_agents:
        return {"code": "partial"}
    return {"code": "not_triggered"}


def agent_summary(conn) -> dict[str, Any]:
    """Return current apply-agent routing, block state, and 24h apply spend."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, machine_owner, home_ip, role, state, last_beat, "
                "current_agent, current_model, agent_chain, last_agent_switch_at, "
                "last_agent_switch_reason "
                "FROM worker_heartbeat "
                "WHERE role = 'apply' "
                "ORDER BY worker_id"
            )
            workers = []
            for row in cur.fetchall():
                workers.append(
                    {
                        "worker_id": row.get("worker_id"),
                        "machine_owner": row.get("machine_owner"),
                        "home_ip": row.get("home_ip"),
                        "role": row.get("role"),
                        "state": row.get("state"),
                        "last_beat": _iso(row.get("last_beat")),
                        "current_agent": row.get("current_agent"),
                        "current_model": row.get("current_model"),
                        "agent_chain": row.get("agent_chain"),
                        "chain_agents": _chain_agents(row.get("agent_chain")),
                        "last_agent_switch_at": _iso(row.get("last_agent_switch_at")),
                        "last_agent_switch_reason": row.get("last_agent_switch_reason"),
                    }
                )

            cur.execute(
                "SELECT agent, blocked_until, reason, updated_at, "
                "(blocked_until IS NOT NULL AND blocked_until > now()) AS blocked "
                "FROM agent_availability "
                "ORDER BY agent"
            )
            availability = {
                row["agent"]: {
                    "blocked": bool(row.get("blocked")),
                    "blocked_until": _iso(row.get("blocked_until")),
                    "reason": row.get("reason"),
                    "updated_at": _iso(row.get("updated_at")),
                }
                for row in cur.fetchall()
            }

            cur.execute(
                "SELECT provider, model, SUM(cost_usd) AS cost_usd "
                "FROM llm_usage "
                "WHERE task = 'apply_agent' AND ts >= now() - interval '24 hours' "
                "GROUP BY provider, model "
                "ORDER BY provider, model"
            )
            spend_24h = [
                {
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "cost_usd": _num(row.get("cost_usd")),
                }
                for row in cur.fetchall()
            ]

        return {
            "workers": workers,
            "availability": availability,
            "spend_24h": spend_24h,
            "verdict": _verdict(workers, availability),
        }
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
