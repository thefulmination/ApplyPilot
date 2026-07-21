from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from applypilot.brain.canonical_contract_v4 import (
    CANONICAL_DECISION_ENVELOPE_V1_FIELDS,
    CANONICAL_JOB_DECISION_V4_FIELDS,
    CanonicalContractV4Error,
    bind_canonical_decision_envelope_v1,
    compute_canonical_job_decision_v4_identities,
    compute_canonical_job_decision_v4_semantic_hash,
    decode_canonical_decision_envelope_v1,
    decode_canonical_decision_envelope_v1_canonical_json,
    decode_canonical_job_decision_v4,
    decode_canonical_job_decision_v4_canonical_json,
    derive_canonical_job_decision_v4_id,
    derive_scoring_request_id_v4,
)
from applypilot.brain.canonical_hash import canonicalize_jcs, jcs_sha256


FIXTURES = Path(__file__).parent / "fixtures" / "canonical_brain_v4"
TS_V4_FIXTURE_SHA256 = {
    "decision": "7d5702c618acdb5a0e954b44c719c04c5c9abacb36d58656fba4b5c0346eeacf",
    "envelope": "fe3cf9ff2f75eb3512d2def40d8c126f9c11ab6718e365e095ba4ce2986fe89b",
    "policy": "10268b09773963ca3c717b3fcd45ab35d6b96240c0c13d23af639a46dca5e510",
    "replay": "93bcd6d8486b07e9b6d482c4064946a37a53c788c66fb2bb0d01c605add1af1a",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def fixture_raw(name: str) -> str:
    return fixture_bytes(name).decode("utf-8")


def fixture_value(name: str) -> dict[str, Any]:
    return json.loads(fixture_raw(name))


def fixture_sha256(name: str) -> str:
    return hashlib.sha256(fixture_bytes(name)).hexdigest()


def decision_fixture() -> dict[str, Any]:
    return fixture_value("decision")


def envelope_fixture() -> dict[str, Any]:
    return fixture_value("envelope")


def with_identities(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    candidate = decision_fixture()
    if overrides:
        candidate.update(overrides)
    candidate["scoring_request_id"] = derive_scoring_request_id_v4(
        candidate["owner_id"],
        candidate["scoring_input_manifest_hash"],
    )
    candidate["semantic_content_hash"] = compute_canonical_job_decision_v4_semantic_hash(candidate)
    candidate["candidate_decision_id"] = derive_canonical_job_decision_v4_id(
        candidate["scoring_request_id"],
        candidate["semantic_content_hash"],
    )
    return candidate


def test_fixture_bytes_match_pinned_typescript_receipts_and_jcs_encoding() -> None:
    for name, expected_sha256 in TS_V4_FIXTURE_SHA256.items():
        raw_bytes = fixture_bytes(name)
        raw = raw_bytes.decode("utf-8")

        assert raw_bytes[:3] != b"\xef\xbb\xbf"
        assert not raw_bytes.endswith((b"\n", b"\r"))
        assert fixture_sha256(name) == expected_sha256
        assert canonicalize_jcs(json.loads(raw)) == raw


def test_policy_and_replay_fixture_hashes_prove_cross_language_jcs_parity() -> None:
    policy = fixture_value("policy")
    replay = fixture_value("replay")

    assert policy["policy_content_hash"] == jcs_sha256(policy["payload"])
    assert replay["replay_content_hash"] == jcs_sha256(replay["payload"])
    assert replay["policy_canonical_json_sha256"] == fixture_sha256("policy")
    assert replay["decision_canonical_json_sha256"] == fixture_sha256("decision")
    assert replay["envelope_canonical_json_sha256"] == fixture_sha256("envelope")
    assert policy["payload"]["max_safe_integer"] == 9_007_199_254_740_991
    assert policy["payload"]["semantic_array_order"] == [
        "qualification",
        "desirability",
        "career_direction",
        "ranking",
    ]
    assert policy["payload"]["unicode_probe"] == "cafe\u0301 / snowman \u2603 / rocket \U0001f680"


def test_decision_fixture_accepts_exact_canonical_bytes_and_nonrecursive_identities() -> None:
    raw = fixture_raw("decision")
    decision = decode_canonical_job_decision_v4_canonical_json(raw, fixture_sha256("decision"))

    assert tuple(decision) == CANONICAL_JOB_DECISION_V4_FIELDS
    assert compute_canonical_job_decision_v4_identities(decision) == {
        key: decision[key]
        for key in ("scoring_request_id", "semantic_content_hash", "candidate_decision_id")
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lane", "legacy"),
        ("execution_channel", "ats"),
        ("execution_scope", "ats:greenhouse:example.com:release-a"),
        ("outcome_score", 1),
        ("application_id", "application-1"),
        ("model_version", "generic-open-source"),
        ("worker_id", "worker-1"),
        ("candidateDecisionId", "decision-legacy"),
    ],
)
def test_decision_rejects_legacy_routing_and_nonsemantic_wire_fields(field: str, value: Any) -> None:
    candidate = decision_fixture()
    candidate[field] = value

    with pytest.raises(CanonicalContractV4Error, match="fields mismatch"):
        decode_canonical_job_decision_v4(candidate)


@pytest.mark.parametrize(
    ("verdict", "score", "floor", "message"),
    [
        ("qualified", 6.9, 7.0, "qualified requires"),
        ("unqualified", 7.0, 7.0, "unqualified requires"),
    ],
)
def test_qualification_verdict_must_agree_with_score_floor(
    verdict: str,
    score: float,
    floor: float,
    message: str,
) -> None:
    candidate = with_identities(
        {
            "recommendation_lane": "review",
            "qualification_verdict": verdict,
            "qualification_score": score,
            "qualification_floor": floor,
        }
    )

    with pytest.raises(CanonicalContractV4Error, match=message):
        decode_canonical_job_decision_v4(candidate)


def test_uncertain_requires_an_evidence_gap_or_closed_indeterminacy_reason() -> None:
    invalid = with_identities(
        {
            "recommendation_lane": "review",
            "qualification_verdict": "uncertain",
            "uncertainty": {
                "complete_graph_coverage": True,
                "material_requirement_unknown_count": 0,
                "reasons": ["coverage_complete"],
            },
        }
    )
    with pytest.raises(CanonicalContractV4Error, match="uncertain requires"):
        decode_canonical_job_decision_v4(invalid)

    for uncertainty in (
        {
            "complete_graph_coverage": False,
            "material_requirement_unknown_count": 0,
            "reasons": ["coverage_incomplete"],
        },
        {
            "complete_graph_coverage": True,
            "material_requirement_unknown_count": 1,
            "reasons": ["material_requirement_unresolved"],
        },
        {
            "complete_graph_coverage": True,
            "material_requirement_unknown_count": 0,
            "reasons": ["conflicting_factual_evidence"],
        },
    ):
        candidate = with_identities(
            {
                "recommendation_lane": "review",
                "qualification_verdict": "uncertain",
                "uncertainty": uncertainty,
            }
        )
        assert decode_canonical_job_decision_v4(candidate)["qualification_verdict"] == "uncertain"


@pytest.mark.parametrize("reason", ("\u0085reason", "\ufeffreason", "reason\u0085", "reason\ufeff"))
def test_uncertainty_reasons_reject_shared_forbidden_edge_code_points(reason: str) -> None:
    candidate = with_identities(
        {
            "uncertainty": {
                "complete_graph_coverage": True,
                "material_requirement_unknown_count": 0,
                "reasons": [reason],
            }
        }
    )

    with pytest.raises(CanonicalContractV4Error, match="uncertainty.reasons"):
        decode_canonical_job_decision_v4(candidate)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "qualification_verdict": "uncertain",
            "uncertainty": {
                "complete_graph_coverage": True,
                "material_requirement_unknown_count": 0,
                "reasons": ["conflicting_factual_evidence"],
            },
        },
        {
            "uncertainty": {
                "complete_graph_coverage": False,
                "material_requirement_unknown_count": 0,
                "reasons": ["coverage_incomplete"],
            }
        },
        {
            "uncertainty": {
                "complete_graph_coverage": True,
                "material_requirement_unknown_count": 1,
                "reasons": ["material_requirement_unresolved"],
            }
        },
    ],
)
def test_executable_lanes_require_qualified_complete_known_coverage(overrides: dict[str, Any]) -> None:
    candidate = with_identities(overrides)

    with pytest.raises(CanonicalContractV4Error, match="recommendation_lane"):
        decode_canonical_job_decision_v4(candidate)


def test_identity_derivation_uses_the_canonical_snapshot_not_mapping_get_coercion() -> None:
    actual = decision_fixture()
    fake_owner = f"owner-{'f' * 64}"
    fake_manifest_hash = "e" * 64

    class DeceptiveMapping(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            if key == "owner_id":
                return fake_owner
            if key == "scoring_input_manifest_hash":
                return fake_manifest_hash
            return super().get(key, default)

    identities = compute_canonical_job_decision_v4_identities(DeceptiveMapping(actual))

    assert identities["scoring_request_id"] == derive_scoring_request_id_v4(
        actual["owner_id"],
        actual["scoring_input_manifest_hash"],
    )
    assert identities["scoring_request_id"] != derive_scoring_request_id_v4(fake_owner, fake_manifest_hash)


def test_canonical_json_decoder_rejects_noncanonical_bytes_duplicate_fields_and_wrong_hash() -> None:
    raw = fixture_raw("decision")

    with pytest.raises(CanonicalContractV4Error, match="canonical encoding"):
        decode_canonical_job_decision_v4_canonical_json(f"{raw}\n", fixture_sha256("decision"))
    with pytest.raises(CanonicalContractV4Error, match="duplicate JSON field"):
        decode_canonical_job_decision_v4_canonical_json(
            raw.replace(
                '"schema_version":"canonical_job_decision_v4"',
                '"schema_version":"canonical_job_decision_v4","schema_version":"canonical_job_decision_v4"',
            ),
            fixture_sha256("decision"),
        )
    with pytest.raises(CanonicalContractV4Error, match="SHA-256 mismatch"):
        decode_canonical_job_decision_v4_canonical_json(raw, "0" * 64)


def test_envelope_is_nonsemantic_and_must_bind_to_the_candidate() -> None:
    decision = decode_canonical_job_decision_v4_canonical_json(
        fixture_raw("decision"),
        fixture_sha256("decision"),
    )
    envelope = decode_canonical_decision_envelope_v1_canonical_json(
        fixture_raw("envelope"),
        fixture_sha256("envelope"),
    )

    assert tuple(envelope) == CANONICAL_DECISION_ENVELOPE_V1_FIELDS
    assert bind_canonical_decision_envelope_v1(decision, envelope) == envelope
    candidate_id = decision["candidate_decision_id"]
    for field, value in (
        ("issued_at", "2026-07-18T17:00:00.000Z"),
        ("expires_at", "2026-07-25T17:00:00.000Z"),
        ("producer_release_id", f"release-{'a' * 64}"),
        ("artifact_manifest_hash", "0" * 64),
    ):
        changed = deepcopy(envelope)
        changed[field] = value
        assert decode_canonical_decision_envelope_v1(changed)["candidate_decision_id"] == candidate_id
        assert decision["candidate_decision_id"] == candidate_id

    mismatched = deepcopy(envelope)
    mismatched["candidate_decision_id"] = f"decision-{'f' * 64}"
    with pytest.raises(CanonicalContractV4Error, match="candidate_decision_id"):
        bind_canonical_decision_envelope_v1(decision, mismatched)


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    [
        ("0001-01-01T00:00:00.000Z", "0001-01-02T00:00:00.000Z"),
        ("9999-12-30T00:00:00.000Z", "9999-12-31T00:00:00.000Z"),
    ],
)
def test_envelope_accepts_canonical_utc_millisecond_year_boundaries(
    issued_at: str,
    expires_at: str,
) -> None:
    envelope = envelope_fixture()
    envelope["issued_at"] = issued_at
    envelope["expires_at"] = expires_at

    assert decode_canonical_decision_envelope_v1(envelope)["issued_at"] == issued_at


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuedAt", "2026-07-17T17:00:00.000Z"),
        ("issued_at", "2026-07-17T17:00:00Z"),
        ("issued_at", "0000-01-01T00:00:00.000Z"),
        ("expires_at", "2026-07-17T17:00:00.000Z"),
    ],
)
def test_envelope_rejects_unknown_noncanonical_and_nonincreasing_issuance(
    field: str,
    value: Any,
) -> None:
    envelope = envelope_fixture()
    envelope[field] = value

    with pytest.raises(CanonicalContractV4Error):
        decode_canonical_decision_envelope_v1(envelope)
