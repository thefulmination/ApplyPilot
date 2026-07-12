"""Fleet-wide OTP (email-verification code) relay over Postgres.

A remote worker that hits an email-verification wall files an ``otp_request`` and
polls it for a code; the home-side responder (answer_pending, below) reads the
home box's Gmail and writes the code into the row. The code lives in PG only for
the seconds between answer and consume, is single-use, and is NEVER logged. Gmail
is read only by ``answer_pending`` (home box). See the 2026-07-03 relay spec."""
from __future__ import annotations

import datetime as _dt
import hashlib
import itertools
import math
import os
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import psycopg

from applypilot import inbox_auth
from applypilot.auth_matching import unique_assignments as _global_unique_assignments
from applypilot.mail_source import validate_max_messages

_DEFAULT_ANSWERED_TTL_SECONDS = 600
_DEFAULT_SCAN_MAX_MESSAGES = 1000
_MAX_RESPONDER_ITEMS = 1000
_REQUEST_LOCK_TIMEOUT_SECONDS = 5.0
_REQUEST_LOCK_RETRY_SECONDS = 0.05

@dataclass(frozen=True)
class RelayCode:
    value: str
    kind: str  # 'code' | 'magic_link'


class OtpResponderOverloadError(RuntimeError):
    """The bounded responder could not inspect a complete request/mail snapshot."""


def _apply_domain(application_url: str) -> str:
    return (urlparse(application_url or "").hostname or "").lower()


def _match_belongs_to_request(sender_hint: str | None, match) -> bool:
    return inbox_auth.match_belongs_to_provider(match, sender_hint)


def _validate_ttl_seconds(value) -> int:
    if isinstance(value, bool):
        raise ValueError("ttl_seconds must be an integer from 1 to 86400")
    if isinstance(value, int):
        ttl_seconds = value
    elif isinstance(value, str):
        raw = value.strip()
        digits = raw[1:] if raw[:1] in ("+", "-") else raw
        if not digits.isdigit():
            raise ValueError("ttl_seconds must be an integer from 1 to 86400")
        ttl_seconds = int(raw)
    else:
        raise ValueError("ttl_seconds must be an integer from 1 to 86400")
    if not 1 <= ttl_seconds <= 86400:
        raise ValueError("ttl_seconds must be an integer from 1 to 86400")
    return ttl_seconds


def _advisory_lock_key(identity: str) -> int:
    digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _request_lock_key(worker_id: str, target: str) -> int:
    return _advisory_lock_key(f"applypilot:otp_request:{worker_id}:{target}")


def _responder_lock_key() -> int:
    return _advisory_lock_key("applypilot:otp_responder")


def _acquire_request_lock(
    conn,
    lock_key: int,
    *,
    timeout_seconds: float = _REQUEST_LOCK_TIMEOUT_SECONDS,
) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("request lock timeout must be from 0 to 60 seconds")
    if timeout_seconds < 0 or timeout_seconds > 60:
        raise ValueError("request lock timeout must be from 0 to 60 seconds")
    normalized_timeout = float(timeout_seconds)
    if not math.isfinite(normalized_timeout):
        raise ValueError("request lock timeout must be from 0 to 60 seconds")

    deadline = time.monotonic() + normalized_timeout
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                (lock_key,),
            )
            if cur.fetchone()["acquired"]:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            conn.rollback()
            raise TimeoutError("timed out acquiring OTP request lock")
        time.sleep(min(_REQUEST_LOCK_RETRY_SECONDS, remaining))


def request_code(conn, *, worker_id: str, job_url: str, application_url: str,
                 ttl_seconds: int = 300) -> int:
    """Return the active request row's id after serializing concurrent duplicates.

    Requests for the same worker and target serialize under a transaction advisory lock.
    """
    ttl_seconds = _validate_ttl_seconds(ttl_seconds)
    target = application_url or job_url
    _acquire_request_lock(conn, _request_lock_key(worker_id, target))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, code FROM otp_request "
            "WHERE worker_id=%s AND url=%s AND consumed_at IS NULL "
            "      AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY requested_at DESC, id DESC LIMIT 1",
            (worker_id, target),
        )
        active = cur.fetchone()
        if active:
            rid = active["id"]
            if active["code"] is None:
                cur.execute(
                    "UPDATE otp_request "
                    "SET expires_at=GREATEST(expires_at, now() + make_interval(secs => %s)) "
                    "WHERE id=%s AND code IS NULL",
                    (ttl_seconds, rid),
                )
        else:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
                "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
                (worker_id, target, _apply_domain(target), ttl_seconds),
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
    _stamp_wait_started(conn, request_id)
    while True:
        code = _try_consume(conn, request_id)
        if code is not None:
            return code
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(max(0.05, poll_seconds), remaining))


def _stamp_wait_started(conn, request_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE otp_request SET wait_started_at=COALESCE(wait_started_at, now()) "
            "WHERE id=%s AND consumed_at IS NULL "
            "      AND (expires_at IS NULL OR expires_at > now())",
            (request_id,),
        )
    conn.commit()


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


def _eligible_for_request(request, match, received_at, skew_seconds: int) -> bool:
    requested_at = request["requested_at"]
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=_dt.timezone.utc)
    else:
        requested_at = requested_at.astimezone(_dt.timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=_dt.timezone.utc)
    else:
        received_at = received_at.astimezone(_dt.timezone.utc)
    requested_floor = requested_at - _dt.timedelta(seconds=skew_seconds)
    return (
        received_at >= requested_floor
        and _match_belongs_to_request(request.get("sender_hint"), match)
    )


def _unique_assignments(pending, parsed, used_ids, skew_seconds: int):
    remaining_messages = []
    seen_message_ids = set()
    for match, received_at in parsed:
        if (
            match.message_id
            and match.message_id not in used_ids
            and match.message_id not in seen_message_ids
        ):
            seen_message_ids.add(match.message_id)
            remaining_messages.append((match, received_at))
            if len(remaining_messages) == _MAX_RESPONDER_ITEMS:
                break
    assignments = _global_unique_assignments(
        pending,
        remaining_messages,
        request_id=lambda request: request["id"],
        message_id=lambda item: item[0].message_id,
        eligible=lambda request, item: _eligible_for_request(
            request, item[0], item[1], skew_seconds
        ),
        max_items=_MAX_RESPONDER_ITEMS,
    )
    return [(request, item[0]) for request, item in assignments]


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except BaseException:
        pass


def _release_responder_lock(conn, lock_key: int) -> None:
    cleanup_error = None
    try:
        conn.rollback()
    except BaseException as exc:
        cleanup_error = exc
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        conn.commit()
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
        _rollback_quietly(conn)
    if cleanup_error is not None:
        raise cleanup_error


def answer_pending(conn, gmail_service=None, *, window_minutes: int = 15,
                   max_messages: int = _DEFAULT_SCAN_MAX_MESSAGES, skew_seconds: int = 60,
                   answered_ttl_seconds: int | None = None) -> int:
    """Serialize mailbox scans and answer only unambiguous pending requests."""
    requested_max_messages = validate_max_messages(max_messages, cap=None)
    lock_key = _responder_lock_key()
    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (lock_key,),
            )
            acquired = bool(cur.fetchone()["acquired"])
        conn.commit()
        if not acquired:
            return 0
        answered = _answer_pending_locked(
            conn,
            gmail_service,
            window_minutes=window_minutes,
            max_messages=requested_max_messages,
            skew_seconds=skew_seconds,
            answered_ttl_seconds=answered_ttl_seconds,
        )
    except BaseException:
        if acquired:
            try:
                _release_responder_lock(conn, lock_key)
            except BaseException:
                pass
        else:
            _rollback_quietly(conn)
        raise
    _release_responder_lock(conn, lock_key)
    return answered


def _answer_pending_locked(conn, gmail_service=None, *, window_minutes: int = 15,
                           max_messages: int = _DEFAULT_SCAN_MAX_MESSAGES,
                           skew_seconds: int = 60,
                           answered_ttl_seconds: int | None = None) -> int:
    """Read Gmail once while holding the responder session lock.

    Home box only (this is the sole function that touches Gmail). Time-based match:
    a candidate fits a request when its email arrived >= requested_at - skew and
    belongs to the same provider. Ambiguous request/message components are left
    unanswered. The code is NEVER logged.

    When `gmail_service` is None (the default), the mailbox is read via
    get_mail_source() (IMAP app-password, falling back to the legacy Gmail API);
    passing a `gmail_service` explicitly preserves the old direct-service path.

    The fetch budget defaults to 1000 because the home inbox is noisy enough that a
    real verification mail can sit outside the newest 500 messages while still being
    well inside the 15-minute OTP window.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, requested_at, sender_hint FROM otp_request "
            "WHERE code IS NULL AND consumed_at IS NULL "
            "      AND expires_at > now() "
            "ORDER BY requested_at, id LIMIT %s",
            (_MAX_RESPONDER_ITEMS + 1,),
        )
        pending = cur.fetchall()
    conn.commit()
    if not pending or len(pending) > _MAX_RESPONDER_ITEMS:
        return 0
    if len(pending) > _MAX_RESPONDER_ITEMS:
        raise OtpResponderOverloadError("pending_request_snapshot_overflow")
    answered_ttl_seconds = _answered_ttl_seconds(answered_ttl_seconds)
    scan_max_messages = min(
        validate_max_messages(max_messages, cap=None), _MAX_RESPONDER_ITEMS
    )

    from applypilot.mail_source import MailSourceOverflowError

    try:
        if gmail_service is None:
            from applypilot.mail_source import get_auth_mail_source

            since_days = max(1, (window_minutes + 1439) // 1440)
            msgs = get_auth_mail_source().fetch(
                since_days=since_days,
                max_messages=scan_max_messages,
                gmail_raw_query=inbox_auth.AUTH_GMAIL_RAW_QUERY,
            )
            matches = inbox_auth.scan_gmail_for_auth_codes(
                messages=msgs, minutes=window_minutes, max_messages=scan_max_messages)
        else:
            matches = inbox_auth.scan_gmail_for_auth_codes(
                service=gmail_service, minutes=window_minutes,
                max_messages=scan_max_messages)
    except MailSourceOverflowError:
        return 0
    matches = inbox_auth.eligible_auth_matches(
        list(itertools.islice(matches, _MAX_RESPONDER_ITEMS)),
        reference_time=_dt.datetime.now(_dt.timezone.utc),
        skew_seconds=skew_seconds,
    )
    parsed = [(m, _parse_email_dt(m.received_at)) for m in matches]
    parsed = [(m, ts) for (m, ts) in parsed if ts is not None and m.message_id]
    parsed.sort(key=lambda mt: mt[1])
    unique_parsed = {}
    for match, received_at in parsed:
        unique_parsed.setdefault(match.message_id, (match, received_at))
        if len(unique_parsed) == _MAX_RESPONDER_ITEMS:
            break
    parsed = list(unique_parsed.values())
    if not parsed:
        return 0

    candidate_message_ids = list(dict.fromkeys(m.message_id for m, _ts in parsed))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT matched_message_id FROM otp_request "
            "WHERE matched_message_id = ANY(%s)",
            (candidate_message_ids,),
        )
        used_messages = {row["matched_message_id"] for row in cur.fetchall()}
    conn.commit()

    assignments = _unique_assignments(
        pending, parsed, used_messages, skew_seconds,
    )
    answered = 0
    for req, chosen in assignments:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
                    "matched_message_id=%s, answered_at=now(), "
                    "expires_at=GREATEST(expires_at, now() + make_interval(secs => %s)) "
                    "WHERE id=%s AND code IS NULL AND consumed_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > now())",
                    (chosen.candidate.value, chosen.candidate.kind,
                     _parse_email_dt(chosen.received_at), chosen.message_id,
                     answered_ttl_seconds, req["id"]),
                )
                updated = cur.rowcount
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            used_messages.add(chosen.message_id)
            continue
        if updated:
            used_messages.add(chosen.message_id)
            answered += updated
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
