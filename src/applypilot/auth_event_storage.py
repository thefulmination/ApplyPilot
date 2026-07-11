"""Privacy-minimized durable representation for local inbox auth events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from applypilot.ats_domains import ATS_SENDER_DOMAINS

MESSAGE_ID_PREFIX = "sha256:"
STORAGE_VERSION = 1
_OPAQUE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)
_LOCAL_RE = re.compile(r"[a-z0-9.!#$%&'*+/=?^_{|}~-]+\Z", re.IGNORECASE)
_MATCH_METHODS = frozenset({"code", "magic_link"})
_CONFIDENCE = frozenset({"low", "medium", "high"})


def external_message_id_digest(value: str) -> str:
    raw = str(value)
    return MESSAGE_ID_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def external_message_id_lookup_keys(value: str) -> tuple[str, str]:
    primary = external_message_id_digest(value)
    return primary, external_message_id_digest(primary)


def migration_message_id(value: str, storage_version: int) -> str:
    raw = str(value)
    if storage_version >= STORAGE_VERSION and _OPAQUE_ID_RE.fullmatch(raw.lower()):
        return raw.lower()
    return external_message_id_digest(raw)


def _strict_sender_domain(sender: str | None) -> str | None:
    raw = (sender or "").strip()
    if not raw:
        return None
    if _DOMAIN_RE.fullmatch(raw):
        return raw.lower()
    _, address = parseaddr(raw)
    if not address or address.count("@") != 1:
        return None
    if "<" in raw or ">" in raw:
        if raw.count("<") != 1 or raw.count(">") != 1 or not raw.endswith(">"):
            return None
        if raw[raw.index("<") + 1:-1] != address:
            return None
    elif raw != address:
        return None
    local, domain = address.rsplit("@", 1)
    if not _LOCAL_RE.fullmatch(local):
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return domain.lower() if _DOMAIN_RE.fullmatch(domain) else None


def canonical_ats_sender_domain(sender: str | None) -> str | None:
    domain = _strict_sender_domain(sender)
    if domain is None:
        return None
    matches = [
        trusted
        for trusted in ATS_SENDER_DOMAINS
        if domain == trusted or domain.endswith(f".{trusted}")
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: (item.count("."), len(item), item))


def canonical_utc_timestamp(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        pass
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def closed_match_method(value: str | None) -> str | None:
    return value if value in _MATCH_METHODS else None


def closed_confidence(value: str | None) -> str:
    return value if value in _CONFIDENCE else "low"


def scrub_inbox_events(conn, *, migration_now: str) -> None:
    """Idempotently minimize legacy rows while preserving integer references."""
    rows = conn.execute(
        """
        SELECT * FROM inbox_events
         WHERE COALESCE(storage_version, 0) < ?
         ORDER BY id
        """,
        (STORAGE_VERSION,),
    ).fetchall()
    if not rows:
        return
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(
            migration_message_id(row["message_id"], int(row["storage_version"] or 0)),
            [],
        ).append(row)
    digests = sorted(groups)
    for offset in range(0, len(digests), 900):
        chunk = digests[offset:offset + 900]
        placeholders = ",".join("?" for _ in chunk)
        current_rows = conn.execute(
            f"""
            SELECT * FROM inbox_events
             WHERE storage_version >= ? AND message_id IN ({placeholders})
            """,
            (STORAGE_VERSION, *chunk),
        ).fetchall()
        for row in current_rows:
            groups[str(row["message_id"]).lower()].append(row)

    for digest, group in groups.items():
        survivor = next(
            (row for row in group if str(row["message_id"]).lower() == digest),
            group[0],
        )
        event_ids = [int(row["id"]) for row in group]
        placeholders = ",".join("?" for _ in event_ids)
        challenges = conn.execute(
            f"""
            SELECT id, inbox_event_id FROM auth_challenges
             WHERE inbox_event_id IN ({placeholders}) ORDER BY id
            """,
            event_ids,
        ).fetchall()
        keeper = next(
            (
                row
                for row in challenges
                if int(row["inbox_event_id"]) == int(survivor["id"])
            ),
            challenges[0] if challenges else None,
        )
        for challenge in challenges:
            if keeper is not None and int(challenge["id"]) == int(keeper["id"]):
                continue
            conn.execute(
                """
                UPDATE auth_challenges
                   SET status='failed', resolved_at=NULL, inbox_event_id=NULL,
                       last_error='message_claim_conflict', updated_at=?
                 WHERE id=?
                """,
                (migration_now, challenge["id"]),
            )
        if keeper is not None and int(keeper["inbox_event_id"]) != int(survivor["id"]):
            conn.execute(
                "UPDATE auth_challenges SET inbox_event_id=?, updated_at=? WHERE id=?",
                (survivor["id"], migration_now, keeper["id"]),
            )

        duplicates = [event_id for event_id in event_ids if event_id != int(survivor["id"])]
        if duplicates:
            duplicate_marks = ",".join("?" for _ in duplicates)
            conn.execute(
                f"DELETE FROM inbox_events WHERE id IN ({duplicate_marks})", duplicates
            )

        sender_domain = next(
            (
                canonical
                for row in group
                for canonical in (
                    canonical_ats_sender_domain(row["sender"]),
                    canonical_ats_sender_domain(row["sender_domain"]),
                )
                if canonical is not None
            ),
            None,
        )
        received_at = next(
            (
                canonical
                for row in group
                for canonical in (canonical_utc_timestamp(row["received_at"]),)
                if canonical is not None
            ),
            None,
        )
        matched_method = next(
            (
                method
                for row in group
                for method in (closed_match_method(row["matched_method"]),)
                if method is not None
            ),
            None,
        )
        confidence = next(
            (
                str(row["confidence"])
                for row in group
                if row["confidence"] in _CONFIDENCE
            ),
            "low",
        )
        created_at = next(
            (
                canonical
                for row in group
                for canonical in (canonical_utc_timestamp(row["created_at"]),)
                if canonical is not None
            ),
            migration_now,
        )
        if int(survivor["storage_version"] or 0) < STORAGE_VERSION:
            conn.execute(
                """
                UPDATE inbox_events
                   SET message_id=?, thread_id=NULL, sender=NULL, sender_domain=?,
                       subject=NULL, received_at=?, event_type='auth_event', confidence=?,
                       matched_job_url=NULL, matched_company=NULL, matched_method=?,
                       snippet=NULL, created_at=?, storage_version=?
                 WHERE id=?
                """,
                (
                    digest,
                    sender_domain,
                    received_at,
                    confidence,
                    matched_method,
                    created_at,
                    STORAGE_VERSION,
                    survivor["id"],
                ),
            )
