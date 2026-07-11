"""Durable interlock for uncertain ApplyPilot child-process lifecycle state."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from applypilot import config


class LifecycleHardFault(BaseException):
    """Escape ordinary job failure handling when a child may still be live."""


@dataclass(frozen=True)
class LoadedLifecycleFault:
    path: Path
    payload: dict
    raw: bytes
    legacy: bool


def legacy_lifecycle_hard_fault_marker() -> Path:
    return config.DB_PATH.parent / "keepalive.hard-fault.json"


def lifecycle_hard_fault_directory() -> Path:
    return config.DB_PATH.parent / "lifecycle-faults"


def lifecycle_hard_fault_paths() -> list[Path]:
    paths = []
    legacy = legacy_lifecycle_hard_fault_marker()
    if legacy.is_file():
        paths.append(legacy)
    directory = lifecycle_hard_fault_directory()
    if directory.is_dir():
        paths.extend(sorted(directory.glob("fault-*.json")))
    return paths


def enforce_no_lifecycle_faults() -> None:
    """Refuse a launch while any unresolved lifecycle fault exists."""
    paths = lifecycle_hard_fault_paths()
    if paths:
        raise LifecycleHardFault(
            f"{len(paths)} unresolved lifecycle hard-fault record(s); "
            "operator reconciliation is required before launch"
        )


def identity_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _payload_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def persist_lifecycle_hard_fault(
    reason: str,
    *,
    pid: int = 0,
    created_at: float = 0.0,
    executable: str = "",
    command: str = "",
) -> Path:
    fault_id = uuid.uuid4().hex
    directory = lifecycle_hard_fault_directory()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "fault_id": fault_id,
        "reason": reason[:160],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": int(pid),
        "created_at": float(created_at),
        "executable_name": os.path.basename(executable) if executable else "",
        "executable_sha256": identity_digest(executable) if executable else "",
        "command_sha256": identity_digest(command) if command else "",
    }
    payload["payload_digest"] = _payload_digest(payload)
    marker = directory / f"fault-{fault_id}.json"
    temp = directory / f".{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, marker)
    return marker


def require_browser_cleanup(
    cleanup: Callable[[int, object], object],
    worker_id: int,
    process: object,
) -> object:
    """Run browser cleanup and interlock every uncertain result."""
    pid = int(getattr(process, "pid", 0) or 0)
    try:
        result = cleanup(worker_id, process)
    except Exception as exc:
        persist_lifecycle_hard_fault("browser cleanup exception", pid=pid)
        raise LifecycleHardFault("browser cleanup could not be proven") from exc
    if result is False:
        persist_lifecycle_hard_fault("browser cleanup unproven", pid=pid)
        active_error = sys.exc_info()[1]
        if active_error is not None:
            raise LifecycleHardFault("browser cleanup could not be proven") from active_error
        raise LifecycleHardFault("browser cleanup could not be proven")
    return result


def load_lifecycle_hard_fault(path: Path) -> LoadedLifecycleFault:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("lifecycle fault payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("lifecycle fault payload is invalid")
    legacy = path == legacy_lifecycle_hard_fault_marker()
    if legacy:
        if not all(key in payload for key in ("reason", "timestamp", "pid")):
            raise ValueError("legacy lifecycle fault payload is incomplete")
        return LoadedLifecycleFault(path, payload, raw, True)

    fault_id = str(payload.get("fault_id") or "")
    digest = str(payload.get("payload_digest") or "")
    if not fault_id or path.name != f"fault-{fault_id}.json":
        raise ValueError("lifecycle fault identity is invalid")
    core = dict(payload)
    core.pop("payload_digest", None)
    if digest != _payload_digest(core):
        raise ValueError("lifecycle fault digest is invalid")
    return LoadedLifecycleFault(path, payload, raw, False)


def remove_lifecycle_fault_if_unchanged(path: Path, expected_raw: bytes) -> bool:
    """Atomically claim one fault and delete only the exact bytes already validated."""
    claim_dir = lifecycle_hard_fault_directory()
    claim_dir.mkdir(parents=True, exist_ok=True)
    claim = claim_dir / f".reconcile-{uuid.uuid4().hex}.tmp"
    try:
        os.replace(path, claim)
    except FileNotFoundError:
        return False
    try:
        claimed_raw = claim.read_bytes()
        if claimed_raw != expected_raw:
            preserved = claim_dir / f"fault-preserved-{uuid.uuid4().hex}.json"
            os.replace(claim, preserved)
            return False
        claim.unlink()
        return True
    finally:
        if claim.exists():
            preserved = claim_dir / f"fault-preserved-{uuid.uuid4().hex}.json"
            os.replace(claim, preserved)
