"""Compatibility wrapper for the canonical fleet software identity."""
from __future__ import annotations

from applypilot.fleet.software_version import current_sw_version


def worker_version() -> str:
    """Return the same tree-derived identity used by every fleet role."""
    return current_sw_version()
