"""Agent-chain readiness helpers for fleet apply workers."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def default_apply_agent_chain(primary: str | None, fallback: str | None = None) -> list[str]:
    """Return the launch-time apply agent chain for a desired worker row.

    ``run-fleet-worker.ps1`` defaults a Claude primary worker to Codex fallback, while
    a Codex primary worker has no implicit fallback unless one is explicitly passed.
    """
    first = (primary or "claude").strip() or "claude"
    fallbacks = [a.strip() for a in (fallback or "").split(",") if a.strip()]
    if not fallbacks and first == "claude":
        fallbacks = ["codex"]

    chain: list[str] = []
    for agent in [first, *fallbacks]:
        if agent and agent not in chain:
            chain.append(agent)
    return chain


def blocked_desired_agent_chains(desired_rows: list[Any], active_blocks: Mapping[str, Any]) -> list[str]:
    """Human-readable blockers for desired workers whose entire agent chain is blocked."""
    blockers: list[str] = []
    blocks = {str(agent): row for agent, row in active_blocks.items()}

    for row in desired_rows:
        try:
            desired_workers = int(_field(row, "desired_workers", 0) or 0)
        except (TypeError, ValueError):
            desired_workers = 0
        if desired_workers <= 0:
            continue

        owner = _field(row, "machine_owner", "unknown") or "unknown"
        chain = default_apply_agent_chain(_field(row, "agent", "claude"), _field(row, "fallback_agent"))
        if chain and all(agent in blocks for agent in chain):
            details = []
            for agent in chain:
                block = blocks[agent]
                until = _field(block, "blocked_until")
                reason = _field(block, "reason")
                details.append(f"{agent} until {until} ({reason})")
            blockers.append(
                f"{owner} desired agent chain {','.join(chain)} is fully blocked: "
                + "; ".join(details)
            )
    return blockers
