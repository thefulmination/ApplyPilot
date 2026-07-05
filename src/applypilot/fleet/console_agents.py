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


def _configured_agents(worker: dict[str, Any]) -> list[str]:
    chain = _chain_agents(worker.get("agent_chain"))
    if chain:
        return chain
    current = worker.get("current_agent")
    return [current] if current else []


def _num(v: Any) -> float:
    if isinstance(v, Decimal):
        return float(v)
    return float(v or 0)


def _make_verdict(code: str, severity: str, reason: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "reason": reason}


def _verdict(
    workers: list[dict[str, Any]],
    availability: dict[str, dict[str, Any]],
    recent_usage: list[dict[str, Any]],
) -> dict[str, str]:
    if not workers:
        return _make_verdict("unknown", "warn", "No apply workers have reported heartbeat state.")

    blocked_agents = {agent for agent, row in availability.items() if row["blocked"]}
    chain_agents = {
        agent
        for worker in workers
        for agent in _configured_agents(worker)
    }
    if chain_agents and chain_agents.issubset(blocked_agents):
        return _make_verdict(
            "all_agents_blocked",
            "halted",
            "Every configured apply agent is currently blocked.",
        )

    switched_workers = [
        worker
        for worker in workers
        if (worker.get("last_agent_switch_reason") or "").startswith("switch:")
    ]
    if switched_workers and any(_has_scoped_activity(worker, recent_usage) for worker in switched_workers):
        return _make_verdict(
            "working",
            "ok",
            "Agent fallback is active and recent apply-agent spend confirms work on the fallback.",
        )
    if blocked_agents or switched_workers:
        return _make_verdict(
            "partial",
            "warn",
            "Agent blocks or switches exist, but recent fallback apply work is not confirmed.",
        )
    return _make_verdict(
        "not_triggered",
        "ok",
        "No active agent block requires fallback switching.",
    )


def _has_scoped_activity(worker: dict[str, Any], recent_usage: list[dict[str, Any]]) -> bool:
    worker_id = worker.get("worker_id")
    current_agent = worker.get("current_agent")
    current_model = worker.get("current_model")
    switch_at = worker.get("_last_agent_switch_at")
    if not worker_id or not current_agent:
        return False

    for row in recent_usage:
        if row.get("worker_id") != worker_id:
            continue
        if row.get("provider") != current_agent:
            continue
        if current_model and row.get("model") != current_model:
            continue
        if switch_at is not None and row.get("last_usage_at") and row["last_usage_at"] < switch_at:
            continue
        return True
    return False


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
                last_agent_switch_at = row.get("last_agent_switch_at")
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
                        "chain_agents": _configured_agents(row),
                        "last_agent_switch_at": _iso(last_agent_switch_at),
                        "_last_agent_switch_at": last_agent_switch_at,
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
                "SELECT provider, model, COUNT(*) AS count, SUM(cost_usd) AS cost_usd "
                "FROM llm_usage "
                "WHERE task = 'apply_agent' AND ts >= now() - interval '24 hours' "
                "GROUP BY provider, model "
                "ORDER BY provider, model"
            )
            spend_24h = [
                {
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "count": int(row.get("count") or 0),
                    "cost_usd": _num(row.get("cost_usd")),
                }
                for row in cur.fetchall()
            ]

            cur.execute(
                "SELECT worker_id, provider, model, COUNT(*) AS count, MAX(ts) AS last_usage_at "
                "FROM llm_usage "
                "WHERE task = 'apply_agent' AND ts >= now() - interval '24 hours' "
                "GROUP BY worker_id, provider, model"
            )
            recent_usage = [dict(row) for row in cur.fetchall()]

        public_workers = [
            {k: v for k, v in worker.items() if not k.startswith("_")}
            for worker in workers
        ]
        return {
            "workers": public_workers,
            "availability": availability,
            "spend_24h": spend_24h,
            "verdict": _verdict(workers, availability, recent_usage),
        }
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
