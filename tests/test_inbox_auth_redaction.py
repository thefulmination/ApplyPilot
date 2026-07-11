from __future__ import annotations

import hashlib
import io
import json
import re
from urllib.parse import quote

import pytest

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
    assert "continue" not in redacted
    assert "true" in redacted
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
        "ordinary%20text",
        "ordinary text",
    )
    ordinary = "quick tokenization greenhouse.io unrelated prose"
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
    secrets = ("tick7", "sess8", "1234", "custom9", "x", "continue", "frag5")
    ordinary = "unrelated output remains"

    redacted = launcher._redact_inbox_auth_secrets(
        " ".join((*secrets, ordinary)), hint
    )

    for secret in secrets:
        assert secret not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_removes_every_nonempty_structural_value():
    hint = (
        "magic_link=https://u:p@z.boards.greenhouse.io/verify/%61"
        ";mx=%62;%63?id=abc&short=%61&numeric=%31&empty=#%66"
    )
    secrets = ("u", "p", "z", "a", "b", "c", "abc", "1", "f")
    encoded = ("%61", "%62", "%63", "%31", "%66")
    ordinary = "unrelated words and boards.greenhouse.io remain"

    redacted = launcher._redact_inbox_auth_secrets(
        " | ".join((*secrets, *encoded, ordinary)), hint
    )

    for secret in secrets:
        assert re.search(
            rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])",
            redacted,
        ) is None
    for secret in encoded:
        assert secret not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_removes_ports_and_empty_valued_structural_keys():
    encoded_port = "%31%32%33%34%35"
    hint = (
        "magic_link=https://boards.greenhouse.io%3A"
        f"{encoded_port}/verify%3B%74icket%3D%3F%61bc%3D%23%78yz%3D"
    )
    secrets = ("12345", "ticket", "abc", "xyz")
    encoded = (encoded_port, "%74icket", "%61bc", "%78yz")
    ordinary = "unrelated labels and punctuation remain"

    redacted = launcher._redact_inbox_auth_secrets(
        " | ".join((*secrets, *encoded, ordinary)), hint
    )

    for secret in (*secrets, *encoded):
        assert secret not in redacted
    assert ordinary in redacted
    assert "" not in launcher._magic_link_secrets(hint.removeprefix("magic_link="))


def test_magic_link_redactor_preserves_literal_plus_and_form_decode_variants():
    hint = (
        "magic_link=https://boards.greenhouse.io/verify?ticket=Ab%2BcD9"
        "&padding=YWJjZA%3D%3D&slash=Ab%2FcD9&custom=Ab-cD9_"
    )
    secrets = (
        "Ab%2BcD9",
        "Ab%2bcD9",
        "Ab+cD9",
        "Ab cD9",
        "YWJjZA%3D%3D",
        "YWJjZA%3d%3d",
        "YWJjZA==",
        "Ab%2FcD9",
        "Ab%2fcD9",
        "Ab/cD9",
        "Ab-cD9_",
    )
    ordinary = "alpha+beta prose stays intact"

    redacted = launcher._redact_inbox_auth_secrets(
        " | ".join((*secrets, ordinary)), hint
    )

    for secret in secrets:
        assert secret not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_parses_query_and_path_matrix_parameters():
    nested = quote(
        "https://boards.greenhouse.io/path;custom=nested7?mode=ok;ticket=mixed8",
        safe="/:?=.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    )
    hints = (
        "magic_link=https://boards.greenhouse.io/verify?mode=ok;token=semi7",
        "magic_link=https://boards.greenhouse.io/verify%3Btoken%3Dencoded8",
        "magic_link=https://boards.greenhouse.io/path;ticket=matrix9;standalone10",
        f"magic_link={nested}",
    )
    secrets = ("semi7", "encoded8", "matrix9", "standalone10", "nested7", "mixed8")
    ordinary = "status; ready: ordinary punctuation remains"

    redacted = " | ".join((*secrets, ordinary))
    for hint in hints:
        redacted = launcher._redact_inbox_auth_secrets(redacted, hint)

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


def test_magic_link_redactor_parses_fragment_routes_at_every_url_layer():
    nested_url = (
        "https://boards.greenhouse.io/#/verify/nested9;mx=m2?token=t2"
    )
    nested = quote(quote(nested_url, safe=""), safe="")
    hints = (
        "magic_link=https://boards.greenhouse.io/#/verify/fragSecret987",
        "magic_link=https://boards.greenhouse.io/#/%76erify%2Ftoken",
        (
            "magic_link=https://boards.greenhouse.io/"
            "#/apply;ticket=matrix7?auth=query8&empty="
        ),
        "magic_link=https://boards.greenhouse.io/#route%5Cback9",
        f"magic_link={nested}",
    )
    secrets = (
        "fragSecret987",
        "token",
        "matrix7",
        "query8",
        "back9",
        "nested9",
        "m2",
        "t2",
        "%76erify",
    )
    ordinary = "unrelated prose and boards.greenhouse.io remain"

    redacted = " | ".join((*secrets, ordinary))
    for hint in hints:
        redacted = launcher._redact_inbox_auth_secrets(redacted, hint)

    for secret in secrets:
        assert secret not in redacted
    assert ordinary in redacted


def test_magic_link_redactor_recursively_inspects_nested_structured_values():
    inner_ticket = "innerTicket987"
    json_token = "jsonToken654"
    array_secret = "arraySecret321"
    combo_secret = "comboSecret852"
    relative_secret = "relativeSecret741"
    inner_url = f"https://nested.invalid/callback?ticket={inner_ticket}"
    payload = json.dumps(
        {
            "jsonKeyABC": json_token,
            "nestedArray": [{"arrayKeyXYZ": array_secret}, 731],
            "relative": f"/verify/{relative_secret}",
        },
        separators=(",", ":"),
    )
    combo = "next=" + quote(
        json.dumps({"comboKey": combo_secret}, separators=(",", ":")),
        safe="",
    )
    hint = (
        "magic_link=https://boards.greenhouse.io/verify"
        f"?redirect={quote(quote(inner_url, safe=''), safe='')}"
        f"&payload={quote(payload, safe='')}"
        f"&combo={quote(combo, safe='')}"
    )
    secrets = (
        inner_ticket,
        "ticket",
        json_token,
        "jsonKeyABC",
        "nestedArray",
        "arrayKeyXYZ",
        array_secret,
        "731",
        relative_secret,
        combo_secret,
        "comboKey",
    )
    ordinary = "unrelated prose remains intact"

    redacted = launcher._redact_inbox_auth_secrets(
        " | ".join((*secrets, ordinary)), hint
    )

    for secret in secrets:
        assert secret not in redacted
    assert ordinary in redacted
    assert "boards.greenhouse.io" not in launcher._magic_link_secrets(
        hint.removeprefix("magic_link=")
    )


def test_magic_link_redactor_rejects_excessive_unique_decode_work(monkeypatch):
    nested = "ticket=boundedSecret987"
    for _ in range(512):
        nested = quote(nested, safe="")
    hint = f"magic_link=https://boards.greenhouse.io/verify?redirect={nested}"
    monkeypatch.setattr(launcher, "_AUTH_SECRET_MATERIAL_MULTIPLIER", 1_000_000)

    with pytest.raises(
        launcher._InboxAuthHintRejected,
        match="inbox_auth_hint_too_complex",
    ):
        launcher._validate_inbox_auth_hint(hint)


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
    sink_short = "z1"
    sink_key = "k1"
    sink_port = "54321"
    fragment_route = "fr1"
    nested_ticket = "nestedTicket951"
    nested_json_token = "nestedJsonToken753"
    nested_array_token = "nestedArrayToken159"
    nested_redirect = quote(
        quote(
            f"https://nested.invalid/callback?ticket={nested_ticket}",
            safe="",
        ),
        safe="",
    )
    nested_payload = quote(
        json.dumps(
            {
                "payloadKey": nested_json_token,
                "items": [{"arrayKey": nested_array_token}],
            },
            separators=(",", ":"),
        ),
        safe="",
    )
    secret = (
        f"https://user%2Bname:pass%4042@{subdomain_token}.greenhouse.io:{sink_port}/"
        "verify/pathTokenABC123456"
        f"?token={encoded_token}&verify={short_token}"
        f"%26custom%3D{delimiter_token}%26id%3D{sink_short}%26{sink_key}%3D"
        f"&redirect={nested_redirect}&payload={nested_payload}"
        f"#/verify/{fragment_route}?state={standalone_token}"
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
                            f"{userinfo}, {subdomain_token}, {delimiter_token}, "
                            f"{sink_short}, {sink_key}, {sink_port}, and "
                            f"{fragment_route}, {nested_ticket}, "
                            f"{nested_json_token}, and {nested_array_token}; "
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
                                f"{delimiter_token}/{sink_short}/{sink_key}/{sink_port}/"
                                f"{fragment_route}/{nested_ticket}/{nested_json_token}/"
                                f"{nested_array_token}"
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
                f"{userinfo} {subdomain_token} {delimiter_token} {sink_short} "
                f"{sink_key} {sink_port} {fragment_route} {nested_ticket} "
                f"{nested_json_token} {nested_array_token}; "
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
            sink_short,
            sink_key,
            sink_port,
            fragment_route,
            nested_ticket,
            nested_json_token,
            nested_array_token,
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
            sink_short,
            sink_key,
            sink_port,
            fragment_route,
            nested_ticket,
            nested_json_token,
            nested_array_token,
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
