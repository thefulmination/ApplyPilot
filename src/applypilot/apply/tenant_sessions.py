"""Host-local ATS tenant browser-profile readiness registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from applypilot import config

SESSION_STATES = {"ready", "supervised", "expired"}
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,80}$")
_lock = threading.Lock()


def _registry_path() -> Path:
    return Path(os.environ.get("APPLYPILOT_TENANT_SESSION_REGISTRY") or config.APP_DIR / "tenant_sessions.json")


def _profiles_root() -> Path:
    return Path(os.environ.get("APPLYPILOT_TENANT_PROFILE_DIR") or config.CHROME_WORKER_DIR / "tenants")


def normalize_host(value: str) -> str:
    candidate = value if "://" in value else f"https://{value}"
    return (urlsplit(candidate).hostname or "").lower().strip(".")


def profile_id_for_host(host: str) -> str:
    normalized = normalize_host(host)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:45] or "tenant"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _load() -> dict:
    path = _registry_path()
    if not path.is_file():
        return {"version": 1, "sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"version": 1, "sessions": {}}
    return data if isinstance(data, dict) and isinstance(data.get("sessions"), dict) else {"version": 1, "sessions": {}}


def _save(data: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def set_session_state(
    host: str,
    state: str,
    *,
    profile_id: str | None = None,
    ttl_hours: int | None = None,
    reason: str | None = None,
) -> dict:
    normalized = normalize_host(host)
    if not normalized:
        raise ValueError("tenant host is required")
    if state not in SESSION_STATES:
        raise ValueError(f"invalid session state {state!r}")
    profile_id = profile_id or profile_id_for_host(normalized)
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError("invalid tenant profile id")
    now = datetime.now(timezone.utc)
    expires_at = (
        (now + timedelta(hours=max(1, int(ttl_hours)))).isoformat()
        if state == "ready" and ttl_hours is not None
        else None
    )
    record = {
        "host": normalized,
        "profile_id": profile_id,
        "state": state,
        "checked_at": now.isoformat(),
        "expires_at": expires_at,
        "reason": reason,
    }
    with _lock:
        data = _load()
        data["sessions"][normalized] = record
        _save(data)
    profile_dir = _profiles_root() / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    return {**record, "profile_dir": str(profile_dir)}


def select_session(host: str, *, profile_id: str | None = None) -> dict:
    normalized = normalize_host(host)
    requested = profile_id or profile_id_for_host(normalized)
    if not _PROFILE_ID_RE.fullmatch(requested):
        return {"host": normalized, "profile_id": requested, "state": "expired", "reason": "invalid_profile_id", "profile_dir": None}
    with _lock:
        data = _load()
        record = data["sessions"].get(normalized)
    if not record or record.get("profile_id") != requested:
        return set_session_state(normalized, "supervised", profile_id=requested, reason="login_required")

    profile_dir = _profiles_root() / requested
    state = record.get("state") if record.get("state") in SESSION_STATES else "expired"
    reason = record.get("reason")
    expires_at = record.get("expires_at")
    if state == "ready" and expires_at:
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires <= datetime.now(timezone.utc):
                state, reason = "expired", "session_ttl_expired"
        except ValueError:
            state, reason = "expired", "invalid_expiry"
    if state == "ready" and not profile_dir.is_dir():
        state, reason = "expired", "profile_missing"
    if state != record.get("state"):
        return set_session_state(normalized, state, profile_id=requested, reason=reason)
    return {**record, "state": state, "reason": reason, "profile_dir": str(profile_dir)}
