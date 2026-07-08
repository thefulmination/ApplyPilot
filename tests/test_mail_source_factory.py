"""Tests for the mail-source backend factory (no network -- fake Gmail service)."""

from applypilot import config
from applypilot.mail_source import GmailApiMailSource, ImapMailSource, get_mail_source


# ---------------------------------------------------------------------------
# get_mail_source()
# ---------------------------------------------------------------------------

def test_get_mail_source_returns_imap_when_app_password_configured(monkeypatch):
    monkeypatch.setattr(config, "load_gmail_app_password", lambda: ("a@b.com", "pw"))

    source = get_mail_source()

    assert isinstance(source, ImapMailSource)


def test_get_mail_source_returns_gmail_api_when_no_app_password(monkeypatch):
    monkeypatch.setattr(config, "load_gmail_app_password", lambda: None)

    source = get_mail_source()

    assert isinstance(source, GmailApiMailSource)


# ---------------------------------------------------------------------------
# GmailApiMailSource.fetch()
# ---------------------------------------------------------------------------

class _Execable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    def __init__(self, list_result, get_result):
        self._list_result = list_result
        self._get_result = get_result
        self.list_calls = []
        self.get_calls = []

    def list(self, userId, q, maxResults):
        self.list_calls.append((userId, q, maxResults))
        return _Execable(self._list_result)

    def get(self, userId, id, format):
        self.get_calls.append((userId, id, format))
        return _Execable(self._get_result)


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeGmailService:
    def __init__(self, list_result, get_result):
        self.messages_obj = _FakeMessages(list_result, get_result)

    def users(self):
        return _FakeUsers(self.messages_obj)


def _gmail_payload_with_text_plain(body_text: str) -> dict:
    import base64

    encoded = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "headers": [
            {"name": "Subject", "value": "Hi"},
            {"name": "From", "value": "x@y.com"},
            {"name": "Date", "value": "Wed, 01 Jul 2026 10:00:00 -0000"},
        ],
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }


def test_gmail_api_mail_source_fetch_maps_payload_to_mail_message():
    list_result = {"messages": [{"id": "1", "threadId": "t1"}]}
    get_result = {
        "id": "1",
        "threadId": "t1",
        "payload": _gmail_payload_with_text_plain("Hello body text."),
    }
    fake_service = _FakeGmailService(list_result, get_result)

    source = GmailApiMailSource(build_service=lambda: fake_service)
    result = source.fetch(since_days=7, max_messages=5)

    assert len(result) == 1
    msg = result[0]
    assert msg.id == "1"
    assert msg.thread_id == "t1"
    assert msg.subject == "Hi"
    assert msg.sender == "x@y.com"
    assert msg.date == "Wed, 01 Jul 2026 10:00:00 -0000"
    assert msg.body == "Hello body text."

    assert fake_service.messages_obj.list_calls == [("me", "newer_than:7d", 5)]
    assert fake_service.messages_obj.get_calls == [("me", "1", "full")]


def test_gmail_api_mail_source_fetch_uses_gmail_raw_query_when_provided():
    fake_service = _FakeGmailService({"messages": []}, {})

    source = GmailApiMailSource(build_service=lambda: fake_service)
    result = source.fetch(
        since_days=7,
        max_messages=5,
        gmail_raw_query='verification OR "magic link"',
    )

    assert result == []
    assert fake_service.messages_obj.list_calls == [
        ("me", 'newer_than:7d (verification OR "magic link")', 5)
    ]


def test_gmail_api_mail_source_fetch_returns_empty_when_no_messages():
    fake_service = _FakeGmailService({"messages": []}, {})

    source = GmailApiMailSource(build_service=lambda: fake_service)
    result = source.fetch(since_days=30, max_messages=10)

    assert result == []
