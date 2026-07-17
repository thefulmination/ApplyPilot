"""Pure compilation of SQLite decision policies into immutable artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import rfc8785


MAX_POLICY_METADATA_BYTES = 16 * 1024
ARTIFACT_MEDIA_TYPE = "application/json; profile=\"https://www.rfc-editor.org/rfc/rfc8785\""

_MODEL_SPECS = (
    ("qualification_model", "qualification"),
    ("preference_model", "preference"),
    ("outcome_model", "outcome"),
)
_SOURCE_TIMESTAMP_FIELDS = (
    ("created_at", "createdAt"),
    ("validated_at", "validatedAt"),
    ("activated_at", "activatedAt"),
    ("retired_at", "retiredAt"),
)
_SOURCE_REFERENCE_FIELDS = (
    ("kg_version", "kgVersion"),
    ("label_snapshot", "labelSnapshot"),
    ("pairwise_snapshot", "pairwiseSnapshot"),
    ("outcome_snapshot", "outcomeSnapshot"),
)


class PolicyArtifactError(ValueError):
    """Raised when a source policy cannot be compiled without losing integrity."""


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """An immutable, content-addressed RFC 8785 JSON artifact."""

    role: str
    sha256: str
    byte_length: int
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class CompiledPolicyArtifacts:
    """Policy metadata and the artifacts that can be proven from one SQLite row."""

    policy_version: str
    lane: str
    target_lifecycle: str
    policy_metadata: bytes
    artifacts: tuple[ArtifactDescriptor, ...]

    def artifact(self, role: str) -> ArtifactDescriptor | None:
        """Return the artifact for ``role``, if the source row proves one."""

        return next((artifact for artifact in self.artifacts if artifact.role == role), None)

    def metadata_object(self) -> dict[str, Any]:
        """Decode a fresh, caller-owned copy of the compact policy metadata."""

        value = json.loads(self.policy_metadata)
        if not isinstance(value, dict):  # pragma: no cover - compiler invariant
            raise AssertionError("compiled policy metadata is not an object")
        return value


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise PolicyArtifactError(f"{field} must be a nonempty string")
    if value != value.strip():
        raise PolicyArtifactError(f"{field} must not have boundary whitespace")
    if not value:
        raise PolicyArtifactError(f"{field} must be a nonempty string")
    return value


def _optional_text(row: Mapping[str, object], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyArtifactError(f"{field} must be null or a nonempty string")
    if value != value.strip():
        raise PolicyArtifactError(f"{field} must not have boundary whitespace")
    if not value:
        raise PolicyArtifactError(f"{field} must be null or a nonempty string")
    return value


def _timestamp(row: Mapping[str, object], field: str, *, required: bool = False) -> str | None:
    value = _optional_text(row, field)
    if value is None:
        if required:
            raise PolicyArtifactError(f"{field} must be a nonempty string")
        return None
    if "T" not in value and " " not in value:
        raise PolicyArtifactError(f"{field} must be a parseable ISO-8601 timestamp")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PolicyArtifactError(f"{field} must be a parseable ISO-8601 timestamp") from exc
    return value


def _optional_sha256(row: Mapping[str, object], field: str) -> str | None:
    value = _optional_text(row, field)
    if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PolicyArtifactError(f"{field} must be lowercase 64-character hexadecimal")
    return value


def _reject_constant(value: str) -> None:
    raise PolicyArtifactError(f"JSON contains non-standard numeric constant {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyArtifactError(f"JSON contains duplicate object key {key!r}")
        result[key] = value
    return result


def _parse_json_object(raw: object, field: str, *, optional: bool = False) -> dict[str, Any] | None:
    if raw is None:
        if optional:
            return None
        raise PolicyArtifactError(f"{field} must contain a JSON object")
    if not isinstance(raw, str):
        raise PolicyArtifactError(f"{field} must be JSON text")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except PolicyArtifactError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PolicyArtifactError(f"{field} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyArtifactError(f"{field} must contain a JSON object")
    return value


def _canonical_bytes(value: object, description: str) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise PolicyArtifactError(f"{description} cannot be serialized as RFC 8785 JSON: {exc}") from exc


def _artifact(role: str, value: object) -> ArtifactDescriptor:
    content = _canonical_bytes(value, f"{role} artifact")
    return ArtifactDescriptor(
        role=role,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        media_type=ARTIFACT_MEDIA_TYPE,
        content=content,
    )


def compile_policy_artifacts(row: Mapping[str, object]) -> CompiledPolicyArtifacts:
    """Compile only artifacts and metadata directly supported by a SQLite policy row.

    The compiler intentionally does not create knowledge-graph, corpus-snapshot,
    replay-input, approval, gate, or lifecycle-authority artifacts.
    """

    policy_version = _required_text(row, "policy_version")
    lane = _required_text(row, "lane")
    if lane not in {"ats", "linkedin"}:
        raise PolicyArtifactError("lane must be 'ats' or 'linkedin'")

    source_status = _required_text(row, "status")
    if source_status != "draft":
        raise PolicyArtifactError("only draft source policies may be compiled")

    model_names: dict[str, str | None] = {}
    for source_field, metadata_key in _MODEL_SPECS:
        model_names[metadata_key] = _optional_text(row, source_field)

    config = _parse_json_object(row.get("config_json"), "config_json", optional=True)
    metrics = _parse_json_object(row.get("metrics_json"), "metrics_json", optional=True)

    config_artifact = _artifact("config", config) if config is not None else None
    artifacts: list[ArtifactDescriptor] = []
    if config_artifact is not None:
        artifacts.append(config_artifact)
    metrics_artifact: ArtifactDescriptor | None = None
    if metrics is not None:
        metrics_artifact = _artifact("metrics", metrics)
        artifacts.append(metrics_artifact)

    for source_field, metadata_key in _MODEL_SPECS:
        model_name = model_names[metadata_key]
        if model_name is None:
            continue
        model_reference = {"modelName": model_name}
        if config_artifact is not None:
            model_reference["configSha256"] = config_artifact.sha256
        artifacts.append(
            _artifact(
                source_field,
                model_reference,
            )
        )

    replay_input_hash: str | None = None
    if metrics is not None:
        replay_input_hash = metrics.get("replayInputHash")
        if replay_input_hash is not None and not isinstance(replay_input_hash, str):
            raise PolicyArtifactError("metrics_json.replayInputHash must be a string when present")
        if isinstance(replay_input_hash, str) and replay_input_hash:
            if re.fullmatch(r"[0-9a-f]{64}", replay_input_hash) is None:
                raise PolicyArtifactError(
                    "metrics_json.replayInputHash must be lowercase 64-character hexadecimal"
                )
            assert metrics_artifact is not None
            artifacts.append(
                _artifact(
                    "replay_reference",
                    {
                        "contractVersion": 1,
                        "kind": "applypilot.policy.replay-reference",
                        "metricsSha256": metrics_artifact.sha256,
                        "replayInputSha256": replay_input_hash,
                    },
                )
            )
        elif replay_input_hash == "":
            replay_input_hash = None

    timestamps: dict[str, str | None] = {
        target_field: _timestamp(row, source_field, required=source_field == "created_at")
        for source_field, target_field in _SOURCE_TIMESTAMP_FIELDS
    }
    for source_field, target_field in _SOURCE_TIMESTAMP_FIELDS[1:]:
        if timestamps[target_field] is not None:
            raise PolicyArtifactError(f"draft source policies must not carry {source_field}")
    source_references = {
        target_field: _optional_sha256(row, source_field)
        for source_field, target_field in _SOURCE_REFERENCE_FIELDS
    }
    source_references["replayInputHash"] = replay_input_hash
    metadata = {
        "contractVersion": 1,
        "modelNames": model_names,
        "source": {
            "namespace": "sqlite.decision_policy_versions",
            "status": source_status,
            **timestamps,
        },
        "sourceReferences": source_references,
        "targetLifecycle": "draft",
    }
    metadata_bytes = _canonical_bytes(metadata, "policy metadata")
    if len(metadata_bytes) > MAX_POLICY_METADATA_BYTES:
        raise PolicyArtifactError(
            f"policy metadata is {len(metadata_bytes)} bytes; maximum is {MAX_POLICY_METADATA_BYTES}"
        )

    return CompiledPolicyArtifacts(
        policy_version=policy_version,
        lane=lane,
        target_lifecycle="draft",
        policy_metadata=metadata_bytes,
        artifacts=tuple(artifacts),
    )
