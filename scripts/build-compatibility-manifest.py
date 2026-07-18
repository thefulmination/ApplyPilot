#!/usr/bin/env python3
"""Build a new immutable cross-repository ApplyPilot compatibility manifest."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_SCHEMA_PATHS = tuple(f"src/applypilot/brain/schema_v{version}.sql" for version in range(1, 6))
_FLEET_PATHS = (
    "src/applypilot/fleet/schema.py",
    "src/applypilot/fleet/schema_v3.sql",
    "src/applypilot/fleet/migrations/manifest-v1.json",
)
_REQUIRED_ARTIFACT_ROLES = {
    "artifact_manifest",
    "knowledge_graph",
    "compact_knowledge_graph",
    "compact_kg_prompt_pack",
    "compact_canonical_facts",
    "compact_gap_signals",
    "fitmap_observations",
    "preference_profile",
    "pairwise_annotations",
    "pairwise_queue",
    "canonical_all_label_events",
    "label_learning_corpus",
    "label_learning_summary",
}
_TRUSTED_TEST_PRODUCERS = {"applypilot-release-verifier-v1"}
_TRUSTED_TOPOLOGY_PRODUCER = "applypilot-railway-topology-verifier-v1"
_TEST_SUITES = {
    "python": "applypilot-runtime-release-v1",
    "typescript": "applypilot-brain-release-v1",
}
_ATTESTATION_ALGORITHM = "HMAC-SHA256"
_MAX_TEST_RECEIPT_AGE = timedelta(hours=24)
_MAX_TOPOLOGY_RECEIPT_LIFETIME = timedelta(hours=1)
_TOPOLOGY_SOURCE_COMMAND = "railway status --json"
_TOPOLOGY_ENVIRONMENT = "staging"
_ROLLBACK_ROLES = {
    "rollback-only",
    "reference-only",
    "fleet-authority-rollback-only",
    "pre-integration-reference-not-automatic-runtime-rollback",
}
_BRAIN_KEYS = {
    "fitPolicyVersion",
    "fitPromptVersion",
    "qualificationModels",
    "preferenceModel",
    "outcomeModel",
    "llmScoringModel",
    "alternateLlmScoringModel",
    "embeddingModel",
    "decisionPolicies",
}
_POLICY_KEYS = {
    "status",
    "count",
    "rowSetSha256",
    "atsCanaryCandidate",
    "linkedinCanaryCandidate",
}
_CANDIDATE_KEYS = {
    "policyVersion",
    "kgVersion",
    "labelSnapshot",
    "pairwiseSnapshot",
    "outcomeSnapshot",
}


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _stable_read_bytes(path: Path) -> tuple[bytes, str]:
    """Read a small authority input twice and reject observable concurrent mutation."""
    before = path.stat()
    first = _read_bytes(path)
    middle = path.stat()
    second = _read_bytes(path)
    after = path.stat()
    if len({_file_identity(item) for item in (before, middle, after)}) != 1 or first != second:
        raise RuntimeError(f"release input changed while reading: {path}")
    return second, hashlib.sha256(second).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_record(path: Path) -> tuple[int, str]:
    """Double-hash a potentially large input without retaining it in memory."""
    before = path.stat()
    first = _sha256(path)
    middle = path.stat()
    second = _sha256(path)
    after = path.stat()
    if len({_file_identity(item) for item in (before, middle, after)}) != 1 or first != second:
        raise RuntimeError(f"release input changed while hashing: {path}")
    return after.st_size, second


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(repo: Path, commit: str, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{relative_path}"],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _git_file_record(repo: Path, commit: str, relative_path: str) -> tuple[dict[str, Any], bytes]:
    content = _git_bytes(repo, commit, relative_path)
    return (
        {
            "path": relative_path,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        content,
    )


def _git_identity(repo: Path, ref: str) -> tuple[str, str]:
    commit = _git(repo, "rev-parse", f"{ref}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    if not _COMMIT_RE.fullmatch(commit) or not _COMMIT_RE.fullmatch(tree):
        raise RuntimeError(f"invalid Git object identity from {repo}: commit={commit!r}, tree={tree!r}")
    return commit, tree


def _schema_bundle(records: list[tuple[dict[str, Any], bytes]]) -> str:
    digest = hashlib.sha256()
    for record, content in records:
        name = record["path"].encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _package_version(runtime_repo: Path, runtime_commit: str) -> str:
    document = tomllib.loads(_git_bytes(runtime_repo, runtime_commit, "pyproject.toml").decode("utf-8"))
    version = document.get("project", {}).get("version")
    if not _nonempty_string(version):
        raise RuntimeError("pyproject.toml does not contain a project version")
    return version


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256_value(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _positive_or_zero_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _attestation_configuration() -> tuple[bytes, str]:
    encoded_key = os.environ.get("APPLYPILOT_RELEASE_ATTESTATION_KEY_B64")
    key_id = os.environ.get("APPLYPILOT_RELEASE_ATTESTATION_KEY_ID")
    if not _nonempty_string(encoded_key) or not _nonempty_string(key_id):
        raise RuntimeError("release attestation key and key ID environment variables are required")
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("release attestation key must be valid base64") from exc
    if len(key) < 32:
        raise RuntimeError("release attestation key must decode to at least 32 bytes")
    return key, key_id


def _canonical_receipt_payload(receipt: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in receipt.items() if key != "authentication"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _authenticated_receipt(
    path: Path,
    key: bytes,
    expected_key_id: str,
    label: str,
) -> tuple[dict[str, Any], bytes, str, str]:
    receipt_bytes, receipt_sha256 = _stable_read_bytes(path)
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(receipt, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    authentication = receipt.get("authentication")
    if not isinstance(authentication, dict) or set(authentication) != {
        "algorithm",
        "keyId",
        "signature",
    }:
        raise RuntimeError(f"{label} must contain exactly one authentication object")
    if authentication.get("algorithm") != _ATTESTATION_ALGORITHM:
        raise RuntimeError(f"{label} authentication algorithm is unsupported")
    if authentication.get("keyId") != expected_key_id:
        raise RuntimeError(f"{label} authentication key ID does not match the expected key")
    signature = authentication.get("signature")
    if not _nonempty_string(signature):
        raise RuntimeError(f"{label} authentication signature is missing")
    try:
        supplied = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"{label} authentication signature is not valid base64") from exc
    expected = hmac.digest(key, _canonical_receipt_payload(receipt), hashlib.sha256)
    if not hmac.compare_digest(supplied, expected):
        raise RuntimeError(f"{label} authentication signature is invalid")
    return receipt, receipt_bytes, receipt_sha256, authentication["keyId"]


def _normalized_artifact_path(value: Any) -> PurePosixPath:
    if not _nonempty_string(value):
        raise RuntimeError(f"artifact path must be a normalized relative POSIX path: {value!r}")
    if "\\" in value or value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        raise RuntimeError(f"artifact path must be a normalized relative POSIX path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise RuntimeError(f"artifact path must be a normalized relative POSIX path: {value!r}")
    logical = PurePosixPath(value)
    if logical.is_absolute() or logical.as_posix() != value:
        raise RuntimeError(f"artifact path must be a normalized relative POSIX path: {value!r}")
    return logical


def _contained_regular_file(source_root: Path, logical: PurePosixPath) -> Path:
    candidate = source_root
    for part in logical.parts:
        candidate /= part
        if candidate.is_symlink():
            raise RuntimeError(f"artifact path contains a symlink: {logical.as_posix()}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"artifact path is missing from source root: {logical.as_posix()}") from exc
    if not resolved.is_relative_to(source_root) or not resolved.is_file():
        raise RuntimeError(f"artifact path escapes or is not a regular file: {logical.as_posix()}")
    return resolved


def _validate_artifact_record(item: Any) -> tuple[str, PurePosixPath, int, str]:
    if not isinstance(item, dict) or set(item) != {"role", "path", "bytes", "sha256"}:
        raise RuntimeError("artifact records require exactly role, path, bytes, and sha256")
    role = item["role"]
    if not _nonempty_string(role):
        raise RuntimeError("artifact role must be a non-empty string")
    logical = _normalized_artifact_path(item["path"])
    if not _positive_or_zero_integer(item["bytes"]) or not _sha256_value(item["sha256"]):
        raise RuntimeError(f"artifact bytes/hash are invalid for {logical.as_posix()}")
    return role, logical, item["bytes"], item["sha256"]


def _reconcile_artifact_manifest(inventory_path: Path, verified: list[dict[str, Any]]) -> None:
    content, digest = _stable_read_bytes(inventory_path)
    manifest_record = next(item for item in verified if item["role"] == "artifact_manifest")
    if len(content) != manifest_record["bytes"] or digest != manifest_record["sha256"]:
        raise RuntimeError("artifact manifest changed after inventory verification")
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("artifact manifest is not valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "files"}
        or document.get("schemaVersion") != "applypilot_artifact_manifest_v2"
        or not isinstance(document.get("files"), list)
    ):
        raise RuntimeError("artifact manifest must use applypilot_artifact_manifest_v2")
    internal: list[dict[str, Any]] = []
    for item in document["files"]:
        role, logical, byte_length, sha256 = _validate_artifact_record(item)
        internal.append({"role": role, "path": logical.as_posix(), "bytes": byte_length, "sha256": sha256})
    expected = [item for item in verified if item["role"] != "artifact_manifest"]
    if sorted(internal, key=lambda item: item["role"]) != sorted(expected, key=lambda item: item["role"]):
        raise RuntimeError("artifact manifest does not reconcile every required non-manifest role")


def _verify_artifacts(template: dict[str, Any], source_override: Path | None) -> dict[str, Any]:
    artifacts = template.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("files"), list):
        raise RuntimeError("template does not contain an artifact inventory")
    source_value = source_override if source_override is not None else artifacts.get("sourceRoot")
    if not isinstance(source_value, (str, Path)) or not str(source_value).strip():
        raise RuntimeError("artifact source root is missing")
    source_root = Path(source_value).resolve(strict=True)
    if not source_root.is_dir():
        raise RuntimeError("artifact source root is not a directory")

    validated = [_validate_artifact_record(item) for item in artifacts["files"]]
    roles = [item[0] for item in validated]
    if set(roles) != _REQUIRED_ARTIFACT_ROLES or len(roles) != len(set(roles)):
        raise RuntimeError("artifact inventory must contain each required role exactly once")

    logical_paths: set[str] = set()
    physical_paths: set[tuple[int, int] | str] = set()
    verified_files: list[dict[str, Any]] = []
    for role, logical, expected_bytes, expected_sha256 in validated:
        logical_key = os.path.normcase(logical.as_posix())
        if logical_key in logical_paths:
            raise RuntimeError("artifact inventory contains duplicate normalized paths")
        logical_paths.add(logical_key)
        path = _contained_regular_file(source_root, logical)
        stat_result = path.stat()
        physical_key: tuple[int, int] | str
        if stat_result.st_ino:
            physical_key = (stat_result.st_dev, stat_result.st_ino)
        else:
            physical_key = os.path.normcase(str(path))
        if physical_key in physical_paths:
            raise RuntimeError("artifact inventory contains duplicate physical paths")
        physical_paths.add(physical_key)
        actual_bytes, actual_sha256 = _stable_file_record(path)
        if actual_bytes != expected_bytes or actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"artifact drift: {path}; expected bytes/hash {expected_bytes}/{expected_sha256}, "
                f"got {actual_bytes}/{actual_sha256}"
            )
        verified_files.append(
            {
                "role": role,
                "path": logical.as_posix(),
                "bytes": actual_bytes,
                "sha256": actual_sha256,
            }
        )
    verified_files.sort(key=lambda item: (item["role"], item["path"]))
    inventory_record = next(item for item in verified_files if item["role"] == "artifact_manifest")
    _reconcile_artifact_manifest(source_root / inventory_record["path"], verified_files)
    distribution = artifacts.get("distribution", "external-content-addressed-bootstrap-required")
    if distribution != "external-content-addressed-bootstrap-required":
        raise RuntimeError("artifact distribution policy is unsupported")
    return {
        "distribution": distribution,
        "logicalSource": "pinned-external-release-inputs",
        "files": verified_files,
    }


def _verify_sqlite_source(
    template: dict[str, Any],
    source_override: Path | None,
) -> dict[str, Any]:
    source = template.get("canonicalSqliteSource")
    if not isinstance(source, dict):
        raise RuntimeError("template does not contain canonicalSqliteSource")
    source_value = source_override if source_override is not None else source.get("path")
    if not isinstance(source_value, (str, Path)) or not str(source_value).strip():
        raise RuntimeError("canonical SQLite source path is missing")
    path = Path(source_value).resolve()
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"canonical SQLite source is missing or is a symlink: {path}")
    if not _positive_or_zero_integer(source.get("bytes")) or not _sha256_value(source.get("sha256")):
        raise RuntimeError("canonical SQLite source bytes/hash declaration is invalid")
    before = path.stat()
    actual_bytes, actual_sha256 = _stable_file_record(path)
    if actual_bytes != source["bytes"] or actual_sha256 != source["sha256"]:
        raise RuntimeError(
            f"canonical SQLite source drift: {path}; expected bytes/hash "
            f"{source['bytes']}/{source['sha256']}, got {actual_bytes}/{actual_sha256}"
        )
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            quick_check_rows = connection.execute("PRAGMA quick_check").fetchall()
            if quick_check_rows != [("ok",)]:
                raise RuntimeError(f"canonical SQLite source failed quick_check: {quick_check_rows!r}")
            pragmas = {
                name: connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("schema_version", "page_count", "page_size", "user_version")
            }
            table_names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name COLLATE BINARY"
                )
            ]
            counts = {
                name: connection.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
                for name in table_names
            }
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("canonical SQLite source is not a valid SQLite database") from exc
    after_bytes, after_sha256 = _stable_file_record(path)
    after = path.stat()
    if _file_identity(before) != _file_identity(after) or (after_bytes, after_sha256) != (actual_bytes, actual_sha256):
        raise RuntimeError(f"release input changed while inspecting SQLite source: {path}")
    return {
        "logicalSource": "canonical-sqlite-authority-snapshot",
        "bytes": actual_bytes,
        "sha256": actual_sha256,
        "quickCheck": "ok",
        "schemaVersion": pragmas["schema_version"],
        "pageCount": pragmas["page_count"],
        "pageSize": pragmas["page_size"],
        "userVersion": pragmas["user_version"],
        "tableCounts": counts,
        "sourceUnchangedDuringCapture": True,
    }


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not _nonempty_string(value):
        raise RuntimeError(f"{label} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _validated_rollback(repo: Path, value: Any, source_commit: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"branch", "commitSha", "role"}:
        raise RuntimeError("repository rollback identity must contain exactly branch, commitSha, and role")
    branch = value["branch"]
    commit_sha = value["commitSha"]
    role = value["role"]
    if not _nonempty_string(branch) or not _COMMIT_RE.fullmatch(str(commit_sha)) or role not in _ROLLBACK_ROLES:
        raise RuntimeError("repository rollback branch, commit, or role is invalid")
    if commit_sha == source_commit:
        raise RuntimeError("repository rollback commit must differ from the release source commit")
    try:
        resolved_commit = _git(repo, "rev-parse", f"{commit_sha}^{{commit}}")
        branch_commit = _git(repo, "rev-parse", f"{branch}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("repository rollback branch or commit does not resolve") from exc
    if resolved_commit != commit_sha:
        raise RuntimeError("repository rollback commit is not an exact full Git commit identity")
    reachability = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit_sha, branch_commit],
        capture_output=True,
        text=True,
    )
    if reachability.returncode != 0:
        raise RuntimeError("repository rollback commit is not reachable from its declared branch")
    return {"branch": branch, "commitSha": commit_sha, "role": role}


def _validated_candidate(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_KEYS:
        raise RuntimeError(f"brain/model declaration {label} has an invalid field set")
    if not _nonempty_string(value.get("policyVersion")) or any(
        not _sha256_value(value.get(key))
        for key in ("kgVersion", "labelSnapshot", "pairwiseSnapshot", "outcomeSnapshot")
    ):
        raise RuntimeError(f"brain/model declaration {label} has invalid versions or hashes")
    return {key: value[key] for key in sorted(_CANDIDATE_KEYS)}


def _validated_brain_metadata(template: dict[str, Any], template_sha256: str) -> dict[str, Any]:
    brain = template.get("brain")
    required = _BRAIN_KEYS - {"alternateLlmScoringModel"}
    if not isinstance(brain, dict) or not required.issubset(brain):
        raise RuntimeError("template brain/model declaration is incomplete")
    string_fields = (
        "fitPolicyVersion",
        "fitPromptVersion",
        "preferenceModel",
        "outcomeModel",
        "llmScoringModel",
        "embeddingModel",
    )
    models = brain.get("qualificationModels")
    policies = brain.get("decisionPolicies")
    if (
        any(not _nonempty_string(brain.get(key)) for key in string_fields)
        or not isinstance(models, list)
        or not models
        or any(not _nonempty_string(model) for model in models)
        or len(models) != len(set(models))
        or not isinstance(policies, dict)
        or set(policies) != _POLICY_KEYS
        or policies.get("status") not in {"draft", "staging_candidate", "approved"}
        or not _positive_or_zero_integer(policies.get("count"))
        or policies.get("count") == 0
        or not _sha256_value(policies.get("rowSetSha256"))
    ):
        raise RuntimeError("template brain/model declaration contains invalid types, versions, or hashes")
    alternate = brain.get("alternateLlmScoringModel")
    if alternate is not None and not _nonempty_string(alternate):
        raise RuntimeError("template brain/model declaration alternate model is invalid")
    declaration = {key: brain[key] for key in string_fields}
    declaration["qualificationModels"] = list(models)
    if alternate is not None:
        declaration["alternateLlmScoringModel"] = alternate
    declaration["decisionPolicies"] = {
        "status": policies["status"],
        "count": policies["count"],
        "rowSetSha256": policies["rowSetSha256"],
        "atsCanaryCandidate": _validated_candidate(policies["atsCanaryCandidate"], "ATS policy"),
        "linkedinCanaryCandidate": _validated_candidate(policies["linkedinCanaryCandidate"], "LinkedIn policy"),
    }
    return {
        "declaration": declaration,
        "declarationSourceSha256": template_sha256,
        "liveActivationProven": False,
    }


def _test_receipt(
    path: Path | None,
    expected_commit: str,
    expected_tree: str,
    repository_kind: str,
    generated_at: datetime,
    attestation_key: bytes,
    attestation_key_id: str,
) -> dict[str, Any] | None:
    if path is None:
        return None
    receipt, receipt_bytes, receipt_sha256, key_id = _authenticated_receipt(
        path, attestation_key, attestation_key_id, "test receipt"
    )
    required = {
        "schemaVersion",
        "producer",
        "suiteIdentity",
        "status",
        "sourceCommitSha",
        "sourceTreeSha",
        "commands",
        "environment",
        "startedAt",
        "completedAt",
        "authentication",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("test receipt schema is incomplete or contains unknown claims")
    if receipt.get("schemaVersion") != "applypilot_test_receipt_v1":
        raise RuntimeError("test receipt schema version is unsupported")
    producer = receipt.get("producer")
    if producer not in _TRUSTED_TEST_PRODUCERS:
        raise RuntimeError("test receipt producer is not permitted by the trusted producer policy")
    if receipt.get("suiteIdentity") != _TEST_SUITES[repository_kind]:
        raise RuntimeError("test receipt suite identity does not match the repository release suite")
    if (
        receipt.get("status") != "passed"
        or receipt.get("sourceCommitSha") != expected_commit
        or receipt.get("sourceTreeSha") != expected_tree
    ):
        raise RuntimeError("test receipt does not prove the selected source commit and tree passed")
    commands = receipt.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, dict)
            or set(command) != {"command", "exitCode"}
            or not _nonempty_string(command.get("command"))
            or command.get("exitCode") != 0
            or isinstance(command.get("exitCode"), bool)
            for command in commands
        )
    ):
        raise RuntimeError("test receipt must contain one or more successful commands with exit code zero")
    environment = receipt.get("environment")
    if (
        not isinstance(environment, dict)
        or not {"runner", "os"}.issubset(environment)
        or any(not _nonempty_string(key) or not _nonempty_string(value) for key, value in environment.items())
    ):
        raise RuntimeError("test receipt environment is incomplete")
    started = _parse_timestamp(receipt.get("startedAt"), "test receipt startedAt")
    completed = _parse_timestamp(receipt.get("completedAt"), "test receipt completedAt")
    if completed < started:
        raise RuntimeError("test receipt completion timestamp precedes its start")
    if completed > generated_at or generated_at - completed > _MAX_TEST_RECEIPT_AGE:
        raise RuntimeError("test receipt is stale or was completed after manifest generation")
    return {
        "path": path.name,
        "bytes": len(receipt_bytes),
        "sha256": receipt_sha256,
        "schemaVersion": receipt["schemaVersion"],
        "producer": producer,
        "suiteIdentity": receipt["suiteIdentity"],
        "commandCount": len(commands),
        "startedAt": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "completedAt": completed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "authentication": {"algorithm": _ATTESTATION_ALGORITHM, "keyId": key_id},
    }


def _validated_repository_template(
    template: dict[str, Any], kind: str, repo: Path, selected_commit: str
) -> dict[str, Any]:
    repositories = template.get("repositories")
    repository = repositories.get(kind) if isinstance(repositories, dict) else None
    if not isinstance(repository, dict):
        raise RuntimeError(f"template repository declaration is missing for {kind}")
    if not _nonempty_string(repository.get("repository")) or not _nonempty_string(repository.get("branch")):
        raise RuntimeError(f"template repository name or branch is invalid for {kind}")
    try:
        branch_commit = _git(
            repo,
            "rev-parse",
            f"refs/heads/{repository['branch']}^{{commit}}",
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"declared release branch does not resolve for {kind}") from exc
    if branch_commit != selected_commit:
        raise RuntimeError(f"declared release branch head does not equal selected commit for {kind}")
    return repository


def _validate_deployment(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "railwayProjectId": args.railway_project_id,
        "railwayEnvironmentId": args.railway_environment_id,
        "postgresServiceId": args.postgres_service_id,
        "atsWorkerServiceId": args.ats_worker_service_id,
        "linkedinWorkerServiceId": args.linkedin_worker_service_id,
    }
    if not _nonempty_string(args.railway_project) or any(
        not _nonempty_string(value) or not _UUID_RE.fullmatch(value) for value in values.values()
    ):
        raise RuntimeError("deployment identity requires a non-empty Railway project and valid UUID IDs")
    if not _nonempty_string(args.database_name) or not _DATABASE_RE.fullmatch(args.database_name):
        raise RuntimeError("deployment identity contains an invalid PostgreSQL database name")
    if args.ats_worker_service_id == args.linkedin_worker_service_id:
        raise RuntimeError("ATS and LinkedIn worker service IDs must differ")
    return values


def _topology_receipt(
    path: Path,
    args: argparse.Namespace,
    generated_at: datetime,
    attestation_key: bytes,
    attestation_key_id: str,
) -> dict[str, Any]:
    receipt, receipt_bytes, receipt_sha256, key_id = _authenticated_receipt(
        path,
        attestation_key,
        attestation_key_id,
        "Railway topology receipt",
    )
    required = {
        "schemaVersion",
        "producer",
        "status",
        "railwayProject",
        "railwayProjectId",
        "railwayEnvironmentId",
        "postgresServiceId",
        "atsWorkerServiceId",
        "linkedinWorkerServiceId",
        "databaseName",
        "sourceCommand",
        "environment",
        "capturedAt",
        "expiresAt",
        "authentication",
    }
    if set(receipt) != required:
        raise RuntimeError("Railway topology receipt schema is incomplete or contains unknown claims")
    expected = {
        "railwayProject": args.railway_project,
        "railwayProjectId": args.railway_project_id,
        "railwayEnvironmentId": args.railway_environment_id,
        "postgresServiceId": args.postgres_service_id,
        "atsWorkerServiceId": args.ats_worker_service_id,
        "linkedinWorkerServiceId": args.linkedin_worker_service_id,
        "databaseName": args.database_name,
    }
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise RuntimeError("Railway topology receipt does not match the exact deployment identity")
    if (
        receipt.get("schemaVersion") != "applypilot_railway_topology_receipt_v1"
        or receipt.get("producer") != _TRUSTED_TOPOLOGY_PRODUCER
        or receipt.get("status") != "verified"
        or receipt.get("sourceCommand") != _TOPOLOGY_SOURCE_COMMAND
        or receipt.get("environment") != _TOPOLOGY_ENVIRONMENT
    ):
        raise RuntimeError("Railway topology receipt status, producer, command, or environment is invalid")
    captured = _parse_timestamp(receipt.get("capturedAt"), "Railway topology receipt capturedAt")
    expires = _parse_timestamp(receipt.get("expiresAt"), "Railway topology receipt expiresAt")
    if (
        expires <= captured
        or expires - captured > _MAX_TOPOLOGY_RECEIPT_LIFETIME
        or captured > generated_at
        or generated_at > expires
    ):
        raise RuntimeError("Railway topology receipt timestamps are stale, future-dated, or overlong")
    return {
        "path": path.name,
        "bytes": len(receipt_bytes),
        "sha256": receipt_sha256,
        "schemaVersion": receipt["schemaVersion"],
        "producer": receipt["producer"],
        "capturedAt": captured.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "authentication": {"algorithm": _ATTESTATION_ALGORITHM, "keyId": key_id},
    }


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    runtime_repo = args.runtime_repo.resolve()
    brain_repo = args.brain_repo.resolve()
    template_bytes, template_sha256 = _stable_read_bytes(args.template.resolve())
    try:
        template = json.loads(template_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("compatibility-manifest template is not valid UTF-8 JSON") from exc
    if template.get("schemaVersion") not in {
        "applypilot_compatibility_manifest_v2",
        "applypilot_compatibility_manifest_v3",
    }:
        raise RuntimeError("unsupported compatibility-manifest template")
    if not _nonempty_string(args.release_id):
        raise RuntimeError("release ID must be non-empty")
    generated_at = _parse_timestamp(args.generated_at, "--generated-at")
    attestation_key, attestation_key_id = _attestation_configuration()
    deployment_ids = _validate_deployment(args)

    runtime_commit, runtime_tree = _git_identity(runtime_repo, args.runtime_ref)
    brain_commit, brain_tree = _git_identity(brain_repo, args.brain_ref)
    schema_blobs = [_git_file_record(runtime_repo, runtime_commit, path) for path in _SCHEMA_PATHS]
    fleet_blobs = [_git_file_record(runtime_repo, runtime_commit, path) for path in _FLEET_PATHS]
    schema_records = [record for record, _content in schema_blobs]
    fleet_records = [record for record, _content in fleet_blobs]
    artifacts = _verify_artifacts(template, args.artifact_source_root)
    sqlite_source = _verify_sqlite_source(
        template,
        args.sqlite_source,
    )
    runtime_template = _validated_repository_template(template, "python", runtime_repo, runtime_commit)
    brain_template = _validated_repository_template(template, "typescript", brain_repo, brain_commit)
    runtime_tests = _test_receipt(
        args.runtime_test_receipt,
        runtime_commit,
        runtime_tree,
        "python",
        generated_at,
        attestation_key,
        attestation_key_id,
    )
    brain_tests = _test_receipt(
        args.brain_test_receipt,
        brain_commit,
        brain_tree,
        "typescript",
        generated_at,
        attestation_key,
        attestation_key_id,
    )
    topology_receipt = _topology_receipt(
        args.railway_topology_receipt,
        args,
        generated_at,
        attestation_key,
        attestation_key_id,
    )

    python_repo = {
        "repository": runtime_template["repository"],
        "branch": runtime_template["branch"],
        "sourceCommitSha": runtime_commit,
        "sourceTreeSha": runtime_tree,
        "releaseEvidenceCommitSha": None,
        "releaseVersion": f"{_package_version(runtime_repo, runtime_commit)}+git.tree.{runtime_tree[:7]}",
        "rollback": _validated_rollback(runtime_repo, runtime_template.get("rollback"), runtime_commit),
    }
    typescript_repo = {
        "repository": brain_template["repository"],
        "branch": brain_template["branch"],
        "sourceCommitSha": brain_commit,
        "sourceTreeSha": brain_tree,
        "releaseEvidenceCommitSha": None,
        "rollback": _validated_rollback(brain_repo, brain_template.get("rollback"), brain_commit),
    }
    if runtime_tests is not None:
        python_repo.update(testedCommitSha=runtime_commit, testedTreeSha=runtime_tree, testReceipt=runtime_tests)
    if brain_tests is not None:
        typescript_repo.update(testedCommitSha=brain_commit, testedTreeSha=brain_tree, testReceipt=brain_tests)

    schemas: dict[str, Any] = {}
    for index, record in enumerate(schema_records, start=1):
        schemas[f"brainSqlV{index}"] = record
    schemas["brainBundle"] = {
        "algorithm": "sha256-framed-path-and-content-v1",
        "versions": [1, 2, 3, 4, 5],
        "sha256": _schema_bundle(schema_blobs),
    }
    schemas["fleetPython"], schemas["fleetSql"], schemas["fleetMigrationManifest"] = fleet_records
    manifest: dict[str, Any] = {
        "schemaVersion": "applypilot_compatibility_manifest_v3",
        "releaseId": args.release_id,
        "generatedAt": generated_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "releaseState": "staging_candidate",
        "promotionAuthorized": False,
        "sourceTemplateSha256": template_sha256,
        "repositories": {"python": python_repo, "typescript": typescript_repo},
        "schemas": schemas,
        "brain": _validated_brain_metadata(template, template_sha256),
        "artifacts": artifacts,
        "canonicalSqliteSource": sqlite_source,
        "deployment": {
            "railwayProject": args.railway_project,
            **deployment_ids,
            "targetDatabaseName": args.database_name,
            "databaseNameMustMatch": True,
            "topologyReceipt": topology_receipt,
            "liveBrainSchemaVersions": [],
            "liveBrainSchemaV5Verified": False,
        },
    }
    manifest["verification"] = {
        "releaseManifestGenerator": "scripts/build-compatibility-manifest.py",
        "artifactInventoryVerified": True,
        "artifactManifestReconciled": True,
        "canonicalSqliteBytesVerified": True,
        "canonicalSqliteDirectlyVerified": True,
        "railwayTopologyReceiptVerified": True,
        "runtimeSourceTests": "passed" if runtime_tests else "pending-final-release-gate",
        "typescriptSourceTests": "passed" if brain_tests else "pending-final-release-gate",
        "realPostgresTests": "pending-final-release-gate",
    }
    manifest["guardrails"] = {
        "lanesPaused": "must-be-proven-live-after-deployment",
        "zeroFleetLeasesProvenForRelease": False,
        "principalValidationProven": False,
        "workerVersionIdentityProven": False,
        "atsCanaryEnabled": False,
        "linkedinCanaryEnabled": False,
        "productionTouched": False,
    }
    manifest["promotionBlockers"] = [
        "Commit and publish this manifest, then bind its release-evidence commit in the annotated tags.",
        "Install and verify brain schema V5 in the exact target database named by this manifest.",
        "Prove bounded principals, fresh pinned-version heartbeats, paused lanes, and zero open leases.",
        "Import and validate the pinned knowledge graph, FitMap, label, and pairwise inputs.",
        "Run and approve separate ATS and LinkedIn canaries.",
        "Verify a retention-locked production archive and restore drill.",
    ]
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--runtime-repo", type=Path, required=True)
    parser.add_argument("--brain-repo", type=Path, required=True)
    parser.add_argument("--runtime-ref", default="HEAD")
    parser.add_argument("--brain-ref", default="HEAD")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--artifact-source-root", type=Path)
    parser.add_argument("--sqlite-source", type=Path)
    parser.add_argument("--runtime-test-receipt", type=Path)
    parser.add_argument("--brain-test-receipt", type=Path)
    parser.add_argument("--railway-topology-receipt", type=Path, required=True)
    parser.add_argument("--railway-project", required=True)
    parser.add_argument("--railway-project-id", required=True)
    parser.add_argument("--railway-environment-id", required=True)
    parser.add_argument("--postgres-service-id", required=True)
    parser.add_argument("--ats-worker-service-id", required=True)
    parser.add_argument("--linkedin-worker-service-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _fsync_directory(directory_path: Path) -> None:
    try:
        descriptor = os.open(directory_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_exclusive_fsync(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink(missing_ok=True)
            finally:
                _fsync_directory(path.parent)
        raise
    _fsync_directory(path.parent)


def main() -> int:
    args = _parser().parse_args()
    output = args.output.resolve()
    if output.name.lower().endswith(".sha256"):
        raise SystemExit("manifest output cannot use the .sha256 sidecar suffix")
    sidecar = output.with_name(output.name + ".sha256")
    manifest = build_manifest(args)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    sidecar_bytes = f"{digest}  {output.name}\n".encode("ascii")
    if output.exists() and output.read_bytes() != encoded:
        raise SystemExit("refusing to overwrite conflicting immutable release manifest")
    if sidecar.exists() and sidecar.read_bytes() != sidecar_bytes:
        raise SystemExit("refusing to overwrite conflicting immutable release sidecar")
    if output.exists() and sidecar.exists():
        raise SystemExit("refusing to overwrite complete immutable release evidence")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists():
        _write_exclusive_fsync(output, encoded)
    if not sidecar.exists():
        _write_exclusive_fsync(sidecar, sidecar_bytes)
    print(json.dumps({"manifest": str(output), "sha256": digest, "sidecar": str(sidecar)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
