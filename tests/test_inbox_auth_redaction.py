from __future__ import annotations

import hashlib
import io
import json

from applypilot.apply import launcher


class _FakeProc:
    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = iter(json.dumps(event) + "\n" for event in events)
        self.pid = 4242
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _job():
    return {
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "application_url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Test Role",
        "site": "Acme",
        "fit_score": 8,
        "tailored_resume_path": None,
    }


def test_redactor_removes_full_hint_and_credential_echoes():
    hint = "code=839214\nsender=no-reply@example.com\nsubject=Verify"
    text = f"hint was {hint}\nI entered 839214"

    redacted = launcher._redact_inbox_auth_secrets(text, hint)

    assert "839214" not in redacted
    assert "no-reply@example.com" not in redacted
    assert "subject=Verify" not in redacted
    assert launcher.INBOX_AUTH_REDACTION in redacted


def test_run_job_redacts_magic_link_from_every_persistent_sink_and_parses_result(
    tmp_path, monkeypatch
):
    secret = "https://boards.greenhouse.io/verify?token=super-secret"
    hint = f"magic_link={secret}"
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": f"Opened {secret}"},
                    {
                        "type": "tool_use",
                        "name": "mcp__playwright__browser_navigate",
                        "input": {"url": secret},
                    },
                ]
            },
        },
        {"type": "result", "result": f"Used {secret}\nRESULT:APPLIED", "usage": {}},
    ]
    state_updates = []

    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda _path: None)
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *_a, **_kw: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *_a, **_kw: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _worker: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **_kw: "prompt")
    monkeypatch.setattr(launcher, "_make_mcp_config", lambda _port: {})
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **_kw: ["agent"])
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_a, **_kw: _FakeProc(events))
    monkeypatch.setattr(
        launcher, "update_state", lambda *_a, **kwargs: state_updates.append(kwargs)
    )
    monkeypatch.setattr(launcher, "add_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(launcher, "get_state", lambda *_a, **_kw: None)
    monkeypatch.setattr(launcher, "_last_run_stats", {})

    status, _ = launcher._run_job_impl(
        _job(), port=9001, worker_id=7, inbox_auth_hint=hint
    )

    assert status == "applied"
    worker_log = (tmp_path / "logs" / "worker-7.log").read_text(encoding="utf-8")
    job_logs = list((tmp_path / "logs").glob("*_w7_*.txt"))
    assert len(job_logs) == 1
    job_log = job_logs[0].read_text(encoding="utf-8")
    stats = launcher._last_run_stats[7]
    for persisted in (worker_log, job_log, stats["transcript"]):
        assert secret not in persisted
        assert launcher.INBOX_AUTH_REDACTION in persisted
    tool_actions = [update.get("last_action", "") for update in state_updates]
    assert all(secret not in action for action in tool_actions)
    assert any(launcher.INBOX_AUTH_REDACTION in action for action in tool_actions)
    assert stats["transcript_digest"] == (
        "sha256:" + hashlib.sha256(job_log.encode("utf-8")).hexdigest()
    )


def test_local_hint_contains_only_the_credential():
    from datetime import datetime, timezone

    from applypilot import inbox_auth

    candidate = inbox_auth.VerificationCandidate(
        kind="code", value="123456", confidence="high", reasons=("test",)
    )
    match = inbox_auth.AuthEmailMatch(
        message_id="m",
        thread_id="t",
        sender="private-sender@example.com",
        subject="Private subject",
        received_at=datetime.now(timezone.utc).isoformat(),
        snippet="private",
        candidate=candidate,
        reasons=candidate.reasons,
    )

    assert launcher._format_inbox_auth_hint(match) == "code=123456"
