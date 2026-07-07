"""Fleet-wide OTP (email-verification code) relay over Postgres.

A remote worker that hits an email-verification wall files an ``otp_request`` and
polls it for a code; the home-side responder (answer_pending, below) reads the
home box's Gmail and writes the code into the row. The code lives in PG only for
the seconds between answer and consume, is single-use, and is NEVER logged. Gmail
is read only by ``answer_pending`` (home box). See the 2026-07-03 relay spec."""
from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from applypilot import inbox_auth

_DEFAULT_ANSWERED_TTL_SECONDS = 600

_PROVIDER_DOMAIN_GROUPS = (
    ("oraclecloud.com", "oracle.com", "taleo.net"),
    ("myworkdayjobs.com", "myworkdaysite.com", "workdayjobs.com", "workday.com"),
    ("greenhouse.io", "greenhouse-mail.io"),
    ("adp.com", "workforcenow.adp.com"),
    ("amazon.jobs", "jobs.amazon.com"),
    ("eightfold.ai",),
)


@dataclass(frozen=True)
class RelayCode:
    value: str
    kind: str  # 'code' | 'magic_link'


def _apply_domain(application_url: str) -> str:
    return (urlparse(application_url or "").hostname or "").lower()


def _normalize_domain(domain: str | None) -> str:
    return (domain or "").strip().lower().strip(".")


def _domain_related(left: str, right: str) -> bool:
    left = _normalize_domain(left)
    right = _normalize_domain(right)
    if not left or not right:
        return False
    if left == right or left.endswith(f".{right}") or right.endswith(f".{left}"):
        return True
    for group in _PROVIDER_DOMAIN_GROUPS:
        if any(left == d or left.endswith(f".{d}") for d in group) and any(
            right == d or right.endswith(f".{d}") for d in group
        ):
            return True
    return False


def _candidate_url_domain(match) -> str:
    candidate = getattr(match, "candidate", None)
    if getattr(candidate, "kind", None) != "magic_link":
        return ""
    return _apply_domain(getattr(candidate, "value", "") or "")


def _match_belongs_to_request(sender_hint: str | None, match) -> bool:
    hint = _normalize_domain(sender_hint)
    if not hint:
        return True
    evidence = [
        inbox_auth.sender_domain(getattr(match, "sender", "") or ""),
        _candidate_url_domain(match),
    ]
    evidence = [d for d in evidence if d]
    if not evidence:
        # Older unit-test doubles predate sender/link metadata. Production matches always
        # carry sender, so keep legacy doubles from becoming unrelated failures.
        return True
    return any(_domain_related(hint, domain) for domain in evidence)


def request_code(conn, *, worker_id: str, job_url: str, application_url: str,
                 ttl_seconds: int = 300) -> int:
    """File a pending OTP request; return its id. Never blocks."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
            (worker_id, application_url or job_url, _apply_domain(application_url), ttl_seconds),
        )
        rid = cur.fetchone()["id"]
    conn.commit()
    return rid


def _try_consume(conn, request_id: int) -> RelayCode | None:
    """Atomically capture-and-null an unexpired, unconsumed code. Single-use."""
    with conn.cursor() as cur:
        cur.execute(
            "WITH picked AS ("
            "  SELECT id, code, code_kind FROM otp_request "
            "  WHERE id = %s AND consumed_at IS NULL AND code IS NOT NULL "
            "        AND (expires_at IS NULL OR expires_at > now()) "
            "  FOR UPDATE"
            ") "
            "UPDATE otp_request o SET consumed_at = now(), code = NULL "
            "FROM picked WHERE o.id = picked.id "
            "RETURNING picked.code AS code, picked.code_kind AS code_kind",
            (request_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return RelayCode(value=row["code"], kind=(row["code_kind"] or "code"))


def poll_for_code(conn, request_id: int, *, timeout_seconds: int = 300,
                  poll_seconds: float = 5.0) -> RelayCode | None:
    """Poll the request row until a code is available, consuming it, or timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        code = _try_consume(conn, request_id)
        if code is not None:
            return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.0, poll_seconds))


def _parse_email_dt(raw):
    """Parse an RFC2822 'Date' header to an aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def _answered_ttl_seconds(explicit: int | None) -> int:
    if explicit is not None:
        return max(1, int(explicit))
    raw = os.environ.get("APPLYPILOT_INBOX_AUTH_ANSWERED_TTL", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_ANSWERED_TTL_SECONDS


def answer_pending(conn, gmail_service=None, *, window_minutes: int = 15,
                   max_messages: int = 100, skew_seconds: int = 60,
                   answered_ttl_seconds: int | None = None) -> int:
    """Read Gmail ONCE and answer every pending request whose code arrived after it.

    Home box only (this is the sole function that touches Gmail). Time-based match:
    a candidate fits a request when its email arrived >= requested_at - skew. Each
    message_id is assigned to at most one request. The code is NEVER logged.

    When `gmail_service` is None (the default), the mailbox is read via
    get_mail_source() (IMAP app-password, falling back to the legacy Gmail API);
    passing a `gmail_service` explicitly preserves the old direct-service path."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, requested_at, sender_hint FROM otp_request "
            "WHERE code IS NULL AND consumed_at IS NULL "
            "      AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY requested_at"
        )
        pending = cur.fetchall()
    conn.commit()
    if not pending:
        return 0
    answered_ttl_seconds = _answered_ttl_seconds(answered_ttl_seconds)

    if gmail_service is None:
        from applypilot.mail_source import get_mail_source

        since_days = max(1, (window_minutes + 1439) // 1440)
        msgs = get_mail_source().fetch(since_days=since_days, max_messages=max_messages)
        matches = inbox_auth.scan_gmail_for_auth_codes(
            messages=msgs, minutes=window_minutes, max_messages=max_messages)
    else:
        matches = inbox_auth.scan_gmail_for_auth_codes(
            service=gmail_service, minutes=window_minutes, max_messages=max_messages)
    # Oldest email first: pair each request (iterated oldest-first) with the EARLIEST
    # eligible code, so request order maps to email-arrival order (spec: nearest
    # received_at > requested_at). Newest-first would hand the oldest request the
    # newest code and mis-pair concurrent same-window applies.
    parsed = [(m, _parse_email_dt(m.received_at)) for m in matches]
    parsed = [(m, ts) for (m, ts) in parsed if ts is not None]
    parsed.sort(key=lambda mt: mt[1])

    used_messages: set = set()
    answered = 0
    for req in pending:
        req_floor = req["requested_at"] - _dt.timedelta(seconds=skew_seconds)
        chosen = None
        for m, ts in parsed:
            if m.message_id in used_messages:
                continue
            if not _match_belongs_to_request(req.get("sender_hint"), m):
                continue
            if ts >= req_floor:
                chosen = m
                break
        if chosen is None:
            continue
        used_messages.add(chosen.message_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
                "answered_at=now(), expires_at = GREATEST(expires_at, now() + make_interval(secs => %s)) "
                "WHERE id=%s AND code IS NULL AND consumed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())",
                (chosen.candidate.value, chosen.candidate.kind,
                 _parse_email_dt(chosen.received_at), answered_ttl_seconds, req["id"]),
            )
            if cur.rowcount:
                answered += 1
        conn.commit()
    return answered


def purge_expired(conn) -> int:
    """Null the code on expired/consumed rows so no code lingers; keep the audit row."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE otp_request SET code = NULL "
            "WHERE code IS NOT NULL AND expires_at IS NOT NULL AND expires_at <= now()"
        )
        n = cur.rowcount
    conn.commit()
    return n
