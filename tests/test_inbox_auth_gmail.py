from __future__ import annotations

import base64

from applypilot import inbox_auth


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeMessages:
    def __init__(self, message_batches, message_payloads):
        self._message_batches = message_batches
        self._message_payloads = message_payloads
        self._list_calls = 0

    def list(self, userId, q, maxResults):
        index = min(self._list_calls, len(self._message_batches) - 1)
        self._list_calls += 1
        return _FakeRequest({"messages": self._message_batches[index]})

    def get(self, userId, id, format):  # noqa: A002
        return _FakeRequest(self._message_payloads[id])


class _FakeUsers:
    def __init__(self, message_batches, message_payloads):
        self._messages = _FakeMessages(message_batches, message_payloads)

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, message_batches, message_payloads):
        self._users = _FakeUsers(message_batches, message_payloads)

    def users(self):
        return self._users


def _encoded_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _make_payload(subject: str, sender: str, body: str) -> dict:
    return {
        "headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": sender},
            {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
        ],
        "mimeType": "text/plain",
        "body": {"data": _encoded_body(body)},
    }


def _build_service(messages_by_poll, message_payloads):
    batches = messages_by_poll
    if batches and isinstance(batches[0], dict):
        batches = [batches]
    return _FakeService(
        batches,
        message_payloads,
    )


def test_scan_gmail_for_auth_codes_returns_only_high_confidence_matches():
    payload = _make_payload(
        subject="Your Greenhouse verification code",
        sender="no-reply@greenhouse.io",
        body="Use verification code 839214 to continue your application.",
    )
    service = _build_service(
        [{"id": "m1", "threadId": "t1"}],
        {"m1": {"payload": payload}},
    )
    matches = inbox_auth.scan_gmail_for_auth_codes(service=service)

    assert len(matches) == 1
    assert matches[0].message_id == "m1"
    assert matches[0].candidate.kind == "code"
    assert matches[0].candidate.value == "839214"


def test_watch_gmail_for_auth_code_polls_until_match(monkeypatch):
    payload = _make_payload(
        subject="Verify your email",
        sender="no-reply@greenhouse.io",
        body="Use verification code 123456 to continue your application.",
    )
    service = _build_service(
        [[{"id": "m-miss", "threadId": "t-empty"}], [{"id": "m-hit", "threadId": "t-match"}]],
        {
            "m-miss": {"payload": _make_payload(
                subject="Unrelated",
                sender="news@example.com",
                body="Thanks for signing in this time.",
            )},
            "m-hit": {"payload": payload},
        },
    )

    call_state = {"value": 0.0}

    def _fake_monotonic():
        call_state["value"] += 0.5
        return call_state["value"]

    monkeypatch.setattr(inbox_auth.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(inbox_auth.time, "sleep", lambda _sec: None)

    match = inbox_auth.watch_gmail_for_auth_code(
        service=service,
        timeout_seconds=10,
        poll_seconds=0,
        max_errors=3,
    )

    assert match is not None
    assert match.message_id == "m-hit"


def test_watch_gmail_for_auth_code_forwards_scan_window(monkeypatch):
    seen = {}
    service = _build_service([[]], {})

    def _fake_scan_gmail_for_auth_codes(*, service, minutes, max_messages):
        seen["service"] = service
        seen["minutes"] = minutes
        seen["max_messages"] = max_messages
        return []

    monkeypatch.setattr(inbox_auth, "scan_gmail_for_auth_codes", _fake_scan_gmail_for_auth_codes)

    state = {"at": -0.5}

    def _fake_monotonic():
        state["at"] += 0.5
        return state["at"]

    monkeypatch.setattr(inbox_auth.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(inbox_auth.time, "sleep", lambda _sec: None)

    result = inbox_auth.watch_gmail_for_auth_code(
        service=service,
        timeout_seconds=1,
        poll_seconds=0.1,
        max_errors=1,
        minutes=27,
        max_messages=11,
    )

    assert result is None
    assert seen["service"] is service
    assert seen["minutes"] == 27
    assert seen["max_messages"] == 11
