"""Display helpers for fleet machine labels.

The database keeps stable machine_owner identifiers such as ``m2`` and ``m4``.
The console can still present human-readable names without changing queue logic
or worker registration state.
"""
from __future__ import annotations

from typing import Any


_DISPLAY_NAMES = {
    "home": "Home",
    "m2": "TARPON",
    "m4": "GGGTower",
    "mac": "Paloma Mac",
    "mac-mac": "Paloma Mac",
}


def infer_machine_owner(worker_id: Any, machine_owner: Any = None) -> str:
    raw_owner = "" if machine_owner is None else str(machine_owner).strip()
    if raw_owner:
        return raw_owner

    wid = "" if worker_id is None else str(worker_id).strip()
    low = wid.lower()
    if low.startswith("m2"):
        return "m2"
    if low.startswith("m4"):
        return "m4"
    if low.startswith("home-"):
        return "home"
    if low.startswith("mac-"):
        return "mac-Mac"
    if low in {"fleet_doctor", "watchdog"}:
        return "home"
    return "(unknown)"


def display_name(machine_owner: Any) -> str:
    raw = "" if machine_owner is None else str(machine_owner).strip()
    if not raw:
        return "(unknown)"
    return _DISPLAY_NAMES.get(raw.lower(), raw)
