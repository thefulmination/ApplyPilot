"""Tests for the permanent IMAP mail source (no network -- fake imaplib object)."""

import base64
import imaplib
import json

import pytest

from applypilot import config
from applypilot import mail_source
from applypilot.mail_source import ImapMailSource, MailSourceError, _normalize


# ---------------------------------------------------------------------------
# Fixtures: raw RFC822 message builders
# ---------------------------------------------------------------------------

def _multipart_alternative_raw() -> bytes:
    return (
        b"From: Alice <alice@example.com>\r\n"
        b"Subject: Hello there\r\n"
        b"Date: Wed, 01 Jul 2026 10:00:00 -0000\r\n"
        b"Message-ID: <abc123@example.com>\r\n"
        b'Content-Type: multipart/alternative; boundary="BOUNDARY"\r\n'
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Plain text body.\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><p>HTML body.</p></body></html>\r\n"
        b"--BOUNDARY--\r\n"
    )


def _encoded_word_subject_raw() -> bytes:
    return (
        b"From: Bob <bob@example.com>\r\n"
        b"Subject: =?UTF-8?B?SGVsbG8gd2l0aCBhY2NlbnRzOiBjYWbDqQ==?=\r\n"
        b"Date: Wed, 01 Jul 2026 11:00:00 -0000\r\n"
        b"Message-ID: <def456@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


def _base64_plain_raw() -> bytes:
    encoded_body = base64.b64encode(b"This is a base64-encoded plain body.\r\n").decode()
    return (
        b"From: Carol <carol@example.com>\r\n"
        b"Subject: Base64 body test\r\n"
        b"Date: Wed, 01 Jul 2026 12:00:00 -0000\r\n"
        b"Message-ID: <ghi789@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + encoded_body.encode() + b"\r\n"
    )


def _html_only_raw() -> bytes:
    return (
        b"From: Dave <dave@example.com>\r\n"
        b"Subject: HTML only\r\n"
        b"Date: Wed, 01 Jul 2026 13:00:00 -0000\r\n"
        b"Message-ID: <jkl012@example.com>\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><h1>Title</h1><p>Some <b>bold</b> text.</p></body></html>\r\n"
    )


# ---------------------------------------------------------------------------
# _normalize tests
# ---------------------------------------------------------------------------

def test_normalize_multipart_alternative_prefers_plain_body():
    msg = _normalize("1", _multipart_alternative_raw())
    assert msg.id == "1"
    assert msg.thread_id == "<abc123@example.com>"
    assert msg.subject == "Hello there"
    assert "Alice" in msg.sender
    assert msg.date == "Wed, 01 Jul 2026 10:00:00 -0000"
    assert msg.body.strip() == "Plain text body."
    assert "<html>" not in msg.body


def test_normalize_decodes_encoded_word_subject():
    msg = _normalize("2", _encoded_word_subject_raw())
    assert msg.subject == "Hello with accents: café"
    assert msg.thread_id == "<def456@example.com>"


def test_normalize_decodes_base64_plain_body():
    msg = _normalize("3", _base64_plain_raw())
    assert msg.body.strip() == "This is a base64-encoded plain body."


def test_normalize_html_only_strips_tags():
    msg = _normalize("4", _html_only_raw())
    assert "<" not in msg.body
    assert "Title" in msg.body
    assert "bold" in msg.body


def test_normalize_falls_back_to_uid_when_no_message_id():
    raw = (
        b"From: NoId <noid@example.com>\r\n"
        b"Subject: No message id\r\n"
        b"Date: Wed, 01 Jul 2026 14:00:00 -0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body.\r\n"
    )
    msg = _normalize("42", raw)
    assert msg.thread_id == "42"


# ---------------------------------------------------------------------------
# Fake imaplib object
# ---------------------------------------------------------------------------

class FakeImap:
    def __init__(
        self,
        search_ids: list[bytes],
        messages_by_id: dict[bytes, bytes],
        login_error=None,
        gmraw_ids: list[bytes] | None = None,
    ):
        self._search_ids = search_ids
        self._gmraw_ids = gmraw_ids if gmraw_ids is not None else search_ids
        self._messages_by_id = messages_by_id
        self._login_error = login_error
        self.logged_out = False
        self.login_calls = []
        self.selected = None
        self.uid_calls = []
        self.fetch_calls = []

    def login(self, email_addr, app_password):
        self.login_calls.append((email_addr, app_password))
        if self._login_error:
            raise self._login_error

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"1"]

    def search(self, charset, criterion, date):
        return "OK", [b" ".join(self._search_ids)]

    def fetch(self, uid, parts):
        self.fetch_calls.append((uid, parts))
        key = uid.encode() if isinstance(uid, str) else uid
        raw = self._messages_by_id.get(key)
        if raw is None:
            return "NO", [None]
        if "RFC822.SIZE" in parts:
            return "OK", [(b"1 (RFC822.SIZE %d)" % len(raw), b"")]
        match = __import__("re").search(r"BODY\.PEEK\[\]<0\.(\d+)>", parts)
        if match is None:
            raise AssertionError(f"unbounded body fetch: {parts}")
        count = int(match.group(1))
        return "OK", [(b"1 (BODY[])", raw[:count]), b")"]

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        if command.upper() == "SEARCH":
            return "OK", [b" ".join(self._gmraw_ids)]
        if command.upper() == "FETCH":
            uid = args[0]
            parts = args[1]
            key = uid.encode() if isinstance(uid, str) else uid
            raw = self._messages_by_id.get(key)
            if raw is None:
                return "NO", [None]
            if "RFC822.SIZE" in parts:
                return "OK", [(b"1 (RFC822.SIZE %d)" % len(raw), b"")]
            match = __import__("re").search(r"BODY\.PEEK\[\]<0\.(\d+)>", parts)
            if match is None:
                raise AssertionError(f"unbounded body fetch: {parts}")
            count = int(match.group(1))
            return "OK", [(b"1 (BODY[])", raw[:count]), b")"]
        return "NO", [b"unsupported uid command"]

    def logout(self):
        self.logged_out = True


class UnderstatedSizeImap(FakeImap):
    def fetch(self, uid, parts):
        self.fetch_calls.append((uid, parts))
        key = uid.encode() if isinstance(uid, str) else uid
        raw = self._messages_by_id.get(key)
        if raw is None:
            return "NO", [None]
        if "RFC822.SIZE" in parts:
            return "OK", [(b"1 (RFC822.SIZE 1)", b"")]
        match = __import__("re").search(r"BODY\.PEEK\[\]<0\.(\d+)>", parts)
        if match is None:
            raise AssertionError(f"unbounded body fetch: {parts}")
        count = int(match.group(1))
        return "OK", [(b"1 (BODY[])", raw[:count]), b")"]


# ---------------------------------------------------------------------------
# ImapMailSource.fetch tests
# ---------------------------------------------------------------------------

def test_fetch_returns_complete_bounded_message_snapshot():
    ids = [b"1", b"2", b"3", b"4"]
    messages_by_id = {
        b"1": _encoded_word_subject_raw(),
        b"2": _base64_plain_raw(),
        b"3": _html_only_raw(),
        b"4": _multipart_alternative_raw(),
    }
    fake = FakeImap(search_ids=ids, messages_by_id=messages_by_id)
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    result = source.fetch(since_days=7, max_messages=4)

    assert len(result) == 4
    assert [m.id for m in result] == ["1", "2", "3", "4"]
    assert fake.selected == ("INBOX", True)
    assert fake.logged_out is True


def test_fetch_respects_max_messages_cap_when_fewer_available():
    ids = [b"1", b"2"]
    messages_by_id = {
        b"1": _encoded_word_subject_raw(),
        b"2": _base64_plain_raw(),
    }
    fake = FakeImap(search_ids=ids, messages_by_id=messages_by_id)
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    result = source.fetch(since_days=7, max_messages=10)

    assert len(result) == 2


def test_imap_candidate_overflow_fails_before_any_message_fetch():
    ids = [str(index).encode() for index in range(1001)]
    fake = FakeImap(
        search_ids=ids,
        messages_by_id={message_id: b"Subject: Verify\r\n\r\n123456" for message_id in ids},
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        ImapMailSource("me@example.com", "password", imap=fake).fetch(
            since_days=7,
            max_messages=1000,
        )

    assert fake.fetch_calls == []


def test_imap_exact_candidate_bound_remains_bounded():
    ids = [str(index).encode() for index in range(1000)]
    fake = FakeImap(
        search_ids=ids,
        messages_by_id={message_id: b"" for message_id in ids},
    )

    result = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=1,
        max_scan_bytes=1000,
    ).fetch(since_days=7, max_messages=1000)

    assert len(result) == 1000
    assert len(fake.fetch_calls) == 2000
    assert not any(call[1] == "(RFC822)" for call in fake.fetch_calls)


def test_imap_body_fetch_uses_one_byte_sentinel_partial_range():
    raw = b"Subject: Verify\r\n\r\n123456"

    class PartialOnlyImap(FakeImap):
        def fetch(self, uid, parts):
            self.fetch_calls.append((uid, parts))
            if parts == "(RFC822.SIZE)":
                return "OK", [(b"1 (RFC822.SIZE 1)", b"")]
            if "RFC822" in parts:
                raise AssertionError("unbounded RFC822 fetch issued")
            match = __import__("re").search(r"BODY\.PEEK\[\]<0\.(\d+)>", parts)
            assert match is not None
            count = int(match.group(1))
            return "OK", [(b"1 (BODY[])", raw[:count]), b")"]

    fake = PartialOnlyImap(search_ids=[b"1"], messages_by_id={b"1": raw})
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw),
        max_scan_bytes=len(raw),
    )

    result = source.fetch(since_days=7, max_messages=1)

    assert [message.id for message in result] == ["1"]
    assert fake.fetch_calls[-1] == (
        "1",
        f"(BODY.PEEK[]<0.{len(raw) + 1}>)",
    )


def test_imap_oversize_message_fails_snapshot_without_body_fetch():
    raw = _base64_plain_raw()
    fake = FakeImap(search_ids=[b"1"], messages_by_id={b"1": raw})
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw) - 1,
        max_scan_bytes=len(raw) * 2,
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        source.fetch(since_days=7, max_messages=1)
    assert fake.fetch_calls == [("1", "(RFC822.SIZE)")]


def test_imap_accepts_exact_message_byte_boundary():
    raw = _base64_plain_raw()
    fake = FakeImap(search_ids=[b"1"], messages_by_id={b"1": raw})
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw),
        max_scan_bytes=len(raw),
    )

    result = source.fetch(since_days=7, max_messages=1)

    assert [message.id for message in result] == ["1"]
    assert fake.fetch_calls == [
        ("1", "(RFC822.SIZE)"),
        ("1", f"(BODY.PEEK[]<0.{len(raw) + 1}>)"),
    ]


def test_imap_aggregate_cap_stops_further_full_fetches():
    raw = _base64_plain_raw()
    fake = FakeImap(
        search_ids=[b"1", b"2", b"3"],
        messages_by_id={b"1": raw, b"2": raw, b"3": raw},
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw),
        max_scan_bytes=len(raw) * 2,
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        source.fetch(since_days=7, max_messages=3)

    assert not any(
        uid == "3" and "BODY.PEEK" in parts
        for uid, parts in fake.fetch_calls
    )


def test_imap_first_candidate_exhausting_budget_with_remaining_fails_closed():
    raw = _base64_plain_raw()
    fake = FakeImap(
        search_ids=[b"1", b"2"],
        messages_by_id={b"1": raw, b"2": raw},
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw),
        max_scan_bytes=len(raw),
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        source.fetch(since_days=7, max_messages=2)

    assert [call for call in fake.fetch_calls if "BODY.PEEK" in call[1]] == [
        ("1", f"(BODY.PEEK[]<0.{len(raw) + 1}>)")
    ]


def test_imap_understated_oversize_sequence_charges_aggregate_budget():
    raw = b"A" * 100
    fake = UnderstatedSizeImap(
        search_ids=[b"1", b"2", b"3", b"4"],
        messages_by_id={
            b"1": raw,
            b"2": raw,
            b"3": raw,
            b"4": raw,
        },
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=50,
        max_scan_bytes=50,
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        source.fetch(since_days=7, max_messages=4)
    body_calls = [call for call in fake.fetch_calls if "BODY.PEEK" in call[1]]
    assert body_calls == [("1", "(BODY.PEEK[]<0.51>)")]


def test_imap_exact_aggregate_boundary_with_remaining_candidate_fails_closed():
    fake = UnderstatedSizeImap(
        search_ids=[b"1", b"2", b"3"],
        messages_by_id={
            b"1": b"A" * 30,
            b"2": b"B" * 20,
            b"3": b"C" * 10,
        },
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=30,
        max_scan_bytes=50,
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        source.fetch(since_days=7, max_messages=3)

    assert [call for call in fake.fetch_calls if "BODY.PEEK" in call[1]] == [
        ("1", "(BODY.PEEK[]<0.31>)"),
        ("2", "(BODY.PEEK[]<0.21>)"),
    ]


def test_imap_malformed_full_fetch_fails_closed_before_later_downloads():
    class MalformedFullImap(UnderstatedSizeImap):
        def fetch(self, uid, parts):
            if "BODY.PEEK" in parts and uid == "1":
                self.fetch_calls.append((uid, parts))
                return "OK", [(b"1 (BODY[])", "not-bytes"), b")"]
            return super().fetch(uid, parts)

    fake = MalformedFullImap(
        search_ids=[b"1", b"2"],
        messages_by_id={b"1": b"A" * 100, b"2": b"B" * 20},
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=100,
        max_scan_bytes=200,
    )

    with pytest.raises(mail_source.MailSourceOverflowError, match="malformed"):
        source.fetch(since_days=7, max_messages=2)

    assert [call for call in fake.fetch_calls if "BODY.PEEK" in call[1]] == [
        ("1", "(BODY.PEEK[]<0.101>)")
    ]


def test_imap_understated_normal_messages_remain_unchanged():
    raw = _base64_plain_raw()
    fake = UnderstatedSizeImap(
        search_ids=[b"1", b"2"],
        messages_by_id={b"1": raw, b"2": raw},
    )
    source = ImapMailSource(
        "me@example.com",
        "password",
        imap=fake,
        max_message_bytes=len(raw),
        max_scan_bytes=len(raw) * 2,
    )

    result = source.fetch(since_days=7, max_messages=2)

    assert [message.id for message in result] == ["1", "2"]


def test_imap_malformed_size_metadata_fails_closed_without_full_fetch():
    class MalformedSizeImap(FakeImap):
        def fetch(self, uid, parts):
            self.fetch_calls.append((uid, parts))
            if "RFC822.SIZE" in parts:
                return "OK", [(b"1 (RFC822.SIZE nope)", b"")]
            raise AssertionError("full fetch must not occur")

    fake = MalformedSizeImap(
        search_ids=[b"1"], messages_by_id={b"1": _base64_plain_raw()}
    )

    with pytest.raises(mail_source.MailSourceOverflowError):
        ImapMailSource("me@example.com", "password", imap=fake).fetch(
            since_days=7, max_messages=1
        )


def test_fetch_nonpositive_budget_does_no_imap_work():
    fake = FakeImap(search_ids=[b"1"], messages_by_id={b"1": _base64_plain_raw()})
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    assert source.fetch(since_days=7, max_messages=0) == []
    assert source.fetch(since_days=7, max_messages=-1) == []
    assert fake.login_calls == []
    assert fake.selected is None
    assert fake.uid_calls == []
    assert fake.logged_out is False


@pytest.mark.parametrize("budget", [True, 1.5, float("nan"), float("inf"), "1.5", "nan", ""])
def test_imap_rejects_malformed_budget_before_backend_work(budget):
    fake = FakeImap(search_ids=[b"1"], messages_by_id={b"1": _base64_plain_raw()})

    with pytest.raises((TypeError, ValueError), match="max_messages"):
        ImapMailSource("me@example.com", "password", imap=fake).fetch(
            since_days=7, max_messages=budget
        )

    assert fake.login_calls == []


def test_imap_rejects_budget_above_1000_before_backend_work():
    fake = FakeImap(search_ids=[], messages_by_id={})

    with pytest.raises(ValueError, match="1000"):
        ImapMailSource("me@example.com", "password", imap=fake).fetch(
            since_days=7, max_messages=1001
        )

    assert fake.login_calls == []


def test_fetch_uses_gmail_raw_uid_search_when_query_provided():
    ids = [b"1", b"2", b"3", b"4"]
    gmraw_ids = [b"2", b"4"]
    messages_by_id = {
        b"1": _encoded_word_subject_raw(),
        b"2": _base64_plain_raw(),
        b"3": _html_only_raw(),
        b"4": _multipart_alternative_raw(),
    }
    fake = FakeImap(search_ids=ids, gmraw_ids=gmraw_ids, messages_by_id=messages_by_id)
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    result = source.fetch(
        since_days=7,
        max_messages=10,
        gmail_raw_query='verification OR "magic link"',
    )

    assert [m.id for m in result] == ["2", "4"]
    assert fake.uid_calls[0] == (
        "SEARCH",
        "X-GM-RAW",
        '"newer_than:7d (verification OR \\"magic link\\")"',
    )


def test_fetch_strips_spaces_from_app_password_before_login():
    fake = FakeImap(search_ids=[], messages_by_id={})
    source = ImapMailSource("me@example.com", "abcd efgh ijkl mnop", imap=fake)

    source.fetch(since_days=7, max_messages=5)

    assert fake.login_calls == [("me@example.com", "abcdefghijklmnop")]


def test_fetch_login_failure_raises_mail_source_error():
    fake = FakeImap(
        search_ids=[],
        messages_by_id={},
        login_error=imaplib.IMAP4.error("invalid credentials"),
    )
    source = ImapMailSource("me@example.com", "bad password", imap=fake)

    with pytest.raises(MailSourceError):
        source.fetch(since_days=7, max_messages=5)

    # even on failure, logout must still be attempted (finally block)
    assert fake.logged_out is True


def test_fetch_select_failure_raises_mail_source_error():
    class SelectFailsImap(FakeImap):
        def select(self, mailbox, readonly=False):
            self.selected = (mailbox, readonly)
            return "NO", [b"imap disabled"]

    fake = SelectFailsImap(search_ids=[], messages_by_id={})
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    with pytest.raises(MailSourceError, match="select"):
        source.fetch(since_days=7, max_messages=5)

    assert fake.logged_out is True


def test_fetch_search_failure_raises_mail_source_error():
    class SearchFailsImap(FakeImap):
        def search(self, charset, criterion, date):
            return "NO", [b"search failed"]

    fake = SearchFailsImap(search_ids=[], messages_by_id={})
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    with pytest.raises(MailSourceError, match="search"):
        source.fetch(since_days=7, max_messages=5)

    assert fake.logged_out is True


def test_fetch_message_failure_raises_mail_source_error():
    fake = FakeImap(search_ids=[b"1"], messages_by_id={})
    source = ImapMailSource("me@example.com", "app password", imap=fake)

    with pytest.raises(MailSourceError, match="fetch"):
        source.fetch(since_days=7, max_messages=5)

    assert fake.logged_out is True


# ---------------------------------------------------------------------------
# config.load_gmail_app_password tests
# ---------------------------------------------------------------------------

def test_load_gmail_app_password_from_env(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_GMAIL_ADDRESS", "env@example.com")
    monkeypatch.setenv("APPLYPILOT_GMAIL_APP_PASSWORD", "envpassword")

    result = config.load_gmail_app_password()

    assert result == ("env@example.com", "envpassword")


def test_load_gmail_app_password_from_json_file(monkeypatch, tmp_path):
    monkeypatch.delenv("APPLYPILOT_GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("APPLYPILOT_GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)

    creds_path = tmp_path / "gmail_app_password.json"
    creds_path.write_text(
        json.dumps({"email": "file@example.com", "app_password": "filepassword"}),
        encoding="utf-8",
    )

    result = config.load_gmail_app_password()

    assert result == ("file@example.com", "filepassword")


def test_load_gmail_app_password_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("APPLYPILOT_GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("APPLYPILOT_GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)

    result = config.load_gmail_app_password()

    assert result is None
