"""Fleet worker version helpers."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from applypilot import __version__


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_text(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _safe_version_part(value: str | None) -> str | None:
    if not value:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip(".-") or None


def worker_version() -> str:
    """Return a compact version string suitable for worker_heartbeat.sw_version."""
    commit = _git_text(["rev-parse", "--short", "HEAD"])
    if not commit:
        return __version__
    branch = _safe_version_part(_git_text(["rev-parse", "--abbrev-ref", "HEAD"]))
    dirty = ".dirty" if _git_text(["status", "--porcelain"]) else ""
    branch_part = f".{branch}" if branch else ""
    return f"{__version__}+git{branch_part}.{commit}{dirty}"
