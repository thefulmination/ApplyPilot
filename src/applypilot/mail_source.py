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
DEFAULT_MAX_MAIL_MESSAGE_BYTES = 1024 * 1024
DEFAULT_MAX_MAIL_SCAN_BYTES = 8 * 1024 * 1024
MAX_CONFIGURED_MAIL_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_CONFIGURED_MAIL_SCAN_BYTES = 32 * 1024 * 1024


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


def _validate_byte_limit(value, *, name: str, cap: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0 or value > cap:
        raise ValueError(f"{name} must be between 1 and {cap}")
    return value


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


class MailSourceOverflowError(MailSourceError):
    """Raised when a bounded candidate snapshot is known to be incomplete."""


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

    def __init__(
        self,
        email_addr: str,
        app_password: str,
        *,
        imap=None,
        max_message_bytes: int = DEFAULT_MAX_MAIL_MESSAGE_BYTES,
        max_scan_bytes: int = DEFAULT_MAX_MAIL_SCAN_BYTES,
    ):
        self._email_addr = email_addr
        self._app_password = (app_password or "").replace(" ", "")
        self._injected_imap = imap
        self._max_message_bytes = _validate_byte_limit(
            max_message_bytes,
            name="max_message_bytes",
            cap=MAX_CONFIGURED_MAIL_MESSAGE_BYTES,
        )
        self._max_scan_bytes = _validate_byte_limit(
            max_scan_bytes,
            name="max_scan_bytes",
            cap=MAX_CONFIGURED_MAIL_SCAN_BYTES,
        )

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

            if len(ids) > budget:
                raise MailSourceOverflowError(
                    f"mail candidate snapshot exceeds max_messages={budget}"
                )
            newest_ids = ids

            messages: list[MailMessage] = []
            scanned_bytes = 0
            for index, msg_id in enumerate(newest_ids):
                uid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                if scanned_bytes >= self._max_scan_bytes:
                    raise MailSourceOverflowError(
                        "mail scan byte budget left candidates unread"
                    )
                if gmail_raw_query:
                    status, size_data = imap.uid(
                        "FETCH", uid, "(RFC822.SIZE)"
                    )
                    _ensure_ok(status, f"uid size fetch for message {uid}", size_data)
                else:
                    status, size_data = imap.fetch(uid, "(RFC822.SIZE)")
                    _ensure_ok(status, f"size fetch for message {uid}", size_data)
                message_size = _extract_imap_size(size_data)
                if message_size is None or message_size > self._max_message_bytes:
                    raise MailSourceOverflowError(
                        f"message {uid} could not be included in bounded snapshot"
                    )
                if scanned_bytes + message_size > self._max_scan_bytes:
                    raise MailSourceOverflowError(
                        "mail scan byte budget cannot include complete snapshot"
                    )

                remaining_scan_bytes = self._max_scan_bytes - scanned_bytes
                retrieval_limit = min(
                    self._max_message_bytes,
                    remaining_scan_bytes,
                )
                partial_query = f"(BODY.PEEK[]<0.{retrieval_limit + 1}>)"
                if gmail_raw_query:
                    status, fetch_data = imap.uid("FETCH", uid, partial_query)
                    _ensure_ok(status, f"uid fetch for message {uid}", fetch_data)
                else:
                    status, fetch_data = imap.fetch(uid, partial_query)
                    _ensure_ok(status, f"fetch for message {uid}", fetch_data)
                raw = _extract_raw_bytes(fetch_data)
                if raw is None:
                    scanned_bytes = self._max_scan_bytes
                    raise MailSourceOverflowError(
                        f"message {uid} returned malformed bounded payload"
                    )
                actual_size = len(raw)
                scanned_bytes += actual_size
                if actual_size > remaining_scan_bytes:
                    raise MailSourceOverflowError(
                        "mail scan byte budget cannot include complete snapshot"
                    )
                if actual_size > self._max_message_bytes:
                    raise MailSourceOverflowError(
                        f"message {uid} exceeds bounded message size"
                    )
                messages.append(_normalize(uid, raw))
                if (
                    scanned_bytes >= self._max_scan_bytes
                    and index + 1 < len(newest_ids)
                ):
                    raise MailSourceOverflowError(
                        "mail scan byte budget left candidates unread"
                    )

            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass


def _extract_raw_bytes(fetch_data) -> bytes | None:
    """Pull raw message bytes out of an imaplib fetch response.

    Both RFC822.SIZE and bounded BODY.PEEK responses use tuple payloads.
    """
    if not fetch_data:
        return None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2:
            candidate = item[1]
            if isinstance(candidate, bytes):
                return candidate
    return None


def _extract_imap_size(fetch_data) -> int | None:
    for item in fetch_data or ():
        values = item if isinstance(item, tuple) else (item,)
        for value in values:
            if not isinstance(value, bytes):
                continue
            match = re.search(rb"RFC822\.SIZE\s+(\d+)", value)
            if match:
                return int(match.group(1))
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


def _gmail_actual_payload_bytes(message: dict[str, Any], *, limit: int) -> int | None:
    """Conservatively count a Gmail full response without decoding body data."""
    if not isinstance(message, dict) or limit < 0:
        return None
    snippet = message.get("snippet", "")
    payload = message.get("payload")
    if not isinstance(snippet, str) or not isinstance(payload, dict):
        return None
    total = len(snippet.encode("utf-8"))
    stack = [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            return None
        for field in ("mimeType", "filename", "partId"):
            value = part.get(field, "")
            if not isinstance(value, str):
                return None
            total += len(value.encode("utf-8"))

        headers = part.get("headers", [])
        if not isinstance(headers, list):
            return None
        for header in headers:
            if not isinstance(header, dict):
                return None
            name = header.get("name")
            value = header.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                return None
            total += len(name.encode("utf-8")) + len(value.encode("utf-8"))

        body = part.get("body", {})
        if not isinstance(body, dict):
            return None
        data = body.get("data", "")
        attachment_id = body.get("attachmentId", "")
        declared_size = body.get("size", 0)
        if (
            not isinstance(data, str)
            or not isinstance(attachment_id, str)
            or isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            return None
        encoded_size = len(data.encode("utf-8"))
        total += max(encoded_size, declared_size)
        total += len(attachment_id.encode("utf-8"))

        parts = part.get("parts", [])
        if not isinstance(parts, list):
            return None
        stack.extend(parts)
        if total > limit:
            return limit + 1
    return total


def _gmail_query(*, since_days: int, gmail_raw_query: str | None) -> str:
    query = f"newer_than:{since_days}d"
    if gmail_raw_query:
        query = f"{query} ({gmail_raw_query})"
    return query


def _gmail_candidate_refs(service, *, query: str, budget: int) -> list[dict[str, Any]]:
    """Fetch a complete bounded candidate snapshot or raise on MAX+1."""
    target = budget + 1
    refs: list[dict[str, Any]] = []
    page_token = None
    while len(refs) < target:
        page_size = min(500, target - len(refs))
        list_kwargs = {
            "userId": "me",
            "q": query,
            "maxResults": page_size,
        }
        if page_token is not None:
            list_kwargs["pageToken"] = page_token
        response = service.users().messages().list(**list_kwargs).execute()
        if not isinstance(response, dict):
            raise MailSourceError("Gmail candidate list returned malformed response")
        page_refs = response.get("messages", [])
        if not isinstance(page_refs, list) or any(
            not isinstance(ref, dict) or not isinstance(ref.get("id"), str)
            for ref in page_refs
        ):
            raise MailSourceError("Gmail candidate list returned malformed messages")
        remaining = target - len(refs)
        refs.extend(page_refs[:remaining])
        if len(page_refs) > remaining or len(refs) > budget:
            raise MailSourceOverflowError(
                f"mail candidate snapshot exceeds max_messages={budget}"
            )
        page_token = response.get("nextPageToken")
        if not page_token or not page_refs:
            break
    return refs


def _gmail_auth_metadata_bytes(message: dict[str, Any], *, limit: int) -> int | None:
    if not isinstance(message, dict) or limit < 0:
        return None
    total = 0
    for field in ("id", "threadId", "snippet"):
        value = message.get(field, "")
        if not isinstance(value, str):
            return None
        total += len(value.encode("utf-8"))
    payload = message.get("payload", {})
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        return None
    for header in headers:
        if not isinstance(header, dict):
            return None
        name = header.get("name")
        value = header.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total > limit:
            return limit + 1
    return total


class GmailApiMailSource:
    """Backward-compat Gmail read access via the OAuth-backed Gmail API.

    Fallback path used when no IMAP app password is configured (see
    get_mail_source() below). The OAuth token this depends on expires every
    7 days for unverified apps -- ImapMailSource is the permanent successor.
    """

    def __init__(
        self,
        build_service=None,
        *,
        max_message_bytes: int = DEFAULT_MAX_MAIL_MESSAGE_BYTES,
        max_scan_bytes: int = DEFAULT_MAX_MAIL_SCAN_BYTES,
    ):
        if build_service is None:
            from applypilot.gmail_outcomes import build_gmail_service

            build_service = build_gmail_service
        self._build_service = build_service
        self._max_message_bytes = _validate_byte_limit(
            max_message_bytes,
            name="max_message_bytes",
            cap=MAX_CONFIGURED_MAIL_MESSAGE_BYTES,
        )
        self._max_scan_bytes = _validate_byte_limit(
            max_scan_bytes,
            name="max_scan_bytes",
            cap=MAX_CONFIGURED_MAIL_SCAN_BYTES,
        )

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
        query = _gmail_query(
            since_days=since_days,
            gmail_raw_query=gmail_raw_query,
        )
        refs = _gmail_candidate_refs(service, query=query, budget=budget)

        messages: list[MailMessage] = []
        scanned_bytes = 0
        for ref in refs:
            if scanned_bytes >= self._max_scan_bytes:
                break
            metadata = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    fields="id,threadId,sizeEstimate",
                )
                .execute()
            )
            message_size = metadata.get("sizeEstimate")
            if (
                isinstance(message_size, bool)
                or not isinstance(message_size, int)
                or message_size < 0
                or message_size > self._max_message_bytes
            ):
                continue
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            remaining_scan_bytes = self._max_scan_bytes - scanned_bytes
            actual_size = _gmail_actual_payload_bytes(
                msg, limit=remaining_scan_bytes
            )
            if actual_size is None:
                scanned_bytes = self._max_scan_bytes
                break
            scanned_bytes += actual_size
            if actual_size > remaining_scan_bytes:
                break
            if actual_size > self._max_message_bytes:
                continue
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


class GmailApiAuthMailSource(GmailApiMailSource):
    """Gmail auth reader restricted to provider-bounded metadata and snippet."""

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

        service = self._build_service()
        query = _gmail_query(
            since_days=since_days,
            gmail_raw_query=gmail_raw_query,
        )
        refs = _gmail_candidate_refs(service, query=query, budget=budget)
        messages: list[MailMessage] = []
        scanned_bytes = 0
        for index, ref in enumerate(refs):
            if scanned_bytes >= self._max_scan_bytes:
                raise MailSourceOverflowError(
                    "mail scan byte budget left candidates unread"
                )
            message = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date", "Message-ID"],
                    fields=(
                        "id,threadId,sizeEstimate,snippet,"
                        "payload(headers(name,value))"
                    ),
                )
                .execute()
            )
            remaining_scan_bytes = self._max_scan_bytes - scanned_bytes
            actual_size = _gmail_auth_metadata_bytes(
                message,
                limit=remaining_scan_bytes,
            )
            if actual_size is None:
                scanned_bytes = self._max_scan_bytes
                raise MailSourceOverflowError(
                    f"message {ref['id']} returned malformed bounded metadata"
                )
            scanned_bytes += actual_size
            if actual_size > remaining_scan_bytes:
                raise MailSourceOverflowError(
                    "mail scan byte budget cannot include complete snapshot"
                )
            if actual_size > self._max_message_bytes:
                raise MailSourceOverflowError(
                    f"message {ref['id']} exceeds bounded metadata size"
                )
            size_estimate = message.get("sizeEstimate")
            if (
                isinstance(size_estimate, bool)
                or not isinstance(size_estimate, int)
                or size_estimate < 0
                or size_estimate > self._max_message_bytes
            ):
                raise MailSourceOverflowError(
                    f"message {ref['id']} has unsafe size metadata"
                )
            payload = message.get("payload", {})
            headers = _gmail_headers(payload)
            messages.append(
                MailMessage(
                    id=ref["id"],
                    thread_id=message.get("threadId") or ref.get("threadId", ref["id"]),
                    subject=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    date=headers.get("date", ""),
                    body=message.get("snippet", ""),
                )
            )
            if scanned_bytes >= self._max_scan_bytes and index + 1 < len(refs):
                raise MailSourceOverflowError(
                    "mail scan byte budget left candidates unread"
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


def get_auth_mail_source() -> MailSource:
    """Return an auth-safe source; Gmail API fallback never retrieves bodies."""
    source = get_mail_source()
    if isinstance(source, GmailApiMailSource) and not isinstance(
        source,
        GmailApiAuthMailSource,
    ):
        return GmailApiAuthMailSource(
            build_service=source._build_service,
            max_message_bytes=source._max_message_bytes,
            max_scan_bytes=source._max_scan_bytes,
        )
    return source
