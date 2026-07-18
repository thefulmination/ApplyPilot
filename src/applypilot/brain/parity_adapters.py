"""Independent source-table parity projections for the SQLite brain import."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

from applypilot.brain.importer import SOURCE_TABLES
from applypilot.brain.parity import (
    ParityResult,
    SQLiteJSON,
    SQLiteTimestamp,
    Unresolved,
    compare_sorted,
)
from applypilot.brain.policy_artifacts import compile_policy_artifacts
from applypilot.brain.sqlite_to_postgres import SOURCE_NAMESPACE


_BINDABLE_POLICY_ROLES = frozenset(
    {
        "qualification_model",
        "preference_model",
        "outcome_model",
        "config",
        "metrics",
        "label_snapshot",
        "pairwise_snapshot",
        "outcome_snapshot",
    }
)
_MAX_POLICY_FILTER_BYTES = 1024 * 1024
_TARGET_BATCH_SIZE = 512


def _mapping(row: object, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, sqlite3.Row):
        return {column: row[column] for column in row.keys()}
    if isinstance(row, tuple) and len(row) == len(columns):
        return dict(zip(columns, row, strict=True))
    raise TypeError("parity queries require mapping-compatible rows")


def _sqlite_rows(
    source: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> Iterator[dict[str, Any]]:
    cursor = source.execute(sql, params)
    columns = tuple(description[0] for description in cursor.description or ())
    for row in cursor:
        yield _mapping(row, columns)


def _sqlite_one(
    source: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any] | None:
    cursor = source.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = tuple(description[0] for description in cursor.description or ())
    return _mapping(row, columns)


def _pg_rows(
    pg: Any,
    marker: str,
    sql: str,
    params: tuple[Any, ...],
) -> Iterator[dict[str, Any]]:
    try:
        cursor = pg.cursor(name=f"brain_parity_{marker}")
    except TypeError:  # Minimal recording fakes may expose cursor() only.
        cursor = pg.cursor()
    with cursor as cur:
        if hasattr(cur, "itersize"):
            cur.itersize = _TARGET_BATCH_SIZE
        cur.execute(sql, params)
        description = getattr(cur, "description", ()) or ()
        columns = tuple(item.name if hasattr(item, "name") else item[0] for item in description)
        for row in cur:
            yield _mapping(row, columns)


def _text_key(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("text parity keys must be strings")
    return value.encode("utf-8")


def _integer_key(value: object) -> bytes:
    number = int(value)
    if number < -(2**63) or number >= 2**63:
        raise ValueError("integer parity keys must fit in signed 64 bits")
    return (number + 2**63).to_bytes(8, "big")


def _job_id(url: str) -> str:
    return hashlib.sha256(f"{SOURCE_NAMESPACE}:job:{url}".encode()).hexdigest()


def _application_id(source_id: int) -> str:
    return hashlib.sha256(f"{SOURCE_NAMESPACE}:application:{source_id}".encode()).hexdigest()


def _source_timestamp(value: object) -> SQLiteTimestamp | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("SQLite timestamp projections must be text or null")
    return SQLiteTimestamp(value)


def _source_json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return {} if default is None else default
    if not isinstance(value, str):
        return value
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return SQLiteJSON(value)


def _metadata(row: Mapping[str, Any], excluded: set[str]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in excluded and not key.startswith("_parity_")}


def _pick(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row[field] for field in fields}


def _source_job_exists(source: sqlite3.Connection, url: object) -> bool:
    if not isinstance(url, str):
        return False
    return source.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone() is not None


_JOBS_FIELDS = (
    "job_id",
    "source_job_id",
    "canonical_url",
    "title",
    "company",
    "current_metadata",
)


def _source_jobs(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for row in _sqlite_rows(
        source,
        "SELECT * FROM jobs ORDER BY length(CAST(url AS BLOB)), CAST(url AS BLOB)",
    ):
        url = str(row["url"])
        yield (
            _text_key(url),
            {
                "job_id": _job_id(url),
                "source_job_id": url,
                "canonical_url": url,
                "title": row["title"],
                "company": row["company"],
                "current_metadata": _metadata(row, {"url", "title", "company"}),
            },
        )


def _target_jobs(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT job_id,source_job_id,canonical_url,title,company,current_metadata
        FROM brain_jobs
        WHERE source_namespace=%s
        ORDER BY octet_length(convert_to(source_job_id,'UTF8')),convert_to(source_job_id,'UTF8')
        /* parity:jobs */
    """
    for row in _pg_rows(pg, "jobs", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_job_id"]), _pick(row, _JOBS_FIELDS)


_ALIAS_FIELDS = (
    "job_id",
    "source_database_fingerprint",
    "source_item_id",
    "source_url",
    "alias_type",
    "alias_metadata",
)


def _source_aliases(
    source: sqlite3.Connection,
    source_fingerprint: str,
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for row in _sqlite_rows(
        source,
        "SELECT url FROM jobs ORDER BY length(CAST(url AS BLOB)), CAST(url AS BLOB)",
    ):
        url = str(row["url"])
        yield _text_key(url), {
            "job_id": _job_id(url),
            "source_database_fingerprint": source_fingerprint,
            "source_item_id": url,
            "source_url": url,
            "alias_type": "sqlite-job",
            "alias_metadata": {},
        }


def _target_aliases(
    pg: Any,
    source_fingerprint: str,
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT job_id,source_database_fingerprint,source_item_id,source_url,alias_type,alias_metadata
        FROM brain_job_aliases
        WHERE source_namespace=%s AND source_database_fingerprint=%s AND alias_type='sqlite-job'
        ORDER BY octet_length(convert_to(source_item_id,'UTF8')),convert_to(source_item_id,'UTF8')
        /* parity:aliases */
    """
    for row in _pg_rows(pg, "aliases", sql, (SOURCE_NAMESPACE, source_fingerprint)):
        yield _text_key(row["source_item_id"]), _pick(row, _ALIAS_FIELDS)


_OBSERVATION_FIELDS = (
    "source_observation_id",
    "job_id",
    "job_source_namespace",
    "observed_at",
    "observation_metadata",
)


def _source_observations(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for row in _sqlite_rows(
        source,
        "SELECT * FROM jobs ORDER BY length(CAST(url AS BLOB)), CAST(url AS BLOB)",
    ):
        url = str(row["url"])
        observed_at = row["discovered_at"] or row["detail_scraped_at"] or row["scored_at"] or row["applied_at"]
        yield _text_key(url), {
            "source_observation_id": hashlib.sha256(f"observation:{url}".encode()).hexdigest(),
            "job_id": _job_id(url),
            "job_source_namespace": SOURCE_NAMESPACE,
            "observed_at": _source_timestamp(observed_at),
            "observation_metadata": {
                "source_url": url,
                "site": row["site"],
                "source_board": row["source_board"],
            },
        }


def _target_observations(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT observation.source_observation_id,observation.job_id,
               job.source_namespace AS job_source_namespace,observation.observed_at,
               observation.observation_metadata,job.source_job_id
        FROM brain_job_observations observation
        JOIN brain_jobs job ON job.job_id=observation.job_id
        WHERE observation.source_namespace=%s
        ORDER BY octet_length(convert_to(job.source_job_id,'UTF8')),convert_to(job.source_job_id,'UTF8')
        /* parity:observations */
    """
    for row in _pg_rows(pg, "observations", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_job_id"]), _pick(row, _OBSERVATION_FIELDS)


_APPLICATION_FIELDS = (
    "application_id",
    "job_id",
    "source_application_id",
    "source_channel",
    "lane",
    "current_state",
    "application_metadata",
    "created_at",
    "updated_at",
)


def _source_applications(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for row in _sqlite_rows(source, "SELECT * FROM applications ORDER BY id"):
        source_id = int(row["id"])
        url = str(row["job_url"])
        channel = row["channel"] or row["source"] or "unknown"
        lane = "linkedin" if "linkedin" in str(channel).lower() or "linkedin.com" in url else "ats"
        yield (
            _integer_key(source_id),
            {
                "application_id": _application_id(source_id),
                "job_id": _job_id(url),
                "source_application_id": str(source_id),
                "source_channel": channel,
                "lane": lane,
                "current_state": row["status"],
                "application_metadata": _metadata(
                    row,
                    {"id", "job_url", "channel", "status", "created_at", "updated_at"},
                ),
                "created_at": _source_timestamp(row["created_at"]),
                "updated_at": _source_timestamp(row["updated_at"]),
            },
        )


def _target_applications(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT application_id,job_id,source_application_id,source_channel,lane,current_state,
               application_metadata,created_at,updated_at
        FROM brain_applications
        WHERE source_namespace=%s
        ORDER BY source_application_id::bigint
        /* parity:applications */
    """
    for row in _pg_rows(pg, "applications", sql, (SOURCE_NAMESPACE,)):
        yield _integer_key(row["source_application_id"]), _pick(row, _APPLICATION_FIELDS)


_APPLICATION_EVENT_FIELDS = (
    "application_id",
    "source_event_id",
    "event_type",
    "source_channel",
    "occurred_at",
    "event_metadata",
    "payload_artifact_hash",
    "supersedes_application_event_id",
)


def _source_application_events(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT event.*,application.id AS _parity_application_id
        FROM application_events event
        LEFT JOIN applications application ON application.job_url=event.job_url
        ORDER BY event.id
    """
    for row in _sqlite_rows(source, sql):
        source_id = int(row["id"])
        application_source_id = row["_parity_application_id"]
        application_id: str | Unresolved
        if application_source_id is None:
            application_id = Unresolved(f"application event {source_id} has no application")
        else:
            application_id = _application_id(int(application_source_id))
        yield (
            _integer_key(source_id),
            {
                "application_id": application_id,
                "source_event_id": str(source_id),
                "event_type": row["event_type"] or row["status"] or "event",
                "source_channel": row["channel"],
                "occurred_at": _source_timestamp(row["happened_at"]),
                "event_metadata": {
                    "status": row["status"],
                    "notes": row["notes"],
                    "job_url": row["job_url"],
                },
                "payload_artifact_hash": None,
                "supersedes_application_event_id": None,
            },
        )


def _target_application_events(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT application_id,source_event_id,event_type,source_channel,occurred_at,event_metadata,
               payload_artifact_hash,supersedes_application_event_id
        FROM brain_application_events
        WHERE source_namespace=%s
        ORDER BY source_event_id::bigint
        /* parity:application_events */
    """
    for row in _pg_rows(pg, "application_events", sql, (SOURCE_NAMESPACE,)):
        yield _integer_key(row["source_event_id"]), _pick(row, _APPLICATION_EVENT_FIELDS)


_EMAIL_FIELDS = (
    "source_event_id",
    "job_id",
    "event_type",
    "occurred_at",
    "event_metadata",
    "payload_artifact_hash",
    "supersedes_email_event_id",
)


def _source_email_events(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT email.*,CASE WHEN job.url IS NULL THEN 0 ELSE 1 END AS _parity_job_exists
        FROM email_events email
        LEFT JOIN jobs job ON job.url=email.job_url
        ORDER BY length(CAST(email.message_id AS BLOB)),CAST(email.message_id AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        message_id = str(row["message_id"])
        job_id = _job_id(str(row["job_url"])) if row["_parity_job_exists"] else None
        yield (
            _text_key(message_id),
            {
                "source_event_id": message_id,
                "job_id": job_id,
                "event_type": row["stage"] or row["outcome"] or "email",
                "occurred_at": _source_timestamp(row["occurred_at"]),
                "event_metadata": _metadata(
                    row,
                    {"message_id", "job_url", "occurred_at"},
                ),
                "payload_artifact_hash": None,
                "supersedes_email_event_id": None,
            },
        )


def _target_email_events(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = r"""
        SELECT source_event_id,job_id,event_type,occurred_at,event_metadata,
               payload_artifact_hash,supersedes_email_event_id
        FROM brain_email_events
        WHERE source_namespace=%s
          AND source_event_id !~ '^review:(0|[1-9][0-9]*)$'
          AND source_event_id !~ '^outcome-attribution:[0-9a-f]{64}$'
        ORDER BY octet_length(convert_to(source_event_id,'UTF8')),convert_to(source_event_id,'UTF8')
        /* parity:email_events */
    """
    for row in _pg_rows(pg, "email_events", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_event_id"]), _pick(row, _EMAIL_FIELDS)


def _source_email_reviews(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    for row in _sqlite_rows(source, "SELECT * FROM email_event_reviews ORDER BY id"):
        source_id = int(row["id"])
        source_email = _sqlite_one(
            source,
            "SELECT job_url FROM email_events WHERE message_id=?",
            (row["message_id"],),
        )
        source_url = source_email["job_url"] if source_email is not None else None
        url = row["corrected_job_url"] or source_url
        job_id = _job_id(url) if isinstance(url, str) and _source_job_exists(source, url) else None
        yield (
            _integer_key(source_id),
            {
                "source_event_id": f"review:{source_id}",
                "job_id": job_id,
                "event_type": "review",
                "occurred_at": _source_timestamp(row["reviewed_at"]),
                "event_metadata": dict(row),
                "payload_artifact_hash": None,
                "supersedes_email_event_id": None,
            },
        )


def _target_email_reviews(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = r"""
        SELECT source_event_id,job_id,event_type,occurred_at,event_metadata,
               payload_artifact_hash,supersedes_email_event_id
        FROM brain_email_events
        WHERE source_namespace=%s AND source_event_id ~ '^review:(0|[1-9][0-9]*)$'
        ORDER BY substring(source_event_id FROM 8)::bigint
        /* parity:email_event_reviews */
    """
    for row in _pg_rows(pg, "email_event_reviews", sql, (SOURCE_NAMESPACE,)):
        source_id = str(row["source_event_id"]).removeprefix("review:")
        yield _integer_key(source_id), _pick(row, _EMAIL_FIELDS)


_OUTCOME_FIELDS = (
    "row_kind",
    "source_event_id",
    "job_id",
    "review_status",
    "normalized_stage",
    "weight",
    "reviewer",
    "reason",
    "review_metadata",
    "created_at",
    "reviewed_at",
    "updated_at",
    "evidence_artifact_hash",
    "supersedes_reviewed_outcome_id",
    "linked_source_event_id",
    "linked_job_id",
    "linked_event_type",
    "linked_occurred_at",
    "linked_event_metadata",
    "linked_payload_artifact_hash",
    "linked_supersedes_email_event_id",
)


def _source_outcomes(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT * FROM reviewed_outcomes
        ORDER BY length(CAST(event_id || ':' || job_url AS BLOB)),
                 CAST(event_id || ':' || job_url AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        event_id = str(row["event_id"])
        job_url = str(row["job_url"])
        parity_key = f"{event_id}:{job_url}"
        job_id = _job_id(job_url)
        email = _sqlite_one(source, "SELECT * FROM email_events WHERE message_id=?", (event_id,))
        if email is None:
            linked_source_event_id: str | Unresolved = Unresolved(f"reviewed outcome {parity_key} has no source email")
            linked_job_id: str | Unresolved = Unresolved(f"reviewed outcome {parity_key} has no source email")
            linked_event_type: str | Unresolved = Unresolved(f"reviewed outcome {parity_key} has no source email")
            linked_occurred_at: SQLiteTimestamp | Unresolved = Unresolved(
                f"reviewed outcome {parity_key} has no source email"
            )
            linked_metadata: Mapping[str, Any] | Unresolved = Unresolved(
                f"reviewed outcome {parity_key} has no source email"
            )
        elif email["job_url"] == job_url and _source_job_exists(source, job_url):
            linked_source_event_id = event_id
            linked_job_id = job_id
            linked_event_type = email["stage"] or email["outcome"] or "email"
            linked_occurred_at = _source_timestamp(email["occurred_at"])
            linked_metadata = _metadata(email, {"message_id", "job_url", "occurred_at"})
        else:
            linked_source_event_id = "outcome-attribution:" + hashlib.sha256(parity_key.encode()).hexdigest()
            linked_job_id = job_id
            linked_event_type = "outcome-attribution"
            linked_occurred_at = _source_timestamp(email["occurred_at"])
            linked_metadata = {
                "source_message_id": event_id,
                "source_stage": email["stage"],
                "source_outcome": email["outcome"],
                "attribution": row["attribution_json"],
            }
        yield (
            _text_key(parity_key),
            {
                "row_kind": "outcome",
                "source_event_id": parity_key,
                "job_id": job_id,
                "review_status": row["review_status"],
                "normalized_stage": row["normalized_stage"],
                "weight": row["weight"],
                "reviewer": row["reviewer"],
                "reason": row["reason"],
                "review_metadata": _source_json(row["attribution_json"]),
                "created_at": _source_timestamp(row["created_at"]),
                "reviewed_at": _source_timestamp(row["reviewed_at"]),
                "updated_at": _source_timestamp(row["updated_at"]),
                "evidence_artifact_hash": None,
                "supersedes_reviewed_outcome_id": None,
                "linked_source_event_id": linked_source_event_id,
                "linked_job_id": linked_job_id,
                "linked_event_type": linked_event_type,
                "linked_occurred_at": linked_occurred_at,
                "linked_event_metadata": linked_metadata,
                "linked_payload_artifact_hash": None,
                "linked_supersedes_email_event_id": None,
            },
        )


def _target_outcomes(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = r"""
        WITH parity_rows AS (
            SELECT outcome.source_event_id AS parity_key,'outcome'::text AS row_kind,
                   outcome.source_event_id,outcome.job_id,outcome.review_status,
                   outcome.normalized_stage,outcome.weight,outcome.reviewer,outcome.reason,
                   outcome.review_metadata,outcome.created_at,outcome.reviewed_at,outcome.updated_at,
                   outcome.evidence_artifact_hash,outcome.supersedes_reviewed_outcome_id,
                   email.source_event_id AS linked_source_event_id,email.job_id AS linked_job_id,
                   email.event_type AS linked_event_type,email.occurred_at AS linked_occurred_at,
                   email.event_metadata AS linked_event_metadata,
                   email.payload_artifact_hash AS linked_payload_artifact_hash,
                   email.supersedes_email_event_id AS linked_supersedes_email_event_id
            FROM brain_reviewed_outcomes outcome
            JOIN brain_email_events email ON email.email_event_id=outcome.email_event_id
            WHERE outcome.source_namespace=%s
            UNION ALL
            SELECT email.source_event_id,'orphan_attribution'::text,NULL::text,email.job_id,
                   NULL::text,NULL::text,NULL::numeric,NULL::text,NULL::text,NULL::jsonb,
                   NULL::timestamptz,NULL::timestamptz,NULL::timestamptz,NULL::text,NULL::bigint,
                   email.source_event_id,email.job_id,email.event_type,email.occurred_at,
                   email.event_metadata,email.payload_artifact_hash,email.supersedes_email_event_id
            FROM brain_email_events email
            WHERE email.source_namespace=%s
              AND email.source_event_id ~ '^outcome-attribution:[0-9a-f]{64}$'
              AND NOT EXISTS (
                  SELECT 1 FROM brain_reviewed_outcomes outcome
                  WHERE outcome.source_namespace=%s AND outcome.email_event_id=email.email_event_id
              )
        )
        SELECT * FROM parity_rows
        ORDER BY octet_length(convert_to(parity_key,'UTF8')),convert_to(parity_key,'UTF8')
        /* parity:reviewed_outcomes */
    """
    params = (SOURCE_NAMESPACE, SOURCE_NAMESPACE, SOURCE_NAMESPACE)
    for row in _pg_rows(pg, "reviewed_outcomes", sql, params):
        yield _text_key(row["parity_key"]), _pick(row, _OUTCOME_FIELDS)


_LABEL_FIELDS = (
    "source_event_id",
    "job_id",
    "source_item_id",
    "source_item_url",
    "project",
    "method",
    "confidence",
    "weight",
    "label_name",
    "label_value",
    "occurred_at",
    "event_metadata",
    "raw_artifact_hash",
    "evidence_artifact_hash",
    "supersedes_label_event_source_id",
)


def _source_labels(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT label.*,CASE WHEN job.url IS NULL THEN 0 ELSE 1 END AS _parity_job_exists
        FROM research_labels label
        LEFT JOIN jobs job ON job.url=label.job_url
        ORDER BY length(CAST(label.id AS BLOB)),CAST(label.id AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        source_id = str(row["id"])
        job_id = _job_id(str(row["job_url"])) if row["_parity_job_exists"] else None
        yield (
            _text_key(source_id),
            {
                "source_event_id": source_id,
                "job_id": job_id,
                "source_item_id": row["item_id"],
                "source_item_url": row["job_url"],
                "project": row["source_project_id"] or "applypilot",
                "method": row["method"] or "import",
                "confidence": 1 if row["rating"] is not None else None,
                "weight": 1,
                "label_name": "fitmap_decision",
                "label_value": {
                    "decision": row["decision"],
                    "rating": row["rating"],
                    "reason": row["reason"],
                    "tags": row["tags_json"],
                },
                "occurred_at": _source_timestamp(row["created_at"]),
                "event_metadata": _source_json(row["raw_event_json"]),
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_label_event_source_id": None,
            },
        )


def _target_labels(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT label.source_event_id,label.job_id,label.source_item_id,label.source_item_url,
               label.project,label.method,label.confidence,label.weight,label.label_name,
               label.label_value,label.occurred_at,label.event_metadata,label.raw_artifact_hash,
               label.evidence_artifact_hash,NULL::text AS supersedes_label_event_source_id
        FROM brain_label_events label
        WHERE label.source_namespace=%s AND label.supersedes_label_event_id IS NULL
        ORDER BY octet_length(convert_to(label.source_event_id,'UTF8')),
                 convert_to(label.source_event_id,'UTF8')
        /* parity:research_labels */
    """
    for row in _pg_rows(pg, "research_labels", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_event_id"]), _pick(row, _LABEL_FIELDS)


def _source_label_confidence(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT confidence.*,
               label.id AS _parity_label_id,label.job_url AS _parity_job_url,
               label.item_id AS _parity_label_item_id,
               label.source_project_id AS _parity_project,label.decision AS _parity_decision,
               label.rating AS _parity_rating,
               CASE WHEN job.url IS NULL THEN 0 ELSE 1 END AS _parity_job_exists
        FROM research_label_confidence confidence
        LEFT JOIN research_labels label ON label.id=confidence.label_id
        LEFT JOIN jobs job ON job.url=label.job_url
        ORDER BY length(CAST('confidence:' || confidence.label_id AS BLOB)),
                 CAST('confidence:' || confidence.label_id AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        label_id = str(row["label_id"])
        source_event_id = f"confidence:{label_id}"
        label_present = row["_parity_label_id"] is not None
        unresolved = Unresolved(f"confidence {label_id} has no source label")
        source_item_id = row["_parity_label_item_id"] or row["item_id"] if label_present else unresolved
        source_item_url = row["_parity_job_url"] if label_present else unresolved
        job_id: str | None | Unresolved
        if not label_present:
            job_id = unresolved
        elif row["_parity_job_exists"]:
            job_id = _job_id(str(row["_parity_job_url"]))
        else:
            job_id = None
        yield (
            _text_key(source_event_id),
            {
                "source_event_id": source_event_id,
                "job_id": job_id,
                "source_item_id": source_item_id,
                "source_item_url": source_item_url,
                "project": (row["_parity_project"] or "applypilot") if label_present else unresolved,
                "method": row["method"] or "confidence-import",
                "confidence": max(Decimal(0), Decimal(1) - Decimal(str(row["item_flip_rate"] or 0))),
                "weight": Decimal(str(row["weight"])),
                "label_name": "fitmap_decision",
                "label_value": {
                    "decision": row["_parity_decision"] if label_present else unresolved,
                    "rating": row["_parity_rating"] if label_present else unresolved,
                },
                "occurred_at": _source_timestamp(row["imported_at"]),
                "event_metadata": _metadata(row, set()),
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_label_event_source_id": label_id if label_present else unresolved,
            },
        )


def _target_label_confidence(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT label.source_event_id,label.job_id,label.source_item_id,label.source_item_url,
               label.project,label.method,label.confidence,label.weight,label.label_name,
               label.label_value,label.occurred_at,label.event_metadata,label.raw_artifact_hash,
               label.evidence_artifact_hash,
               predecessor.source_event_id AS supersedes_label_event_source_id
        FROM brain_label_events label
        LEFT JOIN brain_label_events predecessor
          ON predecessor.label_event_id=label.supersedes_label_event_id
         AND predecessor.source_namespace=%s
        WHERE label.source_namespace=%s AND label.supersedes_label_event_id IS NOT NULL
        ORDER BY octet_length(convert_to(label.source_event_id,'UTF8')),
                 convert_to(label.source_event_id,'UTF8')
        /* parity:research_label_confidence */
    """
    params = (SOURCE_NAMESPACE, SOURCE_NAMESPACE)
    for row in _pg_rows(pg, "research_label_confidence", sql, params):
        yield _text_key(row["source_event_id"]), _pick(row, _LABEL_FIELDS)


_PAIRWISE_FIELDS = (
    "source_event_id",
    "left_job_id",
    "right_job_id",
    "left_source_item_id",
    "right_source_item_id",
    "left_source_url",
    "right_source_url",
    "project",
    "method",
    "confidence",
    "weight",
    "preference",
    "occurred_at",
    "event_metadata",
    "raw_artifact_hash",
    "evidence_artifact_hash",
    "supersedes_pairwise_event_id",
)


def _source_pairwise(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT pair.*,
               CASE WHEN left_job.url IS NULL THEN 0 ELSE 1 END AS _parity_left_job_exists,
               CASE WHEN right_job.url IS NULL THEN 0 ELSE 1 END AS _parity_right_job_exists
        FROM research_pairwise_labels pair
        LEFT JOIN jobs left_job ON left_job.url=pair.left_job_url
        LEFT JOIN jobs right_job ON right_job.url=pair.right_job_url
        ORDER BY length(CAST(pair.id AS BLOB)),CAST(pair.id AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        source_id = str(row["id"])
        preference = {"left": "left", "right": "right", "tie": "tie"}.get(
            str(row["winner"]),
            "unclear",
        )
        yield (
            _text_key(source_id),
            {
                "source_event_id": source_id,
                "left_job_id": _job_id(str(row["left_job_url"])) if row["_parity_left_job_exists"] else None,
                "right_job_id": _job_id(str(row["right_job_url"])) if row["_parity_right_job_exists"] else None,
                "left_source_item_id": row["left_item_id"],
                "right_source_item_id": row["right_item_id"],
                "left_source_url": row["left_job_url"],
                "right_source_url": row["right_job_url"],
                "project": row["source_project_id"] or "applypilot",
                "method": row["method"] or "import",
                "confidence": None,
                "weight": 1,
                "preference": preference,
                "occurred_at": _source_timestamp(row["created_at"]),
                "event_metadata": _source_json(row["raw_event_json"]),
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_pairwise_event_id": None,
            },
        )


def _target_pairwise(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT source_event_id,left_job_id,right_job_id,left_source_item_id,
               right_source_item_id,left_source_url,right_source_url,project,method,
               confidence,weight,preference,occurred_at,event_metadata,raw_artifact_hash,
               evidence_artifact_hash,supersedes_pairwise_event_id
        FROM brain_pairwise_events
        WHERE source_namespace=%s
        ORDER BY octet_length(convert_to(source_event_id,'UTF8')),convert_to(source_event_id,'UTF8')
        /* parity:research_pairwise_labels */
    """
    for row in _pg_rows(pg, "research_pairwise_labels", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_event_id"]), _pick(row, _PAIRWISE_FIELDS)


def _kg_content(row: Mapping[str, Any]) -> tuple[bytes | None, str | None, bool | Unresolved]:
    raw = row["compact_kg_json"]
    if raw is None:
        return None, None, Unresolved(f"knowledge graph {row['kg_version']} has no content")
    content = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    digest = hashlib.sha256(content).hexdigest()
    try:
        json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content, digest, Unresolved(f"knowledge graph {row['kg_version']} is invalid JSON")
    return content, digest, True


def _source_kg_artifacts(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT * FROM research_kg_artifacts
        ORDER BY length(CAST(kg_version AS BLOB)),CAST(kg_version AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        kg_version = str(row["kg_version"])
        content, digest, valid = _kg_content(row)
        claimed = row["inputs_sha"] or digest
        yield (
            _text_key(kg_version),
            {
                "content_sha256": digest or Unresolved(f"knowledge graph {kg_version} has no content"),
                "claimed_sha256": claimed or Unresolved(f"knowledge graph {kg_version} has no hash"),
                "byte_length": len(content)
                if content is not None
                else Unresolved(f"knowledge graph {kg_version} has no content"),
                "media_type": "application/json",
                "json_valid": valid,
            },
        )


def _target_kg_artifacts(
    pg: Any,
    source: sqlite3.Connection,
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql_source = """
        SELECT * FROM research_kg_artifacts
        ORDER BY length(CAST(kg_version AS BLOB)),CAST(kg_version AS BLOB)
    """
    sql_target = """
        SELECT artifact_hash,byte_length,media_type
        FROM brain_artifacts WHERE artifact_hash=%s
        /* parity:research_kg_artifacts */
    """
    for source_row in _sqlite_rows(source, sql_source):
        content, digest, _valid = _kg_content(source_row)
        if content is None or digest is None:
            continue
        for target in _pg_rows(pg, "research_kg_artifacts", sql_target, (digest,)):
            yield (
                _text_key(str(source_row["kg_version"])),
                {
                    "content_sha256": target["artifact_hash"],
                    "claimed_sha256": target["artifact_hash"],
                    "byte_length": target["byte_length"],
                    "media_type": target["media_type"],
                    "json_valid": True,
                },
            )


def _unsupported_source_rows(
    source: sqlite3.Connection,
    table: str,
    key_column: str,
    integer_key: bool,
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    order = key_column if integer_key else f"length(CAST({key_column} AS BLOB)),CAST({key_column} AS BLOB)"
    for row in _sqlite_rows(source, f'SELECT * FROM "{table}" ORDER BY {order}'):
        key = _integer_key(row[key_column]) if integer_key else _text_key(row[key_column])
        yield (
            b"S" + key,
            {
                "unsupported": Unresolved(f"{table} has no canonical target mapping"),
                "source_row": row,
            },
        )


def _unsupported_target_rows(pg: Any, table: str) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = f"""
        SELECT NULL::bytea AS unsupported_key,NULL::jsonb AS unsupported_value
        WHERE FALSE
        /* parity:{table} */
    """
    for row in _pg_rows(pg, table, sql, ()):
        raw_key = row["unsupported_key"]
        key = bytes(raw_key) if isinstance(raw_key, (bytes, bytearray, memoryview)) else _text_key(raw_key)
        yield (
            b"T" + key,
            {
                "unsupported": Unresolved(f"{table} target projection must be empty"),
                "target_row": row["unsupported_value"],
            },
        )


def _source_policy_ids(source: sqlite3.Connection) -> list[str]:
    policy_ids: list[str] = []
    used = 0
    sql = """
        SELECT policy_version FROM decision_policy_versions
        ORDER BY length(CAST(policy_version AS BLOB)),CAST(policy_version AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        policy_id = str(row["policy_version"])
        used += len(policy_id.encode("utf-8"))
        if used > _MAX_POLICY_FILTER_BYTES:
            raise ValueError("source policy IDs exceed the bounded Postgres filter budget")
        policy_ids.append(policy_id)
    return policy_ids


_POLICY_FIELDS = (
    "policy_version",
    "lane",
    "gate_definition_version",
    "lifecycle",
    "policy_metadata",
    "created_at",
    "validated_at",
    "canary_at",
    "activated_at",
    "retired_at",
    "artifact_bindings",
)


def _source_policies(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT * FROM decision_policy_versions
        ORDER BY length(CAST(policy_version AS BLOB)),CAST(policy_version AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        compiled = compile_policy_artifacts(row)
        metadata = compiled.metadata_object()
        bindings = [
            [artifact.role, artifact.sha256]
            for artifact in compiled.artifacts
            if artifact.role in _BINDABLE_POLICY_ROLES
        ]
        graph_hash = metadata["sourceReferences"]["kgVersion"]
        if graph_hash is not None:
            bindings.append(["knowledge_graph", graph_hash])
        bindings.sort(key=lambda binding: binding[0])
        yield (
            _text_key(compiled.policy_version),
            {
                "policy_version": compiled.policy_version,
                "lane": compiled.lane,
                "gate_definition_version": 1,
                "lifecycle": compiled.target_lifecycle,
                "policy_metadata": metadata,
                "created_at": _source_timestamp(row["created_at"]),
                "validated_at": None,
                "canary_at": None,
                "activated_at": None,
                "retired_at": None,
                "artifact_bindings": bindings,
            },
        )


def _target_policies(pg: Any, policy_ids: list[str]) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT policy.policy_version,policy.lane,policy.gate_definition_version,
               policy.lifecycle,policy.policy_metadata,policy.created_at,policy.validated_at,
               policy.canary_at,policy.activated_at,policy.retired_at,
               COALESCE((
                   SELECT jsonb_agg(
                       jsonb_build_array(binding.artifact_role,binding.artifact_hash)
                       ORDER BY binding.artifact_role
                   )
                   FROM brain_policy_artifacts binding
                   WHERE binding.policy_version=policy.policy_version
               ),'[]'::jsonb) AS artifact_bindings
        FROM brain_decision_policies policy
        WHERE policy.policy_version=ANY(%s::text[])
        ORDER BY octet_length(convert_to(policy.policy_version,'UTF8')),
                 convert_to(policy.policy_version,'UTF8')
        /* parity:decision_policy_versions */
    """
    for row in _pg_rows(pg, "decision_policy_versions", sql, (policy_ids,)):
        yield _text_key(row["policy_version"]), _pick(row, _POLICY_FIELDS)


_DECISION_FIELDS = (
    "decision_id",
    "source_decision_id",
    "job_id",
    "policy_version",
    "lane",
    "qualification_score",
    "qualification_floor",
    "preference_score",
    "outcome_score",
    "final_score",
    "qualification_verdict",
    "action",
    "confidence",
    "uncertainty",
    "blockers",
    "requirements",
    "evidence_nodes",
    "title_signals",
    "explanation",
    "input_hash",
    "created_at",
    "expires_at",
    "uncertainty_artifact_hash",
    "blockers_artifact_hash",
    "requirements_artifact_hash",
    "evidence_artifact_hash",
    "decision_artifact_hash",
)


def _source_decisions(source: sqlite3.Connection) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT decision_id,job_url,policy_version,lane,qualification_score,
               preference_score,outcome_score,final_score,qualification_verdict,
               action,confidence,uncertainty_json,blockers_json,requirements_json,
               evidence_node_ids_json,title_signals_json,explanation,input_hash,
               created_at,expires_at
        FROM job_decisions
        ORDER BY length(CAST(decision_id AS BLOB)),CAST(decision_id AS BLOB)
    """
    for row in _sqlite_rows(source, sql):
        decision_id = str(row["decision_id"])
        yield (
            _text_key(decision_id),
            {
                "decision_id": decision_id,
                "source_decision_id": decision_id,
                "job_id": _job_id(str(row["job_url"])),
                "policy_version": row["policy_version"],
                "lane": row["lane"],
                "qualification_score": row["qualification_score"],
                "qualification_floor": None,
                "preference_score": row["preference_score"],
                "outcome_score": row["outcome_score"],
                "final_score": row["final_score"],
                "qualification_verdict": row["qualification_verdict"],
                "action": row["action"],
                "confidence": row["confidence"],
                "uncertainty": _source_json(row["uncertainty_json"], []),
                "blockers": _source_json(row["blockers_json"], []),
                "requirements": _source_json(row["requirements_json"], []),
                "evidence_nodes": _source_json(row["evidence_node_ids_json"], []),
                "title_signals": _source_json(row["title_signals_json"], []),
                "explanation": row["explanation"],
                "input_hash": row["input_hash"],
                "created_at": _source_timestamp(row["created_at"]),
                "expires_at": _source_timestamp(row["expires_at"]),
                "uncertainty_artifact_hash": None,
                "blockers_artifact_hash": None,
                "requirements_artifact_hash": None,
                "evidence_artifact_hash": None,
                "decision_artifact_hash": None,
            },
        )


def _target_decisions(pg: Any) -> Iterator[tuple[bytes, dict[str, Any]]]:
    sql = """
        SELECT decision_id,source_decision_id,job_id,policy_version,lane,
               qualification_score,qualification_floor,preference_score,outcome_score,
               final_score,qualification_verdict,action,confidence,uncertainty,blockers,
               requirements,evidence_nodes,title_signals,explanation,input_hash,created_at,
               expires_at,uncertainty_artifact_hash,blockers_artifact_hash,
               requirements_artifact_hash,evidence_artifact_hash,decision_artifact_hash
        FROM brain_job_decisions
        WHERE source_namespace=%s
        ORDER BY octet_length(convert_to(source_decision_id,'UTF8')),
                 convert_to(source_decision_id,'UTF8')
        /* parity:job_decisions */
    """
    for row in _pg_rows(pg, "job_decisions", sql, (SOURCE_NAMESPACE,)):
        yield _text_key(row["source_decision_id"]), _pick(row, _DECISION_FIELDS)


def compute_import_parity(
    pg: Any,
    source: sqlite3.Connection,
    *,
    source_fingerprint: str,
) -> dict[str, ParityResult]:
    """Stream and compare all canonical SQLite import projections against Postgres."""

    policy_ids = _source_policy_ids(source)
    results = {
        "jobs": compare_sorted(_source_jobs(source), _target_jobs(pg)),
        "aliases": compare_sorted(
            _source_aliases(source, source_fingerprint),
            _target_aliases(pg, source_fingerprint),
        ),
        "observations": compare_sorted(
            _source_observations(source),
            _target_observations(pg),
        ),
        "applications": compare_sorted(_source_applications(source), _target_applications(pg)),
        "application_events": compare_sorted(
            _source_application_events(source),
            _target_application_events(pg),
        ),
        "email_events": compare_sorted(_source_email_events(source), _target_email_events(pg)),
        "email_event_reviews": compare_sorted(
            _source_email_reviews(source),
            _target_email_reviews(pg),
        ),
        "reviewed_outcomes": compare_sorted(_source_outcomes(source), _target_outcomes(pg)),
        "research_labels": compare_sorted(_source_labels(source), _target_labels(pg)),
        "research_label_confidence": compare_sorted(
            _source_label_confidence(source),
            _target_label_confidence(pg),
        ),
        "research_pairwise_labels": compare_sorted(
            _source_pairwise(source),
            _target_pairwise(pg),
        ),
        "research_kg_artifacts": compare_sorted(
            _source_kg_artifacts(source),
            _target_kg_artifacts(pg, source),
        ),
        "research_kg_runs": compare_sorted(
            _unsupported_source_rows(source, "research_kg_runs", "kg_version", False),
            _unsupported_target_rows(pg, "research_kg_runs"),
        ),
        "research_scores": compare_sorted(
            _unsupported_source_rows(source, "research_scores", "id", True),
            _unsupported_target_rows(pg, "research_scores"),
        ),
        "decision_policy_versions": compare_sorted(
            _source_policies(source),
            _target_policies(pg, policy_ids),
        ),
        "job_decisions": compare_sorted(
            _source_decisions(source),
            _target_decisions(pg),
        ),
    }
    expected = ("jobs", "aliases", "observations") + tuple(
        table.name for table in SOURCE_TABLES if table.name != "jobs"
    )
    if tuple(results) != expected:  # pragma: no cover - static contract assertion
        raise AssertionError("parity adapter result keys do not match SOURCE_TABLES")
    return results
