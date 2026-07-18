from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest
import rfc8785

from applypilot.brain.policy_artifacts import (
    ARTIFACT_MEDIA_TYPE,
    MAX_POLICY_METADATA_BYTES,
    PolicyArtifactError,
    compile_policy_artifacts,
)


REPLAY_INPUT_HASH = "a" * 64
KG_VERSION = "b" * 64
LABEL_SNAPSHOT = "c" * 64
PAIRWISE_SNAPSHOT = "d" * 64
OUTCOME_SNAPSHOT = "e" * 64


def policy_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "policy_version": "canonical-v7-ats",
        "lane": "ats",
        "status": "draft",
        "qualification_model": "qualification-v7",
        "preference_model": "preference-v7",
        "outcome_model": "outcome-v7",
        "config_json": '{"threshold":0.8,"models":{"outcome":{"weight":0.2},'
        '"preference":{"weight":0.3},"qualificationEvidence":{"weight":0.5}}}',
        "metrics_json": '{"releaseGate":{"passed":false},"replayInputHash":"' + REPLAY_INPUT_HASH + '"}',
        "kg_version": None,
        "label_snapshot": None,
        "pairwise_snapshot": None,
        "outcome_snapshot": None,
        "created_at": "2026-07-15T12:00:00Z",
        "validated_at": None,
        "activated_at": None,
        "retired_at": None,
    }
    row.update(overrides)
    return row


def artifact_map(compiled) -> dict[str, object]:
    return {artifact.role: artifact for artifact in compiled.artifacts}


def test_compiles_deterministic_content_addressed_artifacts() -> None:
    first = compile_policy_artifacts(policy_row())
    second = compile_policy_artifacts(
        policy_row(
            config_json=' { "models" : { "qualificationEvidence":{"weight":0.5},'
            '"preference":{"weight":0.3},"outcome":{"weight":0.2}}, "threshold": 0.8 } ',
            metrics_json='{"replayInputHash":"' + REPLAY_INPUT_HASH + '","releaseGate":{"passed":false}}',
        )
    )

    assert first == second
    artifacts = artifact_map(first)
    assert tuple(artifact.role for artifact in first.artifacts) == (
        "config",
        "metrics",
        "qualification_model",
        "preference_model",
        "outcome_model",
        "replay_reference",
    )

    expected_config = rfc8785.dumps(json.loads(str(policy_row()["config_json"])))
    assert artifacts["config"].content == expected_config
    assert artifacts["config"].sha256 == hashlib.sha256(expected_config).hexdigest()
    assert artifacts["config"].byte_length == len(expected_config)

    qualification = json.loads(artifacts["qualification_model"].content)
    assert qualification == {
        "configSha256": artifacts["config"].sha256,
        "modelName": "qualification-v7",
    }
    replay = json.loads(artifacts["replay_reference"].content)
    assert replay == {
        "contractVersion": 1,
        "kind": "applypilot.policy.replay-reference",
        "metricsSha256": artifacts["metrics"].sha256,
        "replayInputSha256": REPLAY_INPUT_HASH,
    }

    with pytest.raises(FrozenInstanceError):
        artifacts["config"].sha256 = "changed"


def test_metadata_is_compact_and_never_embeds_large_payloads_or_authority_claims() -> None:
    marker = "large-config-marker-" + ("x" * 100_000)
    compiled = compile_policy_artifacts(
        policy_row(
            config_json=json.dumps({"models": {}, "payload": marker}),
            metrics_json=json.dumps(
                {
                    "largeReplayEvidence": "y" * 100_000,
                    "releaseGate": {"passed": True},
                    "replayInputHash": "b" * 64,
                }
            ),
        )
    )

    metadata = compiled.metadata_object()
    assert len(compiled.policy_metadata) < MAX_POLICY_METADATA_BYTES
    assert marker.encode() not in compiled.policy_metadata
    assert b"largeReplayEvidence" not in compiled.policy_metadata
    assert metadata == {
        "contractVersion": 1,
        "modelNames": {
            "outcome": "outcome-v7",
            "preference": "preference-v7",
            "qualification": "qualification-v7",
        },
        "source": {
            "activatedAt": None,
            "createdAt": "2026-07-15T12:00:00Z",
            "namespace": "sqlite.decision_policy_versions",
            "retiredAt": None,
            "status": "draft",
            "validatedAt": None,
        },
        "sourceReferences": {
            "kgVersion": None,
            "labelSnapshot": None,
            "outcomeSnapshot": None,
            "pairwiseSnapshot": None,
            "replayInputHash": "b" * 64,
        },
        "targetLifecycle": "draft",
    }
    assert not ({"knowledge_graph", "label_snapshot", "pairwise_snapshot", "outcome_snapshot"} & artifact_map(compiled).keys())
    assert "approval" not in compiled.policy_metadata.decode()
    assert "passed" not in compiled.policy_metadata.decode()


def test_null_metrics_omits_metrics_and_replay_artifacts() -> None:
    compiled = compile_policy_artifacts(policy_row(metrics_json=None))

    assert tuple(artifact.role for artifact in compiled.artifacts) == (
        "config",
        "qualification_model",
        "preference_model",
        "outcome_model",
    )


def test_metrics_without_nonempty_replay_input_hash_omits_only_replay() -> None:
    compiled = compile_policy_artifacts(policy_row(metrics_json='{"replayInputHash":""}'))

    assert compiled.artifact("metrics") is not None
    assert compiled.artifact("replay_reference") is None


@pytest.mark.parametrize(
    "replay_input_hash",
    ["not-a-sha256", "A" * 64, "a" * 63, "a" * 65, ("a" * 63) + "g", " " + ("a" * 64)],
)
def test_rejects_invalid_nonempty_replay_input_hash(replay_input_hash: str) -> None:
    metrics_json = json.dumps({"replayInputHash": replay_input_hash})

    with pytest.raises(PolicyArtifactError, match="lowercase 64-character hexadecimal"):
        compile_policy_artifacts(policy_row(metrics_json=metrics_json))


def test_nullable_legacy_fields_preserve_incomplete_draft_without_fabrication() -> None:
    compiled = compile_policy_artifacts(
        policy_row(
            qualification_model=None,
            preference_model=None,
            outcome_model=None,
            config_json=None,
            metrics_json=None,
        )
    )

    assert compiled.artifacts == ()
    assert compiled.metadata_object()["modelNames"] == {
        "outcome": None,
        "preference": None,
        "qualification": None,
    }


def test_preserves_only_supplied_source_reference_hashes() -> None:
    compiled = compile_policy_artifacts(
        policy_row(
            kg_version=KG_VERSION,
            label_snapshot=LABEL_SNAPSHOT,
            pairwise_snapshot=PAIRWISE_SNAPSHOT,
            outcome_snapshot=OUTCOME_SNAPSHOT,
        )
    )

    assert compiled.metadata_object()["sourceReferences"] == {
        "kgVersion": KG_VERSION,
        "labelSnapshot": LABEL_SNAPSHOT,
        "outcomeSnapshot": OUTCOME_SNAPSHOT,
        "pairwiseSnapshot": PAIRWISE_SNAPSHOT,
        "replayInputHash": REPLAY_INPUT_HASH,
    }


def test_compiles_only_artifacts_supported_by_present_nullable_fields() -> None:
    compiled = compile_policy_artifacts(
        policy_row(
            qualification_model="qualification-v7",
            preference_model=None,
            outcome_model="outcome-v7",
        )
    )

    assert tuple(artifact.role for artifact in compiled.artifacts) == (
        "config",
        "metrics",
        "qualification_model",
        "outcome_model",
        "replay_reference",
    )


def test_present_model_without_config_emits_model_name_only_artifacts() -> None:
    compiled = compile_policy_artifacts(policy_row(config_json=None))

    assert tuple(artifact.role for artifact in compiled.artifacts) == (
        "metrics",
        "qualification_model",
        "preference_model",
        "outcome_model",
        "replay_reference",
    )
    qualification = compiled.artifact("qualification_model")
    assert qualification is not None
    assert json.loads(qualification.content) == {"modelName": "qualification-v7"}
    assert compiled.metadata_object()["modelNames"]["qualification"] == "qualification-v7"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"config_json": "{"}, "config_json is invalid JSON"),
        ({"config_json": ""}, "config_json is invalid JSON"),
        ({"config_json": "   "}, "config_json is invalid JSON"),
        ({"metrics_json": "not-json"}, "metrics_json is invalid JSON"),
        ({"metrics_json": ""}, "metrics_json is invalid JSON"),
        ({"metrics_json": "\t"}, "metrics_json is invalid JSON"),
        ({"config_json": "[]"}, "config_json must contain a JSON object"),
        ({"config_json": '{"duplicate":1,"duplicate":2}'}, "duplicate object key"),
        ({"preference_model": " "}, "preference_model must not have boundary whitespace"),
        ({"status": "validated"}, "only draft source policies may be compiled"),
        ({"lane": " ats"}, "lane must not have boundary whitespace"),
        ({"status": "draft "}, "status must not have boundary whitespace"),
        ({"outcome_model": "outcome-v7\t"}, "outcome_model must not have boundary whitespace"),
        ({"created_at": " 2026-07-15T12:00:00Z"}, "created_at must not have boundary whitespace"),
        ({"created_at": "July 15, 2026"}, "created_at must be a parseable ISO-8601 timestamp"),
        ({"created_at": "2026-07-15"}, "created_at must be a parseable ISO-8601 timestamp"),
        ({"validated_at": "not-a-timestamp"}, "validated_at must be a parseable ISO-8601 timestamp"),
        ({"validated_at": "2026-07-15T13:00:00Z"}, "draft source policies must not carry validated_at"),
        ({"activated_at": "2026-07-15T14:00:00Z"}, "draft source policies must not carry activated_at"),
        ({"retired_at": "2026-07-15T15:00:00Z"}, "draft source policies must not carry retired_at"),
        ({"kg_version": "f" * 63}, "kg_version must be lowercase 64-character hexadecimal"),
        ({"label_snapshot": "A" * 64}, "label_snapshot must be lowercase 64-character hexadecimal"),
        ({"pairwise_snapshot": " " + ("a" * 64)}, "pairwise_snapshot must not have boundary whitespace"),
        ({"outcome_snapshot": ("a" * 63) + "g"}, "outcome_snapshot must be lowercase 64-character hexadecimal"),
    ],
)
def test_rejects_invalid_source_rows(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(PolicyArtifactError, match=message):
        compile_policy_artifacts(policy_row(**overrides))


def test_rejects_metadata_over_16_kib() -> None:
    with pytest.raises(PolicyArtifactError, match="maximum is 16384"):
        compile_policy_artifacts(policy_row(qualification_model="q" * MAX_POLICY_METADATA_BYTES))


def test_materializes_snapshot_references_as_canonical_content_addressed_bytes() -> None:
    overrides = {
        "label_snapshot": LABEL_SNAPSHOT,
        "pairwise_snapshot": PAIRWISE_SNAPSHOT,
        "outcome_snapshot": OUTCOME_SNAPSHOT,
    }
    first = compile_policy_artifacts(policy_row(**overrides))
    second = compile_policy_artifacts(
        policy_row(
            **overrides,
            config_json=' { "models" : { "qualificationEvidence":{"weight":0.5},'
            '"preference":{"weight":0.3},"outcome":{"weight":0.2}}, "threshold": 0.8 } ',
            metrics_json='{"replayInputHash":"' + REPLAY_INPUT_HASH + '","releaseGate":{"passed":false}}',
        )
    )
    artifacts = artifact_map(first)
    source_hashes = {
        "label_snapshot": LABEL_SNAPSHOT,
        "pairwise_snapshot": PAIRWISE_SNAPSHOT,
        "outcome_snapshot": OUTCOME_SNAPSHOT,
    }

    assert first == second
    assert tuple(
        artifact.role for artifact in first.artifacts if artifact.role.endswith("_snapshot")
    ) == ("label_snapshot", "pairwise_snapshot", "outcome_snapshot")
    for role, source_hash in source_hashes.items():
        artifact = artifacts[role]
        expected = {
            "kind": "applypilot.policy.snapshot-reference",
            "lane": "ats",
            "policyVersion": "canonical-v7-ats",
            "role": role,
            "schemaVersion": 1,
            "sourceField": role,
            "sourceSha256": source_hash,
        }
        assert artifact.content == rfc8785.dumps(expected)
        assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()
        assert artifact.sha256 != source_hash
        assert artifact.media_type == ARTIFACT_MEDIA_TYPE
        assert artifact.policy_source_id == artifact.content.decode("utf-8")
        assert json.loads(artifact.policy_source_id) == expected


@pytest.mark.parametrize(
    ("role", "source_hash"),
    [
        ("label_snapshot", LABEL_SNAPSHOT),
        ("pairwise_snapshot", PAIRWISE_SNAPSHOT),
        ("outcome_snapshot", OUTCOME_SNAPSHOT),
    ],
)
def test_one_non_null_snapshot_field_emits_exactly_one_wrapper(
    role: str,
    source_hash: str,
) -> None:
    values = {
        "label_snapshot": None,
        "pairwise_snapshot": None,
        "outcome_snapshot": None,
    }
    values[role] = source_hash

    snapshots = [
        artifact
        for artifact in compile_policy_artifacts(policy_row(**values)).artifacts
        if artifact.role.endswith("_snapshot")
    ]

    assert [artifact.role for artifact in snapshots] == [role]
    assert json.loads(snapshots[0].content)["sourceSha256"] == source_hash


def test_fourteen_policy_rows_match_exact_six_candidate_snapshot_identities() -> None:
    rows = [
        policy_row(
            policy_version="canonical-v7-ats-20260712",
            label_snapshot="951f869d6cca89c2b68dd8e88e1a072fced814b3086df11742a267a2f554d42a",
            pairwise_snapshot="9a87d1cadd836095f7c8c6cc3a7a965f49ef097a3b818ac05de41e84fb4b0cbd",
            outcome_snapshot="ddc78264cd2814771f68d1136179b1377f86966da6a6c14c738dad43de75f132",
        ),
        policy_row(
            policy_version="canonical-v7-linkedin-20260712",
            lane="linkedin",
            label_snapshot="d6b104672b91b92cd83232cc482fb568ed1dfec74ce7e00ff7e0e058cbb0919a",
            pairwise_snapshot="a3d99fcc9775ab6cca78f533d8425a49069cd85fbb71425884d7d55808d8b457",
            outcome_snapshot="b4309156d702da560844f8c6a413746461d1b09a0c29b134e3c5f41eecd9bb92",
        ),
    ] + [
        policy_row(policy_version=f"legacy-{index}", lane="ats" if index % 2 == 0 else "linkedin")
        for index in range(12)
    ]
    actual = []
    for row in rows:
        for artifact in compile_policy_artifacts(row).artifacts:
            if not artifact.role.endswith("_snapshot"):
                continue
            content = json.loads(artifact.content)
            actual.append(
                (
                    content["policyVersion"],
                    content["lane"],
                    content["role"],
                    content["sourceSha256"],
                    artifact.sha256,
                )
            )
    expected = [
        ("canonical-v7-ats-20260712", "ats", "label_snapshot", "951f869d6cca89c2b68dd8e88e1a072fced814b3086df11742a267a2f554d42a", "9909b5699316bd41c3f637955662a7aca7d69435b2f04b76d23d75dde689c317"),
        ("canonical-v7-ats-20260712", "ats", "pairwise_snapshot", "9a87d1cadd836095f7c8c6cc3a7a965f49ef097a3b818ac05de41e84fb4b0cbd", "6ea76a3e8c3cd2ce3a37a7c41d4d771557d6e389eb3478f56ef6eef025d37f5d"),
        ("canonical-v7-ats-20260712", "ats", "outcome_snapshot", "ddc78264cd2814771f68d1136179b1377f86966da6a6c14c738dad43de75f132", "a1878e841b0641e767774d0880e53906780602bae761b666fec22f83e3207a87"),
        ("canonical-v7-linkedin-20260712", "linkedin", "label_snapshot", "d6b104672b91b92cd83232cc482fb568ed1dfec74ce7e00ff7e0e058cbb0919a", "456cea0b9b5145768807d6ad1b404e7ef1fea8542c3bf9f171c9907303c726c3"),
        ("canonical-v7-linkedin-20260712", "linkedin", "pairwise_snapshot", "a3d99fcc9775ab6cca78f533d8425a49069cd85fbb71425884d7d55808d8b457", "5768bf39ae6ac5b45c05ea284045df0b10dc9103c4087418ccf6c9c0a1984128"),
        ("canonical-v7-linkedin-20260712", "linkedin", "outcome_snapshot", "b4309156d702da560844f8c6a413746461d1b09a0c29b134e3c5f41eecd9bb92", "e78815db0a531c44c531837c53854088d857b01caa6bb04e4753771c6f3ab256"),
    ]
    total_non_null_columns = sum(
        row[field] is not None
        for row in rows
        for field in ("label_snapshot", "pairwise_snapshot", "outcome_snapshot")
    )

    assert len(rows) == 14
    assert actual == expected
    assert len(actual) == total_non_null_columns == 6
