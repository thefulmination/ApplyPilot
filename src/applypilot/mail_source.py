"""Permanent (non-expiring) Gmail read access via IMAP + an app password.

Replaces the 7-day OAuth token that previously backed the fleet's OTP/2FA
relay and outcome scan. Uses only the Python standard library (imaplib +
email) -- no new pip dependency.

The mailbox is ALWAYS selected read-only: this module never mutates the
inbox (no flags set, nothing deleted, nothing marked read).
"""

from __future__ import annotations

import datetime
import email
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from typing import Any, Protocol

MAX_MAIL_MESSAGES = 1000


def validate_max_messages(
    value: int | str, *, cap: int | None = MAX_MAIL_MESSAGES
) -> int:
    """Accept only integral integer/string budgets within the backend safety cap."""
    if isinstance(value, bool):
        raise TypeError("max_messages must be an integer, not bool")
    if isinstance(value, int):
        budget = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        budget = int(value.strip())
    else:
        raise ValueError("max_messages must be an integer or integer string")
    if cap is not None and budget > cap:
        raise ValueError(f"max_messages must be <= {cap}")
    return budget


@dataclass
class MailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str
    date: str
    body: str


class MailSourceError(Exception):
    """Raised when the IMAP connection/login/fetch fails."""


_TAG_RE = re.compile(r"<[^>]+>")


def _status_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    return str(value)


def _imap_detail(data, fallback) -> str:
    if data:
        first = data[0]
        if isinstance(first, bytes):
            return first.decode(errors="replace")
        return str(first)
    return _status_text(fallback)


def _ensure_ok(status, operation: str, data=None) -> None:
    if _status_text(status).upper() != "OK":
        raise MailSourceError(f"IMAP {operation} failed: {_imap_detail(data, status)}")


def _decode_header_value(raw_value: str | None) -> str:
    """Decode an RFC 2047 encoded-word header (Subject/From) into plain text."""
    if not raw_value:
        return ""
    parts = decode_header(raw_value)
    decoded_chunks = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded_chunks.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_chunks.append(chunk)
    return "".join(decoded_chunks)


def _strip_html(html: str) -> str:
    """Very simple tag-stripper -- good enough to get readable text out of a
    text/html-only message body (OTP codes, outcome emails, etc.)."""
    return _TAG_RE.sub("", html).strip()


def _decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message) -> str:
    """Walk the message parts: first text/plain wins; else fall back to a
    tag-stripped text/html part."""
    plain_body = None
    html_body = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition:
                continue
            if content_type == "text/plain" and plain_body is None:
                plain_body = _decode_part_payload(part)
            elif content_type == "text/html" and html_body is None:
                html_body = _decode_part_payload(part)
    else:
        content_type = msg.get_content_type()
        if content_type == "text/plain":
            plain_body = _decode_part_payload(msg)
        elif content_type == "text/html":
            html_body = _decode_part_payload(msg)

    if plain_body is not None:
        return plain_body
    if html_body is not None:
        return _strip_html(html_body)
    return ""


def _normalize(uid: str, raw: bytes) -> MailMessage:
    """Parse a raw RFC822 message into a MailMessage."""
    msg = email.message_from_bytes(raw)

    subject = _decode_header_value(msg.get("Subject"))
    sender = _decode_header_value(msg.get("From"))
    date = msg.get("Date") or ""
    thread_id = msg.get("Message-ID") or uid
    body = _extract_body(msg)

    return MailMessage(
        id=uid,
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        date=date,
        body=body,
    )


class ImapMailSource:
    """Read-only IMAP mail reader for a Gmail account, authenticated with an
    app password (never expires, unlike the OAuth token it replaces)."""

    def __init__(self, email_addr: str, app_password: str, *, imap=None):
        self._email_addr = email_addr
        self._app_password = (app_password or "").replace(" ", "")
        self._injected_imap = imap

    def fetch(
        self,
        *,
        since_days: int,
        max_messages: int | str,
        gmail_raw_query: str | None = None,
    ) -> list[MailMessage]:
        budget = validate_max_messages(max_messages)
        if budget <= 0:
            return []

        imap = self._injected_imap or imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            try:
                imap.login(self._email_addr, self._app_password)
            except imaplib.IMAP4.error as exc:
                raise MailSourceError(
                    "IMAP login failed -- check the app password and that IMAP "
                    "is enabled in Gmail settings"
                ) from exc

            status, data = imap.select("INBOX", readonly=True)
            _ensure_ok(status, "select", data)

            if gmail_raw_query:
                status, data = imap.uid(
                    "SEARCH",
                    "X-GM-RAW",
                    _imap_quote_gmail_raw_query(
                        since_days=since_days,
                        gmail_raw_query=gmail_raw_query,
                    ),
                )
                _ensure_ok(status, "uid search", data)
                ids = _extract_search_ids(data)
            else:
                since_date = (
                    datetime.date.today() - datetime.timedelta(days=since_days)
                ).strftime("%d-%b-%Y")
                status, data = imap.search(None, "SINCE", since_date)
                _ensure_ok(status, "search", data)
                ids = _extract_search_ids(data)

            newest_ids = ids[-budget:]

            messages: list[MailMessage] = []
            for msg_id in newest_ids:
                uid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                if gmail_raw_query:
                    status, fetch_data = imap.uid("FETCH", uid, "(RFC822)")
                    _ensure_ok(status, f"uid fetch for message {uid}", fetch_data)
                else:
                    status, fetch_data = imap.fetch(uid, "(RFC822)")
                    _ensure_ok(status, f"fetch for message {uid}", fetch_data)
                raw = _extract_raw_bytes(fetch_data)
                if raw is None:
                    raise MailSourceError(f"IMAP fetch returned no RFC822 payload for message {uid}")
                messages.append(_normalize(uid, raw))

            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass


def _extract_raw_bytes(fetch_data) -> bytes | None:
    """Pull the raw RFC822 bytes out of an imaplib fetch() response.

    imaplib's fetch response is typically shaped like:
        [(b'1 (RFC822 {1234}', b'<raw message bytes>'), b')']
    """
    if not fetch_data:
        return None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2:
            candidate = item[1]
            if isinstance(candidate, bytes):
                return candidate
    return None


def _imap_quote_gmail_raw_query(*, since_days: int, gmail_raw_query: str) -> str:
    escaped = gmail_raw_query.replace("\\", "\\\\").replace('"', '\\"')
    return f'"newer_than:{since_days}d ({escaped})"'


def _extract_search_ids(search_data) -> list[bytes | str]:
    ids: list[bytes | str] = []
    if search_data and search_data[0]:
        raw_ids = search_data[0]
        if isinstance(raw_ids, bytes):
            ids = raw_ids.split()
        else:
            ids = raw_ids.split()
    return ids


class MailSource(Protocol):
    """Structural type for a mail source: anything with a matching .fetch()."""

    def fetch(
        self,
        *,
        since_days: int,
        max_messages: int | str,
        gmail_raw_query: str | None = None,
    ) -> list[MailMessage]:
        ...


def _gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in payload.get("headers", [])
    }


class GmailApiMailSource:
    """Backward-compat Gmail read access via the OAuth-backed Gmail API.

    Fallback path used when no IMAP app password is configured (see
    get_mail_source() below). The OAuth token this depends on expires every
    7 days for unverified apps -- ImapMailSource is the permanent successor.
    """

    def __init__(self, build_service=None):
        if build_service is None:
            from applypilot.gmail_outcomes import build_gmail_service

            build_service = build_gmail_service
        self._build_service = build_service

    def fetch(
        self,
        *,
        since_days: int,
        max_messages: int | str,
        gmail_raw_query: str | None = None,
    ) -> list[MailMessage]:
        budget = validate_max_messages(max_messages)
        if budget <= 0:
            return []

        from applypilot.gmail_outcomes import _get_text_body

        service = self._build_service()
        query = f"newer_than:{since_days}d"
        if gmail_raw_query:
            query = f"{query} ({gmail_raw_query})"

        refs = []
        page_token = None
        while len(refs) < budget:
            page_size = min(500, budget - len(refs))
            list_kwargs = {
                "userId": "me",
                "q": query,
                "maxResults": page_size,
            }
            if page_token is not None:
                list_kwargs["pageToken"] = page_token
            resp = service.users().messages().list(**list_kwargs).execute()
            page_refs = resp.get("messages", [])
            if not page_refs:
                break
            refs.extend(page_refs[: budget - len(refs)])
            if len(refs) >= budget:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        messages: list[MailMessage] = []
        for ref in refs:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            payload = msg.get("payload", {})
            headers = _gmail_headers(payload)
            messages.append(
                MailMessage(
                    id=ref["id"],
                    thread_id=ref.get("threadId", ref["id"]),
                    subject=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    date=headers.get("date", ""),
                    body=_get_text_body(payload),
                )
            )
        return messages


def get_mail_source() -> MailSource:
    """Pick the mail source backend: IMAP (permanent, app-password-backed) when
    configured, else the legacy OAuth-backed Gmail API path."""
    from applypilot import config

    creds = config.load_gmail_app_password()
    if creds:
        return ImapMailSource(creds[0], creds[1])
    return GmailApiMailSource()
