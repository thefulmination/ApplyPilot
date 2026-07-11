from __future__ import annotations

import hashlib
import io
import json
from urllib.parse import quote

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


def test_magic_link_redactor_removes_standalone_encoded_fragment_and_path_tokens():
    encoded_once = "encoded%2Ftoken-ABC123456"
    encoded_twice = "encoded%252Ftoken-ABC123456"
    decoded = "encoded/token-ABC123456"
    fragment_token = "fragmentToken987654"
    path_token = "pathTokenABC987654321"
    hint = (
        "magic_link=https://boards.greenhouse.io/verify/"
        f"{path_token}?token={encoded_twice}&redirect=continue"
        f"#state={fragment_token}"
    )
    text = (
        f"{encoded_once} {encoded_twice} {decoded} "
        f"{fragment_token} {path_token} continue true"
    )

    redacted = launcher._redact_inbox_auth_secrets(text, hint)

    for secret in (
        encoded_once,
        encoded_twice,
        decoded,
        fragment_token,
        path_token,
    ):
        assert secret not in redacted
    assert "continue true" in redacted
    assert redacted.count(launcher.INBOX_AUTH_REDACTION) >= 5


def test_magic_link_redactor_covers_bounded_exact_credential_variants():
    hint = (
        "magic_link=https://user%2Bname:p%40ss@cred839214.greenhouse.io/verify/"
        "pathToken%2BABC987654?accessToken=x7&otp=9&note=ordinary%20text"
        "#verificationCode=frag%2B42"
    )
    sensitive = (
        "user%2Bname",
        "user+name",
        "user name",
        "p%40ss",
        "p@ss",
        "cred839214",
        "pathToken%2BABC987654",
        "pathToken+ABC987654",
        "pathToken ABC987654",
        "x7",
        "9",
        "frag%2B42",
        "frag%2b42",
        "frag+42",
        "frag 42",
        "frag%2042",
    )
    ordinary = "quick tokenization greenhouse.io ordinary text"
    text = " | ".join((*sensitive, ordinary))

    redacted = launcher._redact_inbox_auth_secrets(text, hint)

    for secret in sensitive:
        assert secret not in redacted
    assert ordinary in redacted
    assert "greenhouse.io" in redacted


def test_magic_link_redactor_treats_nontrivial_unknown_values_as_secrets():
    hint = (
        "magic_link=https://boards.greenhouse.io/verify?ticket=tick7&session=sess8"
        "&id=1234&custom=custom9&locale=x&redirect=continue#frag5"
    )
    secrets = ("tick7", "sess8", "1234", "custom9", "frag5")
    ordinary = "x continue ordinary output remains"

    redacted = launcher._redact_inbox_auth_secrets(
        " ".join((*secrets, ordinary)), hint
    )

    for secret in secrets:
        assert secret not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_inspects_late_sensitive_query_pair():
    benign = "&".join(f"note{i}=ordinary{i}" for i in range(60))
    query_token = "lateQueryTokenABC987654"
    fragment_token = "lateFragmentTokenABC987654"
    hint = (
        f"magic_link=https://boards.greenhouse.io/verify?{benign}"
        f"&token={query_token}#{benign}&verify={fragment_token}"
    )
    ordinary = "unrelated tokenization remains"

    redacted = launcher._redact_inbox_auth_secrets(
        f"{query_token} {fragment_token} {ordinary}", hint
    )

    assert query_token not in redacted
    assert fragment_token not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_collects_every_deep_percent_decode_layer():
    layers = ["deep/tokenABC987654"]
    for _ in range(32):
        layers.append(quote(layers[-1], safe=""))
    hint = f"magic_link=https://boards.greenhouse.io/verify?token={layers[-1]}"
    ordinary = "ordinary output remains unchanged"

    redacted = launcher._redact_inbox_auth_secrets(
        " | ".join((*layers, ordinary)), hint
    )

    for layer in layers:
        assert layer not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_parses_structures_at_every_url_and_hint_layer():
    benign_encoded_pairs = "".join(
        quote(f"&note{i}=ordinary{i}", safe="") for i in range(55)
    )
    nested_url = "https://boards.greenhouse.io/verify?token=n7"
    encoded_hint = quote(
        f"magic_link={quote(nested_url, safe='')}",
        safe="",
    )
    hints = (
        "magic_link=https://boards.greenhouse.io/verify?token%3Dq7",
        "magic_link=https://boards.greenhouse.io/verify%2Fp7",
        "magic_link=https://u7%3Ap8%40boards.greenhouse.io/verify",
        (
            "magic_link=https://boards.greenhouse.io/verify?note=ordinary"
            f"{benign_encoded_pairs}%26auth%3Dl7"
        ),
        "magic_link=https://boards.greenhouse.io/verify%3Ftoken%3Dt7",
        "magic_link=https://boards.greenhouse.io/verify%23verify%3Df7",
        encoded_hint,
    )
    credentials = ("q7", "p7", "u7", "p8", "l7", "t7", "f7", "n7")
    ordinary = "unrelated text and greenhouse.io remain"

    redacted = " ".join(credentials) + " " + ordinary
    for hint in hints:
        redacted = launcher._redact_inbox_auth_secrets(redacted, hint)

    for credential in credentials:
        assert credential not in redacted
    assert ordinary in redacted


def test_auth_hint_size_boundary_is_accepted():
    hint = "code=" + "a" * (launcher.MAX_INBOX_AUTH_HINT_BYTES - len("code="))
    prefix = "https://boards.greenhouse.io/verify?note="
    url = prefix + "a" * (launcher.MAX_INBOX_AUTH_HINT_BYTES - len(prefix))

    assert launcher._validate_inbox_auth_hint(hint) == hint
    assert url in launcher._magic_link_secrets(url)


def test_oversize_auth_hint_fails_closed_before_agent_use(monkeypatch):
    hint = "code=" + "a" * launcher.MAX_INBOX_AUTH_HINT_BYTES

    def unexpected(*_args, **_kwargs):
        raise AssertionError("oversize hint reached agent setup")

    monkeypatch.setattr(launcher.config, "resolve_resume_stem", unexpected)
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", unexpected)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", unexpected)
    monkeypatch.setattr(launcher, "reset_worker_dir", unexpected)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", unexpected)
    monkeypatch.setattr(launcher.subprocess, "Popen", unexpected)

    status, duration_ms = launcher._run_job_impl(
        _job(), port=9001, worker_id=7, inbox_auth_hint=hint
    )

    assert status == "failed:inbox_auth_hint_too_large"
    assert duration_ms == 0


def test_run_job_redacts_magic_link_from_every_persistent_sink_and_parses_result(
    tmp_path, monkeypatch
):
    standalone_token = "standaloneTokenABC987654"
    encoded_token = "encoded%252Ftoken-XYZ987654"
    decoded_token = "encoded/token-XYZ987654"
    short_token = "x7"
    userinfo = "user+name"
    subdomain_token = "cred839214"
    delimiter_token = "delimiterCredentialABC987"
    secret = (
        f"https://user%2Bname:pass%4042@{subdomain_token}.greenhouse.io/"
        "verify/pathTokenABC123456"
        f"?token={encoded_token}&verify={short_token}"
        f"%26custom%3D{delimiter_token}#state={standalone_token}"
    )
    hint = f"magic_link={secret}"
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Used {standalone_token}, {decoded_token}, {short_token}, "
                            f"{userinfo}, {subdomain_token}, and {delimiter_token}; "
                            "quick stays ordinary"
                        ),
                    },
                    {
                        "type": "tool_use",
                        "name": "mcp__playwright__browser_navigate",
                        "input": {
                            "url": (
                                f"https://tool.invalid/use/{encoded_token}/"
                                f"{userinfo}/{subdomain_token}/{short_token}/"
                                f"{delimiter_token}"
                            )
                        },
                    },
                ]
            },
        },
        {
            "type": "result",
            "result": (
                f"Used {standalone_token} {encoded_token} {short_token} "
                f"{userinfo} {subdomain_token} {delimiter_token}; "
                "quick stays ordinary\nRESULT:APPLIED"
            ),
            "usage": {},
        },
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
        for sensitive in (
            secret,
            standalone_token,
            encoded_token,
            decoded_token,
            short_token,
            userinfo,
            subdomain_token,
            delimiter_token,
        ):
            assert sensitive not in persisted
        assert launcher.INBOX_AUTH_REDACTION in persisted
        assert "quick stays ordinary" in persisted
    tool_actions = [update.get("last_action", "") for update in state_updates]
    assert all(
        sensitive not in action
        for action in tool_actions
        for sensitive in (
            secret,
            standalone_token,
            encoded_token,
            decoded_token,
            short_token,
            userinfo,
            subdomain_token,
            delimiter_token,
        )
    )
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
