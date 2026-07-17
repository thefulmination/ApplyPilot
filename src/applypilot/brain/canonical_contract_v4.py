"""Strict owner-scoped V4 canonical decision and issuance-envelope contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from applypilot.brain.canonical_hash import canonicalize_jcs, jcs_sha256


CANONICAL_JOB_DECISION_V4_SCHEMA_VERSION = "canonical_job_decision_v4"
CANONICALIZATION_VERSION_RFC8785_JCS_V1 = "rfc8785-jcs-v1"
CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION = "canonical_decision_envelope_v1"

RECOMMENDATION_LANES_V4 = (
    "core_fit",
    "strategic_stretch",
    "qualified_fallback",
    "review",
    "reject_hold",
)
QUALIFICATION_VERDICTS_V4 = ("qualified", "unqualified", "uncertain")
CANONICAL_JOB_DECISION_V4_FIELDS = (
    "schema_version",
    "owner_id",
    "scoring_request_id",
    "candidate_decision_id",
    "policy_generation_id",
    "campaign_id",
    "canonical_job_id",
    "job_content_hash",
    "canonicalization_version",
    "recommendation_lane",
    "qualification_score",
    "qualification_floor",
    "qualification_verdict",
    "desirability_score",
    "career_direction_score",
    "ranking_score",
    "confidence",
    "uncertainty",
    "requirement_manifest_hash",
    "graph_snapshot_id",
    "fitmap_dataset_hash",
    "pairwise_dataset_hash",
    "scoring_input_manifest_hash",
    "evidence_manifest_hash",
    "semantic_content_hash",
)
CANONICAL_DECISION_ENVELOPE_V1_FIELDS = (
    "schema_version",
    "candidate_decision_id",
    "semantic_content_hash",
    "producer_release_id",
    "issued_at",
    "expires_at",
    "artifact_manifest_hash",
)

_UNCERTAINTY_FIELDS = (
    "complete_graph_coverage",
    "material_requirement_unknown_count",
    "reasons",
)
_CLOSED_INDETERMINACY_REASONS = frozenset(
    ("ambiguous_requirement_interpretation", "conflicting_factual_evidence")
)
_REASON_EDGE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_IDENTITY_PATTERNS = {
    "owner_id": re.compile(r"^owner-[a-f0-9]{64}$"),
    "scoring_request_id": re.compile(r"^scoring-request-[a-f0-9]{64}$"),
    "candidate_decision_id": re.compile(r"^decision-[a-f0-9]{64}$"),
    "policy_generation_id": re.compile(r"^policy-generation-[a-f0-9]{64}$"),
    "campaign_id": re.compile(r"^campaign-[a-f0-9]{64}$"),
    "canonical_job_id": re.compile(r"^job-[a-f0-9]{64}$"),
    "graph_snapshot_id": re.compile(r"^graph-snapshot-[a-f0-9]{64}$"),
}
_FORBIDDEN_WIRE_FIELD = re.compile(r"(?:^|_)(?:lane|execution|outcome|application|model|worker)(?:_|$)|[A-Z]")
_EXECUTABLE_RECOMMENDATION_LANES = frozenset(("core_fit", "strategic_stretch", "qualified_fallback"))
_CANONICAL_UTC_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class CanonicalContractV4Error(ValueError):
    """A V4 payload is malformed, noncanonical, or violates its semantic contract."""


def _fail(path: str, message: str) -> None:
    raise CanonicalContractV4Error(f"{path} {message}")


def _canonical_object(value: object, path: str) -> dict[str, Any]:
    try:
        snapshot = json.loads(canonicalize_jcs(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CanonicalContractV4Error(f"{path} must be a canonical JSON object") from exc
    if not isinstance(snapshot, dict):
        _fail(path, "must be a canonical JSON object")
    return snapshot


def _exact_object(value: object, keys: tuple[str, ...], path: str) -> dict[str, Any]:
    row = _canonical_object(value, path)
    actual = set(row)
    expected = set(keys)
    missing = tuple(key for key in keys if key not in actual)
    unknown = tuple(key for key in row if key not in expected)
    if missing or unknown:
        forbidden = tuple(key for key in unknown if _FORBIDDEN_WIRE_FIELD.search(key))
        reason = f"; forbidden wire fields=[{', '.join(forbidden)}]" if forbidden else ""
        _fail(
            path,
            f"fields mismatch; missing=[{', '.join(missing)}], unknown=[{', '.join(unknown)}]{reason}",
        )
    return row


def _exact_nested_object(value: object, keys: tuple[str, ...], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be a JSON object")
    row = dict(value)
    actual = set(row)
    expected = set(keys)
    missing = tuple(key for key in keys if key not in actual)
    unknown = tuple(key for key in row if key not in expected)
    if missing or unknown:
        _fail(path, f"fields mismatch; missing=[{', '.join(missing)}], unknown=[{', '.join(unknown)}]")
    return row


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_PATTERNS[name].fullmatch(value):
        _fail(name, "has an invalid identity format")
    return value


def _prefixed_identity(value: object, prefix: str, path: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"{re.escape(prefix)}-[a-f0-9]{{64}}", value):
        _fail(path, f"must be {prefix}-<64 lower hex>")
    return value


def _hash(value: object, path: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        _fail(path, "must be a lowercase SHA-256 digest")
    return value


def _enum(value: object, allowed: tuple[str, ...], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(path, f"must be one of {', '.join(allowed)}")
    return value


def _finite_number(value: object, minimum: float, maximum: float, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, f"must be finite and between {minimum:g} and {maximum:g}")
    number = float(value)
    if not math.isfinite(number) or (number == 0 and math.copysign(1.0, number) < 0) or not minimum <= number <= maximum:
        _fail(path, f"must be finite and between {minimum:g} and {maximum:g}")
    if number.is_integer() and abs(number) > _MAX_SAFE_INTEGER:
        _fail(path, "must be a safe number")
    return value


def _nonnegative_safe_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a nonnegative safe integer")
    number = float(value)
    if (
        not math.isfinite(number)
        or not number.is_integer()
        or number < 0
        or number > _MAX_SAFE_INTEGER
        or (number == 0 and math.copysign(1.0, number) < 0)
    ):
        _fail(path, "must be a nonnegative safe integer")
    return int(number)


def _sorted_unique_reasons(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    reasons: list[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item
            or item[0] in _REASON_EDGE_WHITESPACE
            or item[-1] in _REASON_EDGE_WHITESPACE
            or any(ord(character) <= 0x1F for character in item)
        ):
            _fail(f"{path}[{index}]", "must be a nonblank canonical string")
        reasons.append(item)
    for previous, current in zip(reasons, reasons[1:]):
        if previous == current:
            _fail(path, "must not contain duplicate reasons")
        if previous.encode("utf-16-be") > current.encode("utf-16-be"):
            _fail(path, "must be sorted by UTF-16 code units")
    return reasons


def _uncertainty(value: object) -> dict[str, Any]:
    row = _exact_nested_object(value, _UNCERTAINTY_FIELDS, "uncertainty")
    if not isinstance(row["complete_graph_coverage"], bool):
        _fail("uncertainty.complete_graph_coverage", "must be a boolean")
    return {
        "complete_graph_coverage": row["complete_graph_coverage"],
        "material_requirement_unknown_count": _nonnegative_safe_integer(
            row["material_requirement_unknown_count"],
            "uncertainty.material_requirement_unknown_count",
        ),
        "reasons": _sorted_unique_reasons(row["reasons"], "uncertainty.reasons"),
    }


def derive_scoring_request_id_v4(owner_id: str, scoring_input_manifest_hash: str) -> str:
    _identity(owner_id, "owner_id")
    _hash(scoring_input_manifest_hash, "scoring_input_manifest_hash")
    return f"scoring-request-{jcs_sha256({'owner_id': owner_id, 'scoring_input_manifest_hash': scoring_input_manifest_hash})}"


def compute_canonical_job_decision_v4_semantic_hash(candidate: Mapping[str, object]) -> str:
    row = _exact_object(candidate, CANONICAL_JOB_DECISION_V4_FIELDS, "canonical_job_decision_v4")
    projection = {
        key: value
        for key, value in row.items()
        if key not in {"candidate_decision_id", "semantic_content_hash"}
    }
    return jcs_sha256(projection)


def derive_canonical_job_decision_v4_id(scoring_request_id: str, semantic_content_hash: str) -> str:
    _identity(scoring_request_id, "scoring_request_id")
    _hash(semantic_content_hash, "semantic_content_hash")
    return f"decision-{jcs_sha256({'scoring_request_id': scoring_request_id, 'semantic_content_hash': semantic_content_hash})}"


def compute_canonical_job_decision_v4_identities(candidate: Mapping[str, object]) -> dict[str, str]:
    snapshot = _exact_object(candidate, CANONICAL_JOB_DECISION_V4_FIELDS, "canonical_job_decision_v4")
    scoring_request_id = derive_scoring_request_id_v4(
        _identity(snapshot["owner_id"], "owner_id"),
        _hash(snapshot["scoring_input_manifest_hash"], "scoring_input_manifest_hash"),
    )
    semantic_content_hash = compute_canonical_job_decision_v4_semantic_hash(snapshot)
    candidate_decision_id = derive_canonical_job_decision_v4_id(scoring_request_id, semantic_content_hash)
    return {
        "scoring_request_id": scoring_request_id,
        "semantic_content_hash": semantic_content_hash,
        "candidate_decision_id": candidate_decision_id,
    }


def decode_canonical_job_decision_v4(value: object) -> dict[str, Any]:
    row = _exact_object(value, CANONICAL_JOB_DECISION_V4_FIELDS, "canonical_job_decision_v4")
    if row["schema_version"] != CANONICAL_JOB_DECISION_V4_SCHEMA_VERSION:
        _fail("schema_version", f"must be {CANONICAL_JOB_DECISION_V4_SCHEMA_VERSION}")
    if row["canonicalization_version"] != CANONICALIZATION_VERSION_RFC8785_JCS_V1:
        _fail("canonicalization_version", f"must be {CANONICALIZATION_VERSION_RFC8785_JCS_V1}")

    decoded = {
        "schema_version": CANONICAL_JOB_DECISION_V4_SCHEMA_VERSION,
        "owner_id": _identity(row["owner_id"], "owner_id"),
        "scoring_request_id": _identity(row["scoring_request_id"], "scoring_request_id"),
        "candidate_decision_id": _identity(row["candidate_decision_id"], "candidate_decision_id"),
        "policy_generation_id": _identity(row["policy_generation_id"], "policy_generation_id"),
        "campaign_id": _identity(row["campaign_id"], "campaign_id"),
        "canonical_job_id": _identity(row["canonical_job_id"], "canonical_job_id"),
        "job_content_hash": _hash(row["job_content_hash"], "job_content_hash"),
        "canonicalization_version": CANONICALIZATION_VERSION_RFC8785_JCS_V1,
        "recommendation_lane": _enum(row["recommendation_lane"], RECOMMENDATION_LANES_V4, "recommendation_lane"),
        "qualification_score": _finite_number(row["qualification_score"], 0, 10, "qualification_score"),
        "qualification_floor": _finite_number(row["qualification_floor"], 0, 10, "qualification_floor"),
        "qualification_verdict": _enum(row["qualification_verdict"], QUALIFICATION_VERDICTS_V4, "qualification_verdict"),
        "desirability_score": _finite_number(row["desirability_score"], 0, 10, "desirability_score"),
        "career_direction_score": _finite_number(row["career_direction_score"], 0, 10, "career_direction_score"),
        "ranking_score": _finite_number(row["ranking_score"], 0, 10, "ranking_score"),
        "confidence": _finite_number(row["confidence"], 0, 1, "confidence"),
        "uncertainty": _uncertainty(row["uncertainty"]),
        "requirement_manifest_hash": _hash(row["requirement_manifest_hash"], "requirement_manifest_hash"),
        "graph_snapshot_id": _identity(row["graph_snapshot_id"], "graph_snapshot_id"),
        "fitmap_dataset_hash": _hash(row["fitmap_dataset_hash"], "fitmap_dataset_hash"),
        "pairwise_dataset_hash": _hash(row["pairwise_dataset_hash"], "pairwise_dataset_hash"),
        "scoring_input_manifest_hash": _hash(row["scoring_input_manifest_hash"], "scoring_input_manifest_hash"),
        "evidence_manifest_hash": _hash(row["evidence_manifest_hash"], "evidence_manifest_hash"),
        "semantic_content_hash": _hash(row["semantic_content_hash"], "semantic_content_hash"),
    }
    if decoded["qualification_verdict"] == "qualified" and decoded["qualification_score"] < decoded["qualification_floor"]:
        _fail("qualification_verdict", "qualified requires qualification_score >= qualification_floor")
    if decoded["qualification_verdict"] == "unqualified" and decoded["qualification_score"] >= decoded["qualification_floor"]:
        _fail("qualification_verdict", "unqualified requires qualification_score < qualification_floor")
    if (
        decoded["qualification_verdict"] == "uncertain"
        and decoded["uncertainty"]["complete_graph_coverage"]
        and decoded["uncertainty"]["material_requirement_unknown_count"] == 0
        and not any(
            reason in _CLOSED_INDETERMINACY_REASONS
            for reason in decoded["uncertainty"]["reasons"]
        )
    ):
        _fail(
            "qualification_verdict",
            "uncertain requires incomplete graph coverage, unknown requirements, or a closed factual-indeterminacy reason",
        )
    if (
        decoded["recommendation_lane"] in _EXECUTABLE_RECOMMENDATION_LANES
        and decoded["qualification_verdict"] != "qualified"
    ):
        _fail("recommendation_lane", "requires qualification_verdict=qualified")
    if (
        decoded["recommendation_lane"] in _EXECUTABLE_RECOMMENDATION_LANES
        and not decoded["uncertainty"]["complete_graph_coverage"]
    ):
        _fail("recommendation_lane", "requires complete graph coverage")
    if (
        decoded["recommendation_lane"] in _EXECUTABLE_RECOMMENDATION_LANES
        and decoded["uncertainty"]["material_requirement_unknown_count"] != 0
    ):
        _fail("recommendation_lane", "requires zero unknown material requirements")

    expected = compute_canonical_job_decision_v4_identities(decoded)
    for field in ("scoring_request_id", "semantic_content_hash", "candidate_decision_id"):
        if decoded[field] != expected[field]:
            _fail(field, f"must match canonical identity projection {expected[field]}")
    return decoded


def _reject_duplicate_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalContractV4Error(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CanonicalContractV4Error(f"nonfinite JSON constant: {value}")


def _parse_strict_json(canonical_json: str) -> object:
    if not isinstance(canonical_json, str):
        _fail("canonical JSON", "must be a string")
    try:
        return json.loads(
            canonical_json,
            object_pairs_hook=_reject_duplicate_json_fields,
            parse_constant=_reject_json_constant,
        )
    except CanonicalContractV4Error:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalContractV4Error(f"invalid canonical JSON: {exc.msg}") from exc


def _decode_canonical_json(canonical_json: str, expected_sha256: str, decoder: Any, name: str) -> dict[str, Any]:
    _hash(expected_sha256, "expected SHA-256")
    value = _parse_strict_json(canonical_json)
    if canonicalize_jcs(value) != canonical_json:
        _fail(name, "JSON payload is not in RFC 8785 canonical encoding")
    actual_sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if actual_sha256 != expected_sha256:
        _fail(name, f"canonical JSON SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    return decoder(value)


def decode_canonical_job_decision_v4_canonical_json(canonical_json: str, expected_sha256: str) -> dict[str, Any]:
    return _decode_canonical_json(
        canonical_json,
        expected_sha256,
        decode_canonical_job_decision_v4,
        CANONICAL_JOB_DECISION_V4_SCHEMA_VERSION,
    )


def _canonical_utc_milliseconds(value: object, path: str) -> str:
    if not isinstance(value, str) or not _CANONICAL_UTC_MILLISECONDS.fullmatch(value):
        _fail(path, "must be a canonical UTC millisecond timestamp")
    if value.startswith("0000"):
        _fail(path, "must use a year between 0001 and 9999")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise CanonicalContractV4Error(f"{path} must be a valid canonical UTC millisecond timestamp") from exc
    return value


def decode_canonical_decision_envelope_v1(value: object) -> dict[str, Any]:
    row = _exact_object(value, CANONICAL_DECISION_ENVELOPE_V1_FIELDS, CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION)
    if row["schema_version"] != CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION:
        _fail("schema_version", f"must be {CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION}")
    issued_at = _canonical_utc_milliseconds(row["issued_at"], "issued_at")
    expires_at = _canonical_utc_milliseconds(row["expires_at"], "expires_at")
    if expires_at <= issued_at:
        _fail("expires_at", "must be later than issued_at")
    return {
        "schema_version": CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION,
        "candidate_decision_id": _prefixed_identity(row["candidate_decision_id"], "decision", "candidate_decision_id"),
        "semantic_content_hash": _hash(row["semantic_content_hash"], "semantic_content_hash"),
        "producer_release_id": _prefixed_identity(row["producer_release_id"], "release", "producer_release_id"),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "artifact_manifest_hash": _hash(row["artifact_manifest_hash"], "artifact_manifest_hash"),
    }


def decode_canonical_decision_envelope_v1_canonical_json(canonical_json: str, expected_sha256: str) -> dict[str, Any]:
    return _decode_canonical_json(
        canonical_json,
        expected_sha256,
        decode_canonical_decision_envelope_v1,
        CANONICAL_DECISION_ENVELOPE_V1_SCHEMA_VERSION,
    )


def bind_canonical_decision_envelope_v1(
    decision_value: object,
    envelope_value: object,
) -> dict[str, Any]:
    decision = decode_canonical_job_decision_v4(decision_value)
    envelope = decode_canonical_decision_envelope_v1(envelope_value)
    for field in ("candidate_decision_id", "semantic_content_hash"):
        if envelope[field] != decision[field]:
            _fail(field, "does not match canonical_job_decision_v4")
    return envelope
