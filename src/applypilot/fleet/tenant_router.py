"""Pure tenant-aware application route decisions."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantRouteDecision:
    route: str
    reason: str
    submit_allowed: bool
    routing_required: bool = True


def route_tenant(*, tenant_status: str | None, session_state: str | None,
                 adapter_supported: bool, halted: bool = False) -> TenantRouteDecision:
    if tenant_status is None:
        if adapter_supported:
            return TenantRouteDecision("deterministic", "adapter_ready", True)
        return TenantRouteDecision("exception", "adapter_unsupported", False)
    if halted:
        return TenantRouteDecision("exception", "tenant_halted", False)
    if tenant_status == "excluded":
        return TenantRouteDecision("exception", "tenant_excluded", False)
    if session_state != "ready":
        if tenant_status == "supervised":
            return TenantRouteDecision("supervised_review", f"session_{session_state or 'supervised'}", False)
        return TenantRouteDecision("exception", f"session_{session_state or 'supervised'}", False)
    if not adapter_supported:
        return TenantRouteDecision("exception", "adapter_unsupported", False)
    if tenant_status == "supervised":
        return TenantRouteDecision("supervised_review", "tenant_supervised", False)
    if tenant_status == "trusted":
        return TenantRouteDecision("deterministic", "trusted_ready_adapter", True)
    return TenantRouteDecision("exception", "invalid_tenant_status", False)
