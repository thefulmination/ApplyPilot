from __future__ import annotations

from applypilot.fleet import version


def test_worker_version_includes_sanitized_branch_commit_and_dirty(monkeypatch):
    values = {
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("rev-parse", "--abbrev-ref", "HEAD"): "codex/fleet ops",
        ("status", "--porcelain"): " M file.py",
    }
    monkeypatch.setattr(version, "_git_text", lambda args: values.get(tuple(args)))

    assert version.worker_version() == "0.3.0+git.codex-fleet-ops.abc1234.dirty"


def test_worker_version_falls_back_to_package_version_without_git(monkeypatch):
    monkeypatch.setattr(version, "_git_text", lambda args: None)

    assert version.worker_version() == "0.3.0"
