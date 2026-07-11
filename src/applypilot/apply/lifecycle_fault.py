"""Durable interlock for uncertain ApplyPilot child-process lifecycle state."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config


class LifecycleHardFault(BaseException):
    """Escape ordinary job failure handling when a child may still be live."""


def lifecycle_hard_fault_marker() -> Path:
    return config.DB_PATH.parent / "keepalive.hard-fault.json"


def identity_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def persist_lifecycle_hard_fault(
    reason: str,
    *,
    pid: int = 0,
    created_at: float = 0.0,
    executable: str = "",
    command: str = "",
) -> Path:
    marker = lifecycle_hard_fault_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "reason": reason[:160],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": int(pid),
        "created_at": float(created_at),
        "executable_name": os.path.basename(executable) if executable else "",
        "executable_sha256": identity_digest(executable) if executable else "",
        "command_sha256": identity_digest(command) if command else "",
    }
    temp = marker.with_name(f"{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, marker)
    return marker
