from __future__ import annotations

from applypilot.fleet import version


def test_worker_version_uses_canonical_tree_identity(monkeypatch):
    monkeypatch.setattr(
        version, "current_sw_version", lambda: "0.3.0+git.tree.abc1234.dirty"
    )

    assert version.worker_version() == "0.3.0+git.tree.abc1234.dirty"


def test_worker_version_preserves_canonical_no_git_fallback(monkeypatch):
    monkeypatch.setattr(version, "current_sw_version", lambda: "0.3.0+git.unavailable")

    assert version.worker_version() == "0.3.0+git.unavailable"
