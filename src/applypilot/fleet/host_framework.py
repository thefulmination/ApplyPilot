"""Common zero-agent routing policy for ATS hosts without tenant records."""
from __future__ import annotations

import os


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def adapter_supported(host: str) -> bool:
    host = (host or "").lower().split(":", 1)[0]
    if host.endswith("greenhouse.io"):
        return _enabled("APPLYPILOT_GREENHOUSE_ADAPTER")
    if host == "jobs.ashbyhq.com" or host.endswith(".ashbyhq.com"):
        return _enabled("APPLYPILOT_ASHBY_ADAPTER")
    if host == "jobs.lever.co" or host.endswith(".lever.co"):
        return _enabled("APPLYPILOT_LEVER_BOUNDED_PATH")
    # Workday is deliberately absent: it requires a registered tenant and session.
    return False


def unregistered_host_policy(host: str) -> dict:
    supported = adapter_supported(host)
    return {
        "session_required": False,
        "tenant_profile_id": None,
        "routing_required": True,
        "execution_route": "deterministic" if supported else "exception",
        "host_policy": "adapter_ready" if supported else "adapter_unsupported",
    }
