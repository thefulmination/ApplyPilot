"""Fail-closed local identity checks for fleet browser workers."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping


_RAILWAY_RUNTIME_MARKERS = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_PROJECT_NAME",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_SERVICE_NAME",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_REPLICA_ID",
    "RAILWAY_PUBLIC_DOMAIN",
    "RAILWAY_PRIVATE_DOMAIN",
    "RAILWAY_STATIC_URL",
    "RAILWAY_TCP_PROXY_DOMAIN",
)

_PLACEHOLDER_IDENTITIES = frozenset(
    {
        "",
        "0.0.0.0",
        "::",
        "none",
        "null",
        "placeholder",
        "railway",
        "unknown",
        "unset",
    }
)


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _require_real_identity(value: object, *, name: str) -> str:
    normalized = _normalized(value)
    if normalized.casefold() in _PLACEHOLDER_IDENTITIES:
        raise SystemExit(
            f"browser-host guard: {name} is missing or a placeholder; "
            "refusing unenrolled browser worker"
        )
    return normalized


def _require_node_ip(value: object) -> str:
    normalized = _require_real_identity(value, name="public IP")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        raise SystemExit(
            "browser-host guard: public IP is not a valid IP address; "
            "refusing unenrolled browser worker"
        ) from None
    if not address.is_global:
        raise SystemExit(
            "browser-host guard: public IP is not a globally routable node identity; "
            "refusing unenrolled browser worker"
        )
    return normalized


def require_enrolled_browser_host(
    *,
    machine_owner: object,
    public_ip: object,
    env: Mapping[str, str] | None = None,
) -> None:
    """Require a non-Railway host with matching enrolled node and IP identity."""
    runtime_env = os.environ if env is None else env
    marker = next((name for name in _RAILWAY_RUNTIME_MARKERS if name in runtime_env), None)
    if marker is not None:
        raise SystemExit(
            f"browser-host guard: Railway runtime marker {marker} is present; "
            "browser automation is restricted to enrolled fleet nodes"
        )

    node_label = _require_real_identity(
        runtime_env.get("APPLYPILOT_FLEET_LABEL"),
        name="APPLYPILOT_FLEET_LABEL",
    )
    owner = _require_real_identity(machine_owner, name="machine-owner")
    if owner.casefold() != node_label.casefold():
        raise SystemExit(
            f"browser-host guard: enrolled node '{node_label}' cannot run "
            f"machine-owner '{owner}' browser workers"
        )
    _require_node_ip(public_ip)


def require_linkedin_owner_host(
    *,
    machine_owner: object,
    public_ip: object,
    owner_ip: object,
    env: Mapping[str, str] | None = None,
) -> None:
    """Require the enrolled owner node and its exact configured public IP."""
    require_enrolled_browser_host(
        machine_owner=machine_owner,
        public_ip=public_ip,
        env=env,
    )
    actual_ip = _require_node_ip(public_ip)
    configured_owner_ip = _require_node_ip(owner_ip)
    if ipaddress.ip_address(actual_ip) != ipaddress.ip_address(configured_owner_ip):
        raise SystemExit(
            "browser-host guard: LinkedIn public IP does not match the owner IP; "
            "refusing non-owner driver"
        )
