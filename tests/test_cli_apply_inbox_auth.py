from __future__ import annotations

import os

from typer.testing import CliRunner

from applypilot import cli


def test_apply_command_inbox_auth_true_sets_env(monkeypatch) -> None:
    calls: dict[str, str] = {}

    def fake_mark_job(url: str, status: str, reason: str | None = None) -> None:
        calls["url"] = url
        calls["status"] = status
        calls["reason"] = reason or ""

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    from applypilot.apply import launcher as apply_launcher

    monkeypatch.setattr(apply_launcher, "mark_job", fake_mark_job)

    result = CliRunner().invoke(
        cli.app,
        ["apply", "--mark-applied", "https://example.com/job1", "--inbox-auth"],
    )

    assert result.exit_code == 0
    assert calls == {"url": "https://example.com/job1", "status": "applied", "reason": ""}
    assert os.environ.get("APPLYPILOT_INBOX_AUTH") == "1"


def test_apply_command_inbox_auth_false_sets_env(monkeypatch) -> None:
    calls: dict[str, str] = {}

    def fake_mark_job(url: str, status: str, reason: str | None = None) -> None:
        calls["status"] = status

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    from applypilot.apply import launcher as apply_launcher
    monkeypatch.setattr(apply_launcher, "mark_job", fake_mark_job)
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")

    result = CliRunner().invoke(
        cli.app,
        [
            "apply",
            "--mark-applied",
            "https://example.com/job2",
            "--no-inbox-auth",
        ],
    )

    assert result.exit_code == 0
    assert calls["status"] == "applied"
    assert os.environ.get("APPLYPILOT_INBOX_AUTH") == "0"


def test_apply_command_inbox_auth_flag_is_optional(monkeypatch) -> None:
    calls: dict[str, str] = {}

    def fake_mark_job(url: str, status: str, reason: str | None = None) -> None:
        calls["status"] = status

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    from applypilot.apply import launcher as apply_launcher
    monkeypatch.setattr(apply_launcher, "mark_job", fake_mark_job)
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")

    result = CliRunner().invoke(
        cli.app,
        ["apply", "--mark-applied", "https://example.com/job3"],
    )

    assert result.exit_code == 0
    assert calls["status"] == "applied"
    assert os.environ.get("APPLYPILOT_INBOX_AUTH") == "1"
