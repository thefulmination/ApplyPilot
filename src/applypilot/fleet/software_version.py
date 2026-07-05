"""Fleet software identity helpers.

Workers report this value in ``worker_heartbeat.sw_version`` so health checks can
spot stale boxes before they behave differently from the rest of the fleet.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Callable

from applypilot import __version__


Runner = Callable[[list[str], Path], str]


@dataclass(frozen=True)
class GitIdentity:
    package_version: str
    branch: str | None
    commit: str | None
    dirty: bool
    git_available: bool
    tree: str | None = None


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout


def _clean_part(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return "unknown"
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-") or "unknown"


def git_identity(
    *,
    repo: Path | None = None,
    package_version: str = __version__,
    runner: Runner = _run,
) -> GitIdentity:
    root = repo or Path(__file__).resolve().parents[3]
    try:
        branch = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).strip()
        commit = runner(["git", "rev-parse", "HEAD"], root).strip()
        tree = runner(["git", "rev-parse", "HEAD^{tree}"], root).strip()
        dirty = bool(runner(["git", "status", "--porcelain", "--untracked-files=no"], root).strip())
    except Exception:
        return GitIdentity(
            package_version=package_version,
            branch=None,
            commit=None,
            dirty=False,
            git_available=False,
        )
    return GitIdentity(
        package_version=package_version,
        branch=branch if branch and branch != "HEAD" else "detached",
        commit=commit or None,
        dirty=dirty,
        git_available=True,
        tree=tree or None,
    )


def build_sw_version(identity: GitIdentity | None = None) -> str:
    ident = identity or git_identity()
    if not ident.git_available:
        return f"{ident.package_version}+git.unavailable"
    short = _clean_part((ident.tree or ident.commit or "")[:7])
    suffix = ".dirty" if ident.dirty else ""
    return f"{ident.package_version}+git.tree.{short}{suffix}"


def current_sw_version(*, repo: Path | None = None) -> str:
    return build_sw_version(git_identity(repo=repo))
