"""Fleet-wide OTP (email-verification code) relay over Postgres.

A remote worker that hits an email-verification wall files an ``otp_request`` and
polls it for a code; the home-side responder (answer_pending, below) reads the
home box's Gmail and writes the code into the row. The code lives in PG only for
the seconds between answer and consume, is single-use, and is NEVER logged. Gmail
is read only by ``answer_pending`` (home box). See the 2026-07-03 relay spec."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class RelayCode:
    value: str
    kind: str  # 'code' | 'magic_link'


def _apply_domain(application_url: str) -> str:
    return (urlparse(application_url or "").hostname or "").lower()


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
