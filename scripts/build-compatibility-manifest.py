#!/usr/bin/env python3
"""Build a new immutable cross-repository ApplyPilot compatibility manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote


_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from release_evidence_common import (  # noqa: E402
    ALGORITHM,
    EXECUTABLE_TRUST_POLICY,
    PRODUCER,
    PRODUCER_VERSION,
    TEST_ENVIRONMENT_POLICY,
    TEST_SUITE_POLICIES,
    assert_separated_keys,
    atomic_write_no_overwrite,
    canonical_json,
    observation_environment,
    protected_executable_execution,
    purpose_key,
    regular_file,
    remove_published_file,
    strict_json_loads,
    trusted_executable,
    validate_release_binding,
    verify_receipt,
)


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
_TEST_SUITES = {
    "python": TEST_SUITE_POLICIES["runtime"],
    "typescript": TEST_SUITE_POLICIES["brain"],
}
_MAX_TEST_RECEIPT_AGE = timedelta(hours=24)
_MAX_TOPOLOGY_RECEIPT_LIFETIME = timedelta(hours=1)
_MAX_CLOCK_SKEW = timedelta(minutes=2)
_RELEASE_STAGE = "staging"
_EXECUTION_BOUNDARY = {
    "browserExecutionLocation": "fleet-nodes-only",
    "linkedinExecutionLocation": "owner-home-node-only",
    "railwayBrowserWorkersPermitted": False,
    "runtimeEnforcementProven": False,
}
_TOPOLOGY_LIMITATION = (
    "Railway CLI status and variables prove configured service identity and role markers, "
    "not the runtime process tree or fleet-node routing enforcement."
)
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
_trusted_executable = trusted_executable
_observation_environment = observation_environment
_protected_executable_execution = protected_executable_execution


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    git = _trusted_executable("release-git")
    environment, _hashes = _observation_environment()
    with _protected_executable_execution(git) as boundary:
        options: dict[str, Any] = {}
        if boundary["passFds"]:
            options["pass_fds"] = boundary["passFds"]
        result = subprocess.run(
            [boundary["path"], "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            **options,
        )
    if _trusted_executable("release-git") != git:
        raise RuntimeError("approved Git executable identity changed after observation")
    return result.stdout.strip()


def _git_bytes(repo: Path, commit: str, relative_path: str) -> bytes:
    git = _trusted_executable("release-git")
    environment, _hashes = _observation_environment()
    with _protected_executable_execution(git) as boundary:
        options: dict[str, Any] = {}
        if boundary["passFds"]:
            options["pass_fds"] = boundary["passFds"]
        result = subprocess.run(
            [boundary["path"], "-C", str(repo), "show", f"{commit}:{relative_path}"],
            check=True,
            capture_output=True,
            env=environment,
            **options,
        )
    if _trusted_executable("release-git") != git:
        raise RuntimeError("approved Git executable identity changed after observation")
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


def _authenticated_receipt(
    path: Path,
    key: bytes,
    expected_key_id: str,
    label: str,
) -> tuple[dict[str, Any], bytes, str, str]:
    receipt_bytes, receipt_sha256 = _stable_read_bytes(path)
    receipt = strict_json_loads(receipt_bytes, label)
    if not isinstance(receipt, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    key_id = verify_receipt(receipt, key=key, expected_key_id=expected_key_id, label=label)
    return receipt, receipt_bytes, receipt_sha256, key_id


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
    document = strict_json_loads(content, "artifact manifest")
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
    unresolved_path = Path(os.path.abspath(source_value))
    try:
        path = regular_file(unresolved_path, "canonical SQLite source")
    except RuntimeError as exc:
        raise RuntimeError(
            f"canonical SQLite source is missing, is a symlink, or contains a reparse point: "
            f"{unresolved_path}"
        ) from exc
    sidecars = [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm")]
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
        raise RuntimeError("canonical SQLite source has live WAL/SHM sidecars; close and checkpoint it first")
    if not _positive_or_zero_integer(source.get("bytes")) or not _sha256_value(source.get("sha256")):
        raise RuntimeError("canonical SQLite source bytes/hash declaration is invalid")
    before = path.stat()
    actual_bytes, actual_sha256 = _stable_file_record(path)
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
        raise RuntimeError("canonical SQLite source gained WAL/SHM sidecars while being verified")
    if actual_bytes != source["bytes"] or actual_sha256 != source["sha256"]:
        raise RuntimeError(
            f"canonical SQLite source drift: {path}; expected bytes/hash "
            f"{source['bytes']}/{source['sha256']}, got {actual_bytes}/{actual_sha256}"
        )
    snapshot_bytes: int
    snapshot_sha256: str
    snapshot_identity: tuple[int, int, int, int]
    source_uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    try:
        with tempfile.TemporaryDirectory(prefix="applypilot-sqlite-snapshot-") as temporary_directory:
            snapshot = Path(temporary_directory) / "canonical.db"
            with closing(sqlite3.connect(source_uri, uri=True)) as source_connection, closing(
                sqlite3.connect(snapshot)
            ) as destination:
                source_connection.execute("PRAGMA query_only = ON")
                source_pragmas = {
                    name: source_connection.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in ("schema_version", "page_count", "page_size", "user_version")
                }

                def reject_sidecar_during_backup(_status: int, _remaining: int, _total: int) -> None:
                    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
                        raise RuntimeError(
                            "canonical SQLite source gained WAL/SHM sidecars while creating a controlled backup"
                        )

                source_connection.backup(destination, pages=64, progress=reject_sidecar_during_backup)
            if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
                raise RuntimeError("canonical SQLite source gained WAL/SHM sidecars while creating a controlled backup")
            snapshot_before = snapshot.stat()
            snapshot_bytes, snapshot_sha256 = _stable_file_record(snapshot)
            snapshot_after = snapshot.stat()
            if _file_identity(snapshot_before) != _file_identity(snapshot_after):
                raise RuntimeError("controlled SQLite backup changed while hashing")
            snapshot_identity = _file_identity(snapshot_after)
            snapshot_uri = f"file:{quote(snapshot.as_posix(), safe='/:')}?mode=ro&immutable=1"
            with closing(sqlite3.connect(snapshot_uri, uri=True)) as connection:
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
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
        raise RuntimeError("canonical SQLite source gained WAL/SHM sidecars while being verified")
    after_bytes, after_sha256 = _stable_file_record(path)
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
        raise RuntimeError("canonical SQLite source gained WAL/SHM sidecars during final source verification")
    after = path.stat()
    if _file_identity(before) != _file_identity(after) or (after_bytes, after_sha256) != (actual_bytes, actual_sha256):
        raise RuntimeError(f"release input changed while inspecting SQLite source: {path}")
    return {
        "logicalSource": "canonical-sqlite-authority-snapshot",
        "bytes": actual_bytes,
        "sha256": actual_sha256,
        "snapshotBytes": snapshot_bytes,
        "snapshotSha256": snapshot_sha256,
        "snapshotIdentity": {
            "device": snapshot_identity[0],
            "inode": snapshot_identity[1],
            "bytes": snapshot_identity[2],
            "modifiedNs": snapshot_identity[3],
        },
        "quickCheck": "ok",
        "schemaVersion": source_pragmas["schema_version"],
        "pageCount": source_pragmas["page_count"],
        "pageSize": source_pragmas["page_size"],
        "userVersion": source_pragmas["user_version"],
        "snapshotSchemaVersion": pragmas["schema_version"],
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
    git = _trusted_executable("release-git")
    with _protected_executable_execution(git) as boundary:
        options: dict[str, Any] = {}
        if boundary["passFds"]:
            options["pass_fds"] = boundary["passFds"]
        reachability = subprocess.run(
            [boundary["path"], "-C", str(repo), "merge-base", "--is-ancestor", commit_sha, branch_commit],
            capture_output=True,
            text=True,
            env=_observation_environment()[0],
            **options,
        )
    if _trusted_executable("release-git") != git:
        raise RuntimeError("approved Git executable identity changed after observation")
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
    release_id: str,
    release_nonce: str,
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
        "receiptPurpose",
        "producer",
        "producerVersion",
        "releaseId",
        "releaseNonce",
        "suiteIdentity",
        "suitePolicySha256",
        "status",
        "sourceCommitSha",
        "sourceTreeSha",
        "sourceControl",
        "commands",
        "environment",
        "startedAt",
        "completedAt",
        "authentication",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("test receipt schema is incomplete or contains unknown claims")
    if receipt.get("schemaVersion") != "applypilot_test_receipt_v4":
        raise RuntimeError("test receipt schema version is unsupported")
    policy = _TEST_SUITES[repository_kind]
    if receipt.get("producer") != PRODUCER or receipt.get("producerVersion") != PRODUCER_VERSION:
        raise RuntimeError("test receipt producer is not permitted by the trusted producer policy")
    if (
        receipt.get("receiptPurpose") != policy["purpose"]
        or receipt.get("releaseId") != release_id
        or receipt.get("releaseNonce") != release_nonce
    ):
        raise RuntimeError("test receipt is not bound to this release purpose, ID, and nonce")
    if receipt.get("suiteIdentity") != policy["suiteIdentity"]:
        raise RuntimeError("test receipt suite identity does not match the repository release suite")
    if receipt.get("suitePolicySha256") != hashlib.sha256(canonical_json(policy)).hexdigest():
        raise RuntimeError("test receipt suite policy hash does not match the exact approved command set")
    if (
        receipt.get("status") != "passed"
        or receipt.get("sourceCommitSha") != expected_commit
        or receipt.get("sourceTreeSha") != expected_tree
    ):
        raise RuntimeError("test receipt does not prove the selected source commit and tree passed")
    commands = receipt.get("commands")
    environment = receipt.get("environment")
    source_control = receipt.get("sourceControl")
    if (
        not isinstance(environment, dict)
        or set(environment)
        != {
            "platform",
            "pythonExecutable",
            "pythonVersion",
            "executionPolicySha256",
            "inheritedEnvironmentSha256",
        }
        or any(
            not _nonempty_string(environment.get(name))
            for name in ("platform", "pythonExecutable", "pythonVersion")
        )
        or environment.get("executionPolicySha256")
        != hashlib.sha256(canonical_json(TEST_ENVIRONMENT_POLICY)).hexdigest()
    ):
        raise RuntimeError("test receipt environment is incomplete")
    inherited_hashes = environment.get("inheritedEnvironmentSha256")
    if (
        not isinstance(inherited_hashes, dict)
        or not set(inherited_hashes).issubset(TEST_ENVIRONMENT_POLICY["inheritedAllowlist"])
        or any(not _sha256_value(value) for value in inherited_hashes.values())
    ):
        raise RuntimeError("test receipt inherited environment evidence is invalid")
    source_control_executable = source_control.get("executable") if isinstance(source_control, dict) else None
    if (
        not isinstance(source_control, dict)
        or set(source_control) != {"system", "executable"}
        or source_control.get("system") != "git"
        or not isinstance(source_control_executable, dict)
        or set(source_control_executable) != {"path", "sha256", "purpose", "trustPolicy"}
        or not _nonempty_string(source_control_executable.get("path"))
        or not Path(source_control_executable["path"]).is_absolute()
        or not _sha256_value(source_control_executable.get("sha256"))
        or source_control_executable.get("purpose") != "release-git"
        or source_control_executable.get("trustPolicy") != EXECUTABLE_TRUST_POLICY
    ):
        raise RuntimeError("test receipt source-control executable identity is invalid")
    if not isinstance(commands, list) or len(commands) != len(policy["commands"]):
        raise RuntimeError("test receipt does not contain the exact approved command set")
    command_keys = {
        "commandId",
        "command",
        "argv",
        "exitCode",
        "stdoutSha256",
        "stderrSha256",
        "logSha256",
        "startedAt",
        "completedAt",
        "executable",
    }
    command_intervals: list[tuple[datetime, datetime]] = []
    for record, (command_id, command, arguments) in zip(commands, policy["commands"], strict=True):
        executable = record.get("executable") if isinstance(record, dict) else None
        is_brain = policy["purpose"] == "brain-tests"
        expected_keys = command_keys | ({"dependencies", "executionEnvironment"} if is_brain else set())
        dependencies = record.get("dependencies") if isinstance(record, dict) else None
        npm_cli = dependencies.get("npmCli") if isinstance(dependencies, dict) else None
        expected_argv = (
            [executable.get("path"), npm_cli.get("path"), *arguments[1:]]
            if is_brain and isinstance(executable, dict) and isinstance(npm_cli, dict)
            else [executable.get("path"), *arguments]
            if isinstance(executable, dict)
            else []
        )
        if (
            not isinstance(record, dict)
            or set(record) != expected_keys
            or record.get("commandId") != command_id
            or record.get("command") != command
            or not isinstance(executable, dict)
            or set(executable) != {"path", "sha256", "purpose", "trustPolicy"}
            or not _nonempty_string(executable.get("path"))
            or not Path(executable["path"]).is_absolute()
            or not _sha256_value(executable.get("sha256"))
            or executable.get("purpose")
            != ("brain-node" if is_brain else "runtime-python")
            or executable.get("trustPolicy") != EXECUTABLE_TRUST_POLICY
            or record.get("argv") != expected_argv
            or (
                is_brain
                and (
                    not isinstance(dependencies, dict)
                    or set(dependencies) != {"npmCli"}
                    or not isinstance(npm_cli, dict)
                    or set(npm_cli) != {"path", "sha256", "purpose", "trustPolicy"}
                    or not _nonempty_string(npm_cli.get("path"))
                    or not Path(npm_cli["path"]).is_absolute()
                    or not _sha256_value(npm_cli.get("sha256"))
                    or npm_cli.get("purpose") != "brain-npm-cli"
                    or npm_cli.get("trustPolicy") != EXECUTABLE_TRUST_POLICY
                    or record.get("executionEnvironment")
                    != {"PATH": str(Path(executable["path"]).parent)}
                )
            )
            or (
                policy["purpose"] == "runtime-tests"
                and executable["path"] != environment["pythonExecutable"]
            )
            or record.get("exitCode") != 0
            or any(not _sha256_value(record.get(field)) for field in ("stdoutSha256", "stderrSha256", "logSha256"))
        ):
            raise RuntimeError("test receipt does not contain the exact successful approved command set and log digests")
        command_started = _parse_timestamp(record.get("startedAt"), "test command startedAt")
        command_completed = _parse_timestamp(record.get("completedAt"), "test command completedAt")
        if command_completed < command_started:
            raise RuntimeError("test command completion timestamp precedes its start")
        command_intervals.append((command_started, command_completed))
    started = _parse_timestamp(receipt.get("startedAt"), "test receipt startedAt")
    completed = _parse_timestamp(receipt.get("completedAt"), "test receipt completedAt")
    if completed < started:
        raise RuntimeError("test receipt completion timestamp precedes its start")
    if (
        started != command_intervals[0][0]
        or completed != command_intervals[-1][1]
        or any(current[0] < previous[1] for previous, current in zip(command_intervals, command_intervals[1:]))
    ):
        raise RuntimeError("test receipt timestamps do not exactly contain the ordered approved commands")
    if completed > generated_at + _MAX_CLOCK_SKEW or generated_at - completed > _MAX_TEST_RECEIPT_AGE:
        raise RuntimeError("test receipt is stale or was completed after manifest generation")
    return {
        "path": path.name,
        "bytes": len(receipt_bytes),
        "sha256": receipt_sha256,
        "schemaVersion": receipt["schemaVersion"],
        "producer": PRODUCER,
        "producerVersion": PRODUCER_VERSION,
        "suiteIdentity": receipt["suiteIdentity"],
        "suitePolicySha256": receipt["suitePolicySha256"],
        "commandCount": len(commands),
        "startedAt": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "completedAt": completed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "authentication": {"algorithm": ALGORITHM, "keyId": key_id},
        "sourceControl": source_control,
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
        "controlPlaneServiceId": args.control_plane_service_id,
        "gatewayServiceId": args.gateway_service_id,
    }
    if (
        not _nonempty_string(args.railway_project)
        or not _nonempty_string(args.expected_railway_environment_name)
        or any(
        not _nonempty_string(value) or not _UUID_RE.fullmatch(value) for value in values.values()
        )
    ):
        raise RuntimeError("deployment identity requires a non-empty Railway project and valid UUID IDs")
    if not _nonempty_string(args.database_name) or not _DATABASE_RE.fullmatch(args.database_name):
        raise RuntimeError("deployment identity contains an invalid PostgreSQL database name")
    if not _nonempty_string(args.control_plane_service_name) or not _nonempty_string(
        args.gateway_service_name
    ):
        raise RuntimeError("deployment identity requires exact control-plane and gateway service names")
    service_ids = {
        args.postgres_service_id,
        args.control_plane_service_id,
        args.gateway_service_id,
    }
    if len(service_ids) != 3:
        raise RuntimeError("PostgreSQL, control-plane, and gateway service IDs must be pairwise distinct")
    return values


def _topology_receipt(
    path: Path,
    args: argparse.Namespace,
    release_id: str,
    release_nonce: str,
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
        "receiptPurpose",
        "producer",
        "producerVersion",
        "releaseId",
        "releaseNonce",
        "status",
        "railwayProject",
        "railwayProjectId",
        "railwayEnvironmentId",
        "expectedRailwayEnvironmentName",
        "observedRailwayEnvironmentName",
        "postgresServiceId",
        "controlPlaneServiceId",
        "controlPlaneServiceName",
        "gatewayServiceId",
        "gatewayServiceName",
        "serviceRoles",
        "workerBrowserContractMarkersPresent",
        "databaseName",
        "commands",
        "railwayCli",
        "releaseStage",
        "executionBoundary",
        "topologyLimitation",
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
        "expectedRailwayEnvironmentName": args.expected_railway_environment_name,
        "postgresServiceId": args.postgres_service_id,
        "controlPlaneServiceId": args.control_plane_service_id,
        "controlPlaneServiceName": args.control_plane_service_name,
        "gatewayServiceId": args.gateway_service_id,
        "gatewayServiceName": args.gateway_service_name,
        "databaseName": args.database_name,
    }
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise RuntimeError("Railway topology receipt does not match the exact deployment identity")
    if (
        receipt.get("schemaVersion") != "applypilot_railway_topology_receipt_v4"
        or receipt.get("receiptPurpose") != "railway-topology"
        or receipt.get("producer") != PRODUCER
        or receipt.get("producerVersion") != PRODUCER_VERSION
        or receipt.get("releaseId") != release_id
        or receipt.get("releaseNonce") != release_nonce
        or receipt.get("status") != "verified"
        or receipt.get("releaseStage") != _RELEASE_STAGE
        or receipt.get("observedRailwayEnvironmentName")
        != receipt.get("expectedRailwayEnvironmentName")
        or receipt.get("executionBoundary") != _EXECUTION_BOUNDARY
        or receipt.get("serviceRoles") != {"controlPlane": "control-plane", "gateway": "gateway"}
        or receipt.get("workerBrowserContractMarkersPresent") != []
        or receipt.get("topologyLimitation") != _TOPOLOGY_LIMITATION
    ):
        raise RuntimeError("Railway topology receipt release binding, status, producer, or environment is invalid")
    commands = receipt.get("commands")
    railway_cli = receipt.get("railwayCli")
    if (
        not isinstance(railway_cli, dict)
        or set(railway_cli) != {"version", "executable", "statusSchema", "variablesSchema"}
        or railway_cli.get("version") != "railway 5.23.0"
        or railway_cli.get("statusSchema") != "railway-cli-5-project-status-v1"
        or railway_cli.get("variablesSchema") != "railway-cli-5-flat-service-variables-v1"
        or not isinstance(railway_cli.get("executable"), dict)
        or set(railway_cli["executable"]) != {"path", "sha256", "purpose", "trustPolicy"}
        or not _nonempty_string(railway_cli["executable"].get("path"))
        or not Path(railway_cli["executable"]["path"]).is_absolute()
        or not _sha256_value(railway_cli["executable"].get("sha256"))
        or railway_cli["executable"].get("purpose") != "railway-cli"
        or railway_cli["executable"].get("trustPolicy") != EXECUTABLE_TRUST_POLICY
    ):
        raise RuntimeError("Railway topology receipt CLI or output-schema identity is invalid")
    railway_path = railway_cli["executable"]["path"]
    expected_commands = (
        ("railway-version", "railway --version", [railway_path, "--version"]),
        ("railway-status", "railway status --json", [railway_path, "status", "--json"]),
        (
            "railway-postgres-variables",
            f"railway variables --service {args.postgres_service_id} --json",
            [railway_path, "variables", "--service", args.postgres_service_id, "--json"],
        ),
        (
            "railway-control-plane-variables",
            f"railway variables --service {args.control_plane_service_id} --json",
            [railway_path, "variables", "--service", args.control_plane_service_id, "--json"],
        ),
        (
            "railway-gateway-variables",
            f"railway variables --service {args.gateway_service_id} --json",
            [railway_path, "variables", "--service", args.gateway_service_id, "--json"],
        ),
    )
    command_keys = {
        "commandId",
        "command",
        "argv",
        "exitCode",
        "stdoutSha256",
        "stderrSha256",
        "logSha256",
        "startedAt",
        "completedAt",
        "executable",
    }
    if not isinstance(commands, list) or len(commands) != len(expected_commands):
        raise RuntimeError("Railway topology receipt does not contain the exact observation commands")
    command_intervals: list[tuple[datetime, datetime]] = []
    for record, (command_id, command, argv) in zip(commands, expected_commands, strict=True):
        if (
            not isinstance(record, dict)
            or set(record) != command_keys
            or record.get("commandId") != command_id
            or record.get("command") != command
            or record.get("argv") != argv
            or record.get("executable") != railway_cli["executable"]
            or record.get("exitCode") != 0
            or any(not _sha256_value(record.get(field)) for field in ("stdoutSha256", "stderrSha256", "logSha256"))
        ):
            raise RuntimeError("Railway topology receipt command or raw output/log digest is invalid")
        command_started = _parse_timestamp(record.get("startedAt"), "Railway command startedAt")
        command_completed = _parse_timestamp(record.get("completedAt"), "Railway command completedAt")
        if command_completed < command_started:
            raise RuntimeError("Railway command completion timestamp precedes its start")
        command_intervals.append((command_started, command_completed))
    captured = _parse_timestamp(receipt.get("capturedAt"), "Railway topology receipt capturedAt")
    expires = _parse_timestamp(receipt.get("expiresAt"), "Railway topology receipt expiresAt")
    if (
        expires <= captured
        or expires - captured > _MAX_TOPOLOGY_RECEIPT_LIFETIME
        or captured != command_intervals[0][0]
        or command_intervals[-1][1] > expires
        or any(current[0] < previous[1] for previous, current in zip(command_intervals, command_intervals[1:]))
        or captured > generated_at + _MAX_CLOCK_SKEW
        or generated_at > expires + _MAX_CLOCK_SKEW
    ):
        raise RuntimeError("Railway topology receipt timestamps are stale, future-dated, or overlong")
    return {
        "path": path.name,
        "bytes": len(receipt_bytes),
        "sha256": receipt_sha256,
        "schemaVersion": receipt["schemaVersion"],
        "producer": receipt["producer"],
        "producerVersion": receipt["producerVersion"],
        "commandOutputSha256": {record["commandId"]: record["stdoutSha256"] for record in commands},
        "capturedAt": captured.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "authentication": {"algorithm": ALGORITHM, "keyId": key_id},
        "releaseStage": receipt["releaseStage"],
        "expectedRailwayEnvironmentName": receipt["expectedRailwayEnvironmentName"],
        "observedRailwayEnvironmentName": receipt["observedRailwayEnvironmentName"],
        "controlPlaneServiceName": receipt["controlPlaneServiceName"],
        "gatewayServiceName": receipt["gatewayServiceName"],
        "serviceRoles": receipt["serviceRoles"],
        "workerBrowserContractMarkersPresent": receipt["workerBrowserContractMarkersPresent"],
        "executionBoundary": receipt["executionBoundary"],
        "topologyLimitation": receipt["topologyLimitation"],
    }


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    runtime_repo = args.runtime_repo.resolve()
    brain_repo = args.brain_repo.resolve()
    template_bytes, template_sha256 = _stable_read_bytes(args.template.resolve())
    template = strict_json_loads(template_bytes, "compatibility-manifest template")
    if not isinstance(template, dict):
        raise RuntimeError("compatibility-manifest template must be a JSON object")
    if template.get("schemaVersion") not in {
        "applypilot_compatibility_manifest_v2",
        "applypilot_compatibility_manifest_v3",
    }:
        raise RuntimeError("unsupported compatibility-manifest template")
    release_id, release_nonce = validate_release_binding(args.release_id, args.release_nonce)
    generated_at = _utc_now()
    attestations = {
        purpose: purpose_key(purpose)
        for purpose in ("runtime-tests", "brain-tests", "railway-topology")
    }
    assert_separated_keys(attestations)
    deployment_ids = _validate_deployment(args)
    git_executable = _trusted_executable("release-git")

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
        release_id,
        release_nonce,
        generated_at,
        *attestations["runtime-tests"],
    )
    brain_tests = _test_receipt(
        args.brain_test_receipt,
        brain_commit,
        brain_tree,
        "typescript",
        release_id,
        release_nonce,
        generated_at,
        *attestations["brain-tests"],
    )
    topology_receipt = _topology_receipt(
        args.railway_topology_receipt,
        args,
        release_id,
        release_nonce,
        generated_at,
        *attestations["railway-topology"],
    )
    if _trusted_executable("release-git") != git_executable:
        raise RuntimeError("approved Git executable identity changed during manifest generation")

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
        "schemaVersion": "applypilot_compatibility_manifest_v4",
        "releaseId": release_id,
        "releaseNonce": release_nonce,
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
            "controlPlaneServiceName": args.control_plane_service_name,
            "gatewayServiceName": args.gateway_service_name,
            "serviceRoles": topology_receipt["serviceRoles"],
            "workerBrowserContractMarkersPresent": topology_receipt[
                "workerBrowserContractMarkersPresent"
            ],
            "releaseStage": topology_receipt["releaseStage"],
            "expectedRailwayEnvironmentName": topology_receipt["expectedRailwayEnvironmentName"],
            "observedRailwayEnvironmentName": topology_receipt["observedRailwayEnvironmentName"],
            "executionBoundary": topology_receipt["executionBoundary"],
            "targetDatabaseName": args.database_name,
            "databaseNameMustMatch": True,
            "topologyReceipt": topology_receipt,
            "liveBrainSchemaVersions": [],
            "liveBrainSchemaV5Verified": False,
            "topologyLimitations": [
                topology_receipt["topologyLimitation"]
            ],
        },
    }
    manifest["verification"] = {
        "releaseManifestGenerator": "scripts/build-compatibility-manifest.py",
        "gitExecutable": git_executable,
        "artifactInventoryVerified": True,
        "artifactManifestReconciled": True,
        "canonicalSqliteBytesVerified": True,
        "canonicalSqliteDirectlyVerified": True,
        "railwayTopologyReceiptVerified": True,
        "railwayBrowserWorkerMarkersAbsent": True,
        "browserExecutionRuntimeBoundaryProven": False,
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
        "browserExecutionMustRemainOnFleetNodes": True,
        "linkedinMustRemainOwnerHomeNodeOnly": True,
    }
    manifest["security"] = {
        "nonceUniquenessVerified": False,
        "nonceReplayDefense": "external-durable-registry-required",
        "residualRisk": (
            "This repository has no durable release-evidence nonce registry or anchor. "
            "The release orchestrator must atomically register and reject reused release ID/nonce pairs."
        ),
    }
    manifest["promotionBlockers"] = [
        "Atomically register this release ID/nonce in an external durable release-evidence registry before promotion.",
        "Prove from runtime process and routing evidence that Railway launches no browser workers and LinkedIn remains owner-node only.",
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
    parser.add_argument("--release-nonce", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--artifact-source-root", type=Path)
    parser.add_argument("--sqlite-source", type=Path)
    parser.add_argument("--runtime-test-receipt", type=Path)
    parser.add_argument("--brain-test-receipt", type=Path)
    parser.add_argument("--railway-topology-receipt", type=Path, required=True)
    parser.add_argument("--railway-project", required=True)
    parser.add_argument("--railway-project-id", required=True)
    parser.add_argument("--railway-environment-id", required=True)
    parser.add_argument("--expected-railway-environment-name", required=True)
    parser.add_argument("--postgres-service-id", required=True)
    parser.add_argument("--control-plane-service-id", required=True)
    parser.add_argument("--control-plane-service-name", required=True)
    parser.add_argument("--gateway-service-id", required=True)
    parser.add_argument("--gateway-service-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _write_exclusive_fsync(path: Path, content: bytes) -> None:
    atomic_write_no_overwrite(path, content)


def _assert_topology_receipt_fresh_for_publication(
    manifest: dict[str, Any], publication_time: datetime
) -> None:
    try:
        expires_value = manifest["deployment"]["topologyReceipt"]["expiresAt"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("manifest topology receipt expiry is missing at final publication") from exc
    expires = _parse_timestamp(expires_value, "manifest topology receipt expiresAt")
    if publication_time >= expires:
        raise RuntimeError("Railway topology receipt expired before final publication")


def main() -> int:
    args = _parser().parse_args()
    output = Path(os.path.abspath(args.output))
    if output.name.lower().endswith(".sha256"):
        raise SystemExit("manifest output cannot use the .sha256 sidecar suffix")
    sidecar = output.with_name(output.name + ".sha256")
    manifest = build_manifest(args)
    encoded = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    sidecar_bytes = f"{digest}  {output.name}\n".encode("ascii")
    _assert_topology_receipt_fresh_for_publication(manifest, _utc_now())
    try:
        _write_exclusive_fsync(output, encoded)
    except FileExistsError as exc:
        raise SystemExit(f"refusing to overwrite release evidence: {output}") from exc
    try:
        _write_exclusive_fsync(sidecar, sidecar_bytes)
    except BaseException as exc:
        try:
            remove_published_file(output)
        except BaseException as cleanup_error:
            raise RuntimeError(
                f"sidecar publication failed and descriptor-relative output cleanup also failed: {output}"
            ) from cleanup_error
        if isinstance(exc, FileExistsError):
            raise SystemExit(f"refusing to overwrite release evidence: {sidecar}") from exc
        raise
    print(
        json.dumps(
            {"manifest": str(output), "sha256": digest, "sidecar": str(sidecar)},
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
