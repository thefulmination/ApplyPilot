"""Tests for the mail-source backend factory (no network -- fake Gmail service)."""

import json

import pytest

from applypilot import config
from applypilot import mail_source
from applypilot.mail_source import (
    MAX_CONFIGURED_MAIL_SCAN_BYTES,
    GmailApiAuthMailSource,
    GmailApiMailSource,
    ImapMailSource,
    get_mail_source,
)


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


def test_get_auth_mail_source_returns_metadata_only_gmail_fallback(monkeypatch):
    monkeypatch.setattr(config, "load_gmail_app_password", lambda: None)

    assert isinstance(mail_source.get_auth_mail_source(), GmailApiAuthMailSource)


# ---------------------------------------------------------------------------
# GmailApiMailSource.fetch()
# ---------------------------------------------------------------------------

class _Execable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    def __init__(self, list_result, get_result, metadata_result=None):
        self._list_result = list_result
        self._get_result = get_result
        self._metadata_result = metadata_result
        self.list_calls = []
        self.get_calls = []
        self.metadata_calls = []

    def list(self, userId, q, maxResults, pageToken=None):
        self.list_calls.append((userId, q, maxResults, pageToken))
        if isinstance(self._list_result, list):
            result = self._list_result[len(self.list_calls) - 1]
        else:
            result = self._list_result
        return _Execable(result)

    def get(self, userId, id, format, fields=None, metadataHeaders=None):
        if callable(self._get_result):
            result = self._get_result(id)
        elif id in self._get_result:
            result = self._get_result[id]
        else:
            result = self._get_result
        if format == "metadata":
            self.metadata_calls.append((userId, id, format, fields))
            if self._metadata_result is not None:
                if callable(self._metadata_result):
                    return _Execable(self._metadata_result(id))
                per_message = self._metadata_result.get(id)
                return _Execable(
                    per_message if isinstance(per_message, dict) else self._metadata_result
                )
            return _Execable(
                {
                    "id": id,
                    "threadId": result.get("threadId", id),
                    "sizeEstimate": len(json.dumps(result).encode("utf-8")),
                }
            )
        self.get_calls.append((userId, id, format))
        return _Execable(result)


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeGmailService:
    def __init__(self, list_result, get_result, metadata_result=None):
        self.messages_obj = _FakeMessages(list_result, get_result, metadata_result)

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

    assert fake_service.messages_obj.list_calls == [("me", "newer_than:7d", 5, None)]
    assert fake_service.messages_obj.get_calls == [("me", "1", "full")]
    assert len(fake_service.messages_obj.metadata_calls) == 1


def test_gmail_auth_candidate_overflow_fails_before_message_get():
    pages = [
        {
            "messages": [{"id": str(index)} for index in range(500)],
            "nextPageToken": "page-2",
        },
        {
            "messages": [{"id": str(index)} for index in range(500, 1000)],
            "nextPageToken": "page-3",
        },
        {"messages": [{"id": "1000"}]},
    ]
    service = _FakeGmailService(pages, {})

    with pytest.raises(mail_source.MailSourceOverflowError):
        GmailApiAuthMailSource(build_service=lambda: service).fetch(
            since_days=7,
            max_messages=1000,
        )

    assert service.messages_obj.get_calls == []
    assert service.messages_obj.metadata_calls == []
    assert [call[2] for call in service.messages_obj.list_calls] == [500, 500, 1]


def test_gmail_auth_source_never_requests_full_or_raw_message():
    service = _FakeGmailService(
        {"messages": [{"id": "1", "threadId": "thread"}]},
        {},
        metadata_result={
            "id": "1",
            "threadId": "thread",
            "sizeEstimate": 100,
            "snippet": "Use verification code 246810 to continue.",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Verify your email"},
                    {"name": "From", "value": "no-reply@greenhouse.io"},
                    {"name": "Date", "value": "Wed, 01 Jul 2026 10:00:00 -0000"},
                ]
            },
        },
    )

    messages = GmailApiAuthMailSource(build_service=lambda: service).fetch(
        since_days=7,
        max_messages=1,
    )

    assert [message.body for message in messages] == [
        "Use verification code 246810 to continue."
    ]
    assert service.messages_obj.get_calls == []
    assert len(service.messages_obj.metadata_calls) == 1


def test_gmail_auth_first_candidate_exhausting_budget_with_remaining_overflows():
    first = {
        "id": "first",
        "threadId": "first",
        "sizeEstimate": 100,
        "snippet": "Use verification code 111111 to continue.",
        "payload": {"headers": []},
    }
    second = {
        "id": "second",
        "threadId": "second",
        "sizeEstimate": 100,
        "snippet": "Use verification code 222222 to continue.",
        "payload": {"headers": []},
    }
    first_size = mail_source._gmail_auth_metadata_bytes(first, limit=10_000)
    assert first_size is not None
    service = _FakeGmailService(
        {"messages": [{"id": "first"}, {"id": "second"}]},
        {},
        metadata_result={"first": first, "second": second},
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        GmailApiAuthMailSource(
            build_service=lambda: service,
            max_message_bytes=200,
            max_scan_bytes=first_size,
        ).fetch(since_days=7, max_messages=2)

    assert len(service.messages_obj.metadata_calls) == 1


def test_gmail_auth_exact_budget_without_remaining_candidate_succeeds():
    metadata = {
        "id": "only",
        "threadId": "only",
        "sizeEstimate": 100,
        "snippet": "Use verification code 333333 to continue.",
        "payload": {"headers": []},
    }
    actual_size = mail_source._gmail_auth_metadata_bytes(metadata, limit=10_000)
    assert actual_size is not None
    service = _FakeGmailService(
        {"messages": [{"id": "only"}]},
        {},
        metadata_result=metadata,
    )

    messages = GmailApiAuthMailSource(
        build_service=lambda: service,
        max_message_bytes=200,
        max_scan_bytes=actual_size,
    ).fetch(since_days=7, max_messages=1)

    assert [message.id for message in messages] == ["only"]


def test_gmail_auth_oversized_candidate_with_remaining_fails_closed():
    oversized = {
        "id": "oversized",
        "threadId": "oversized",
        "sizeEstimate": 10,
        "snippet": "A" * 100,
        "payload": {"headers": []},
    }
    normal = {
        "id": "normal",
        "threadId": "normal",
        "sizeEstimate": 10,
        "snippet": "Use verification code 444444 to continue.",
        "payload": {"headers": []},
    }
    service = _FakeGmailService(
        {"messages": [{"id": "oversized"}, {"id": "normal"}]},
        {},
        metadata_result={"oversized": oversized, "normal": normal},
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        GmailApiAuthMailSource(
            build_service=lambda: service,
            max_message_bytes=50,
            max_scan_bytes=200,
        ).fetch(since_days=7, max_messages=2)

    assert len(service.messages_obj.metadata_calls) == 1


def test_gmail_api_exact_candidate_bound_remains_bounded():
    pages = [
        {
            "messages": [{"id": str(index)} for index in range(500)],
            "nextPageToken": "page-2",
        },
        {"messages": [{"id": str(index)} for index in range(500, 1000)]},
    ]
    service = _FakeGmailService(
        pages,
        {},
        metadata_result=lambda message_id: {
            "id": message_id,
            "sizeEstimate": 2,
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=1,
        max_scan_bytes=1000,
    ).fetch(since_days=7, max_messages=1000)

    assert result == []
    assert len(service.messages_obj.metadata_calls) == 1000
    assert service.messages_obj.get_calls == []


def test_gmail_api_skips_oversize_message_without_full_fetch():
    full = {"id": "1", "payload": _gmail_payload_with_text_plain("secret")}
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result={"id": "1", "sizeEstimate": 101},
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=100,
        max_scan_bytes=200,
    ).fetch(since_days=7, max_messages=1)

    assert result == []
    assert service.messages_obj.get_calls == []
    assert len(service.messages_obj.metadata_calls) == 1


def test_gmail_api_accepts_exact_byte_boundary():
    full = {
        "id": "1",
        "payload": {
            "headers": [],
            "mimeType": "text/plain",
            "body": {"data": "AAAA"},
        },
    }
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result={"id": "1", "sizeEstimate": 14},
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=14,
        max_scan_bytes=14,
    ).fetch(since_days=7, max_messages=1)

    assert [message.id for message in result] == ["1"]
    assert service.messages_obj.get_calls == [("me", "1", "full")]


def test_gmail_api_aggregate_cap_stops_further_full_fetches():
    refs = [{"id": str(index)} for index in range(3)]
    full = {
        str(index): {
            "id": str(index),
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "AAAA"},
            },
        }
        for index in range(3)
    }
    service = _FakeGmailService(
        {"messages": refs},
        full,
        metadata_result={
            str(index): {"id": str(index), "sizeEstimate": 10}
            for index in range(3)
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=14,
        max_scan_bytes=20,
    ).fetch(since_days=7, max_messages=3)

    assert [message.id for message in result] == ["0"]
    assert service.messages_obj.get_calls == [
        ("me", "0", "full"),
        ("me", "1", "full"),
    ]


def test_gmail_api_understated_oversize_sequence_charges_aggregate_budget():
    refs = [{"id": str(index)} for index in range(8)]
    full = {
        str(index): {
            "id": str(index),
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "A" * 200},
            },
        }
        for index in range(8)
    }
    service = _FakeGmailService(
        {"messages": refs},
        full,
        metadata_result={
            str(index): {"id": str(index), "sizeEstimate": 10}
            for index in range(8)
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=100,
        max_scan_bytes=200,
    ).fetch(since_days=7, max_messages=8)

    assert result == []
    assert service.messages_obj.get_calls == [("me", "0", "full")]


def test_gmail_api_oversize_discard_retains_exact_aggregate_charge():
    refs = [{"id": str(index)} for index in range(3)]
    full = {
        "0": {
            "id": "0",
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "A" * 20},
            },
        },
        "1": {
            "id": "1",
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "AAAA"},
            },
        },
        "2": {
            "id": "2",
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "AAAA"},
            },
        },
    }
    service = _FakeGmailService(
        {"messages": refs},
        full,
        metadata_result={
            str(index): {"id": str(index), "sizeEstimate": 10}
            for index in range(3)
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=20,
        max_scan_bytes=44,
    ).fetch(since_days=7, max_messages=3)

    assert [message.id for message in result] == ["1"]
    assert service.messages_obj.get_calls == [
        ("me", "0", "full"),
        ("me", "1", "full"),
    ]


def test_gmail_api_malformed_full_payload_consumes_remaining_scan_budget():
    service = _FakeGmailService(
        {"messages": [{"id": "bad"}, {"id": "normal"}]},
        {
            "bad": {
                "id": "bad",
                "payload": {
                    "headers": [],
                    "mimeType": "multipart/mixed",
                    "body": {},
                    "parts": "not-a-list",
                },
            },
            "normal": {
                "id": "normal",
                "payload": {
                    "headers": [],
                    "mimeType": "text/plain",
                    "body": {"data": "AAAA"},
                },
            },
        },
        metadata_result={
            "bad": {"id": "bad", "sizeEstimate": 10},
            "normal": {"id": "normal", "sizeEstimate": 10},
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=100,
        max_scan_bytes=200,
    ).fetch(since_days=7, max_messages=2)

    assert result == []
    assert service.messages_obj.get_calls == [("me", "bad", "full")]


def test_gmail_api_normal_sequence_uses_exact_actual_aggregate_bytes():
    refs = [{"id": str(index)} for index in range(3)]
    full = {
        str(index): {
            "id": str(index),
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "AAAA"},
            },
        }
        for index in range(3)
    }
    service = _FakeGmailService(
        {"messages": refs},
        full,
        metadata_result={
            str(index): {"id": str(index), "sizeEstimate": 10}
            for index in range(3)
        },
    )

    result = GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=14,
        max_scan_bytes=28,
    ).fetch(since_days=7, max_messages=3)

    assert [message.id for message in result] == ["0", "1"]
    assert service.messages_obj.get_calls == [
        ("me", "0", "full"),
        ("me", "1", "full"),
    ]


def test_gmail_api_rejects_understated_oversize_full_payload():
    full = {
        "id": "1",
        "payload": {
            "headers": [],
            "mimeType": "text/plain",
            "body": {"data": "A" * 200},
        },
    }
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result={"id": "1", "sizeEstimate": 10},
    )

    assert GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=100,
        max_scan_bytes=200,
    ).fetch(since_days=7, max_messages=1) == []


def test_gmail_api_rejects_oversize_nested_attachment_payload():
    full = {
        "id": "1",
        "snippet": "verify",
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                {
                    "headers": [],
                    "mimeType": "application/pdf",
                    "filename": "resume.pdf",
                    "body": {"attachmentId": "att-1", "size": 500},
                }
            ],
        },
    }
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result={"id": "1", "sizeEstimate": 10},
    )

    assert GmailApiMailSource(
        build_service=lambda: service,
        max_message_bytes=200,
        max_scan_bytes=400,
    ).fetch(since_days=7, max_messages=1) == []


def test_gmail_api_rejects_malformed_full_payload_before_parsing():
    full = {
        "id": "1",
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": "not-a-list",
        },
    }
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result={"id": "1", "sizeEstimate": 10},
    )

    assert GmailApiMailSource(build_service=lambda: service).fetch(
        since_days=7, max_messages=1
    ) == []


@pytest.mark.parametrize("size", [None, "10", -1, True])
def test_gmail_api_malformed_size_fails_closed_without_full_fetch(size):
    full = {"id": "1", "payload": _gmail_payload_with_text_plain("secret")}
    metadata = {"id": "1"}
    if size is not None:
        metadata["sizeEstimate"] = size
    service = _FakeGmailService(
        {"messages": [{"id": "1"}]},
        full,
        metadata_result=metadata,
    )

    assert GmailApiMailSource(build_service=lambda: service).fetch(
        since_days=7, max_messages=1
    ) == []
    assert service.messages_obj.get_calls == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, MAX_CONFIGURED_MAIL_SCAN_BYTES + 1])
def test_mail_source_rejects_unbounded_or_malformed_byte_configuration(limit):
    with pytest.raises((TypeError, ValueError), match="max_scan_bytes"):
        GmailApiMailSource(max_scan_bytes=limit)


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
        ("me", 'newer_than:7d (verification OR "magic link")', 5, None)
    ]


def test_gmail_api_mail_source_fetch_returns_empty_when_no_messages():
    fake_service = _FakeGmailService({"messages": []}, {})

    source = GmailApiMailSource(build_service=lambda: fake_service)
    result = source.fetch(since_days=30, max_messages=10)

    assert result == []


def test_gmail_api_mail_source_nonpositive_budget_skips_api_calls():
    fake_service = _FakeGmailService({"messages": [{"id": "1"}]}, {})
    source = GmailApiMailSource(build_service=lambda: fake_service)

    assert source.fetch(since_days=7, max_messages=0) == []
    assert source.fetch(since_days=7, max_messages=-1) == []
    assert fake_service.messages_obj.list_calls == []
    assert fake_service.messages_obj.get_calls == []


@pytest.mark.parametrize("budget", [True, 1.5, float("nan"), float("inf"), "1.5", "nan", ""])
def test_gmail_api_mail_source_rejects_malformed_budget_before_api_calls(budget):
    fake_service = _FakeGmailService({"messages": [{"id": "1"}]}, {})
    source = GmailApiMailSource(build_service=lambda: fake_service)

    with pytest.raises((TypeError, ValueError), match="max_messages"):
        source.fetch(since_days=7, max_messages=budget)

    assert fake_service.messages_obj.list_calls == []
    assert fake_service.messages_obj.get_calls == []


def test_gmail_api_mail_source_rejects_budget_above_1000_before_api_calls():
    fake_service = _FakeGmailService({"messages": [{"id": "1"}]}, {})

    with pytest.raises(ValueError, match="1000"):
        GmailApiMailSource(build_service=lambda: fake_service).fetch(
            since_days=7, max_messages=1001
        )

    assert fake_service.messages_obj.list_calls == []


def test_gmail_outcome_caps_same_mailbox_that_overflows_auth_snapshot():
    first_page = [{"id": str(index), "threadId": f"t-{index}"} for index in range(500)]
    second_page = [
        {"id": str(index), "threadId": f"t-{index}"}
        for index in range(500, 700)
    ]
    pages = [
        {"messages": first_page, "nextPageToken": "page-2"},
        {"messages": second_page, "nextPageToken": "page-3"},
    ]
    outcome_service = _FakeGmailService(
        pages,
        lambda message_id: {
            "id": message_id,
            "threadId": f"t-{message_id}",
            "payload": _gmail_payload_with_text_plain(f"Body {message_id}"),
        },
    )

    result = GmailApiMailSource(build_service=lambda: outcome_service).fetch(
        since_days=7,
        max_messages=650,
    )

    assert len(result) == 650
    assert [message.id for message in result[-2:]] == ["648", "649"]
    assert outcome_service.messages_obj.list_calls == [
        ("me", "newer_than:7d", 500, None),
        ("me", "newer_than:7d", 150, "page-2"),
    ]

    auth_service = _FakeGmailService(pages, {})
    with pytest.raises(mail_source.MailSourceOverflowError):
        GmailApiAuthMailSource(build_service=lambda: auth_service).fetch(
            since_days=7,
            max_messages=650,
        )
    assert auth_service.messages_obj.list_calls == [
        ("me", "newer_than:7d", 500, None),
        ("me", "newer_than:7d", 151, "page-2"),
    ]
    assert auth_service.messages_obj.metadata_calls == []
