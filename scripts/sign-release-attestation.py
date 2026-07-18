#!/usr/bin/env python3
"""Produce authenticated ApplyPilot release evidence from executed observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from release_evidence_common import (  # noqa: E402
    NON_RELEASE_PRODUCER,
    PRODUCER,
    PRODUCER_VERSION,
    TEST_ENVIRONMENT_POLICY,
    TEST_SUITE_POLICIES,
    atomic_write_no_overwrite,
    canonical_json,
    regular_file,
    sign_receipt,
    stable_read_bytes,
    strict_json_loads,
    validate_release_binding,
)


_TOPOLOGY_PURPOSE = "railway-topology"
_TOPOLOGY_LIFETIME = timedelta(minutes=30)
_RAILWAY_STATUS_SCHEMA = "railway-cli-5-project-status-v1"
_RAILWAY_VARIABLES_SCHEMA = "railway-cli-5-flat-service-variables-v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _framed_log_digest(stdout: bytes, stderr: bytes, exit_code: int) -> str:
    digest = hashlib.sha256()
    for label, content in ((b"stdout", stdout), (b"stderr", stderr)):
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    digest.update(exit_code.to_bytes(8, "big", signed=True))
    return digest.hexdigest()


def _stable_file_sha256(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"command executable changed while hashing: {path}")
    return digest.hexdigest()


def _resolved_executable(command: str) -> dict[str, str]:
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"required command executable is not available: {command}")
    path = Path(resolved).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required command executable is not a regular file: {path}")
    return {"path": str(path), "sha256": _stable_file_sha256(path)}


def _rejected_test_environment() -> list[str]:
    exact = set(TEST_ENVIRONMENT_POLICY["rejectedExact"])
    prefixes = tuple(TEST_ENVIRONMENT_POLICY["rejectedPrefixes"])
    rejected = []
    for name in os.environ:
        normalized = name.upper()
        if normalized in exact or normalized.startswith(prefixes):
            rejected.append(name)
    return sorted(rejected, key=str.upper)


def _test_environment() -> tuple[dict[str, str], dict[str, str]]:
    rejected = _rejected_test_environment()
    if rejected:
        raise RuntimeError(
            "release test environment contains prohibited selection/injection variables: " + ", ".join(rejected)
        )
    ambient = {name.upper(): value for name, value in os.environ.items()}
    inherited = {
        name: ambient[name]
        for name in TEST_ENVIRONMENT_POLICY["inheritedAllowlist"]
        if name in ambient
    }
    if "PATH" in inherited:
        path_entries: list[str] = []
        seen: set[str] = set()
        for entry in inherited["PATH"].split(os.pathsep):
            if not entry or not Path(entry).is_absolute():
                continue
            normalized = os.path.normcase(os.path.normpath(entry))
            if normalized not in seen:
                seen.add(normalized)
                path_entries.append(entry)
        inherited["PATH"] = os.pathsep.join(path_entries)
    fixed = {
        name: os.devnull if value == "<OS_DEVNULL>" else value
        for name, value in TEST_ENVIRONMENT_POLICY["fixed"].items()
    }
    child = {**inherited, **fixed}
    value_hashes = {name: _sha256(value.encode("utf-8")) for name, value in sorted(inherited.items())}
    return child, value_hashes


def _git(repo: Path, *arguments: str, environment: dict[str, str]) -> str:
    git = _resolved_executable("git")
    result = subprocess.run(
        [
            git["path"],
            "-c",
            "core.autocrlf=true" if os.name == "nt" else "core.autocrlf=input",
            "-c",
            "core.filemode=false" if os.name == "nt" else "core.filemode=true",
            "-C",
            str(repo),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _assert_clean_identity(repo: Path, environment: dict[str, str]) -> tuple[str, str]:
    if not repo.is_dir():
        raise RuntimeError(f"release repository is not a directory: {repo}")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", environment=environment)
    if status:
        raise RuntimeError(f"release repository must be clean before and after evidence production: {repo}")
    commit = _git(repo, "rev-parse", "HEAD^{commit}", environment=environment)
    tree = _git(repo, "rev-parse", "HEAD^{tree}", environment=environment)
    return commit, tree


def _command_result(
    *,
    command_id: str,
    command: str,
    argv: list[str],
    cwd: Path,
    environment: dict[str, str] | None = None,
    executable: dict[str, str] | None = None,
) -> tuple[dict[str, Any], bytes, bytes, datetime, datetime]:
    started = _utc_now()
    result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False, env=environment)
    completed = _utc_now()
    record = {
        "commandId": command_id,
        "command": command,
        "argv": argv,
        "executable": executable or _resolved_executable(argv[0]),
        "exitCode": result.returncode,
        "stdoutSha256": _sha256(result.stdout),
        "stderrSha256": _sha256(result.stderr),
        "logSha256": _framed_log_digest(result.stdout, result.stderr, result.returncode),
        "startedAt": _timestamp(started),
        "completedAt": _timestamp(completed),
    }
    return record, result.stdout, result.stderr, started, completed


def _publish(path: Path, receipt: dict[str, Any], purpose: str) -> None:
    encoded = (json.dumps(sign_receipt(receipt, purpose), indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    try:
        atomic_write_no_overwrite(path, encoded)
    except FileExistsError as exc:
        raise RuntimeError(f"output already exists; refusing to overwrite: {path}") from exc


def produce_test_evidence(
    *,
    suite: str,
    repo: Path,
    release_id: str,
    release_nonce: str,
    output: Path,
) -> None:
    release_id, release_nonce = validate_release_binding(release_id, release_nonce)
    policy = TEST_SUITE_POLICIES[suite]
    repo = repo.resolve(strict=True)
    environment, inherited_environment_hashes = _test_environment()
    commit_before, tree_before = _assert_clean_identity(repo, environment)
    policy_hash = _sha256(canonical_json(policy))
    execution_policy_hash = _sha256(canonical_json(TEST_ENVIRONMENT_POLICY))
    records: list[dict[str, Any]] = []
    first_started: datetime | None = None
    last_completed: datetime | None = None
    for command_id, command, arguments in policy["commands"]:
        if arguments[0] == "npm":
            executable = _resolved_executable("npm")
            argv = [executable["path"], *arguments[1:]]
        else:
            executable = _resolved_executable(sys.executable)
            argv = [executable["path"], *arguments]
        record, _stdout, _stderr, started, completed = _command_result(
            command_id=command_id,
            command=command,
            argv=argv,
            cwd=repo,
            environment=environment,
            executable=executable,
        )
        records.append(record)
        first_started = first_started or started
        last_completed = completed
        if record["exitCode"] != 0:
            raise RuntimeError(f"required release command failed: {command}")
    commit_after, tree_after = _assert_clean_identity(repo, environment)
    if (commit_before, tree_before) != (commit_after, tree_after):
        raise RuntimeError("release repository identity changed during evidence production")
    assert first_started is not None and last_completed is not None
    receipt = {
        "schemaVersion": "applypilot_test_receipt_v2",
        "receiptPurpose": policy["purpose"],
        "producer": PRODUCER,
        "producerVersion": PRODUCER_VERSION,
        "releaseId": release_id,
        "releaseNonce": release_nonce,
        "suiteIdentity": policy["suiteIdentity"],
        "suitePolicySha256": policy_hash,
        "status": "passed",
        "sourceCommitSha": commit_after,
        "sourceTreeSha": tree_after,
        "commands": records,
        "environment": {
            "platform": platform.platform(),
            "pythonExecutable": str(Path(sys.executable).resolve()),
            "pythonVersion": platform.python_version(),
            "executionPolicySha256": execution_policy_hash,
            "inheritedEnvironmentSha256": inherited_environment_hashes,
        },
        "startedAt": _timestamp(first_started),
        "completedAt": _timestamp(last_completed),
    }
    _publish(output, receipt, policy["purpose"])


def _edges(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"edges"} or not isinstance(value.get("edges"), list):
        raise RuntimeError(f"Railway {label} does not match the supported {_RAILWAY_STATUS_SCHEMA} shape")
    nodes: list[dict[str, Any]] = []
    for edge in value["edges"]:
        if not isinstance(edge, dict) or set(edge) != {"node"} or not isinstance(edge.get("node"), dict):
            raise RuntimeError(f"Railway {label} contains an unknown or ambiguous edge shape")
        nodes.append(edge["node"])
    return nodes


def _unique_by_id(nodes: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        identifier = node.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise RuntimeError(f"Railway {label} contains a missing or duplicate id")
        result[identifier] = node
    return result


def _validate_railway_status(document: Any, args: argparse.Namespace) -> dict[str, str]:
    if not isinstance(document, dict):
        raise RuntimeError(f"Railway status does not match the supported {_RAILWAY_STATUS_SCHEMA} shape")
    if document.get("id") != args.railway_project_id or document.get("name") != args.railway_project:
        raise RuntimeError("Railway status project identity does not match the requested project")
    services = _unique_by_id(_edges(document.get("services"), "services"), "services")
    expected_service_ids = {
        args.postgres_service_id,
        args.ats_worker_service_id,
        args.linkedin_worker_service_id,
    }
    if not expected_service_ids.issubset(services):
        raise RuntimeError("Railway status target services are missing from the project service relationship")
    environments = _unique_by_id(_edges(document.get("environments"), "environments"), "environments")
    target = environments.get(args.railway_environment_id)
    if target is None:
        raise RuntimeError("Railway status target environment is missing from the project")
    environment_name = target.get("name")
    if not isinstance(environment_name, str) or not environment_name:
        raise RuntimeError("Railway status target environment name is missing")
    instances = _edges(target.get("serviceInstances"), "target environment serviceInstances")
    by_service: dict[str, dict[str, Any]] = {}
    for instance in instances:
        service_id = instance.get("serviceId")
        if instance.get("environmentId") != args.railway_environment_id:
            raise RuntimeError("Railway service instance is bound to the wrong environment")
        if not isinstance(service_id, str) or not service_id or service_id in by_service:
            raise RuntimeError("Railway target environment has a missing or duplicate service instance")
        by_service[service_id] = instance
    if not expected_service_ids.issubset(by_service):
        raise RuntimeError("Railway target environment is missing a required Postgres, ATS, or LinkedIn service instance")
    for service_id in expected_service_ids:
        project_name = services[service_id].get("name")
        instance_name = by_service[service_id].get("serviceName")
        if not isinstance(project_name, str) or not project_name or instance_name != project_name:
            raise RuntimeError("Railway project service and target-environment instance names disagree")
    return {
        "environmentName": environment_name,
        "postgresServiceName": str(services[args.postgres_service_id]["name"]),
    }


def _validate_railway_variables(document: Any, args: argparse.Namespace, observed: dict[str, str]) -> None:
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in document.items()
    ):
        raise RuntimeError(f"Railway variables do not match the supported {_RAILWAY_VARIABLES_SCHEMA} shape")
    expected = {
        "RAILWAY_PROJECT_ID": args.railway_project_id,
        "RAILWAY_PROJECT_NAME": args.railway_project,
        "RAILWAY_ENVIRONMENT_ID": args.railway_environment_id,
        "RAILWAY_ENVIRONMENT_NAME": observed["environmentName"],
        "RAILWAY_SERVICE_ID": args.postgres_service_id,
        "RAILWAY_SERVICE_NAME": observed["postgresServiceName"],
        "PGDATABASE": args.database_name,
        "POSTGRES_DB": args.database_name,
    }
    mismatches = sorted(name for name, value in expected.items() if document.get(name) != value)
    if mismatches:
        raise RuntimeError("Railway Postgres variables do not match required structural metadata: " + ", ".join(mismatches))


def _railway_observation(
    *, command_id: str, command: str, argv: list[str], cwd: Path, executable: dict[str, str]
) -> tuple[dict[str, Any], Any, datetime, datetime]:
    record, stdout, _stderr, started, completed = _command_result(
        command_id=command_id, command=command, argv=argv, cwd=cwd, executable=executable
    )
    if record["exitCode"] != 0:
        raise RuntimeError(f"required Railway observation failed: {command}")
    document = strict_json_loads(stdout, f"{command} output")
    return record, document, started, completed


def produce_railway_evidence(args: argparse.Namespace) -> None:
    release_id, release_nonce = validate_release_binding(args.release_id, args.release_nonce)
    cwd = args.working_directory.resolve(strict=True)
    railway = _resolved_executable("railway")
    version_record, version_stdout, _stderr, captured_at, _ = _command_result(
        command_id="railway-version",
        command="railway --version",
        argv=[railway["path"], "--version"],
        cwd=cwd,
        executable=railway,
    )
    if version_record["exitCode"] != 0:
        raise RuntimeError("required Railway version observation failed")
    version = version_stdout.decode("utf-8", errors="strict").strip()
    if version != "railway 5.23.0":
        raise RuntimeError("unsupported Railway CLI version for structural release evidence")
    status_record, status_document, _, _ = _railway_observation(
        command_id="railway-status",
        command="railway status --json",
        argv=[railway["path"], "status", "--json"],
        cwd=cwd,
        executable=railway,
    )
    variables_command = f"railway variables --service {args.postgres_service_id} --json"
    variables_record, variables_document, _, completed = _railway_observation(
        command_id="railway-postgres-variables",
        command=variables_command,
        argv=[railway["path"], "variables", "--service", args.postgres_service_id, "--json"],
        cwd=cwd,
        executable=railway,
    )
    observed = _validate_railway_status(status_document, args)
    _validate_railway_variables(variables_document, args, observed)
    receipt = {
        "schemaVersion": "applypilot_railway_topology_receipt_v2",
        "receiptPurpose": _TOPOLOGY_PURPOSE,
        "producer": PRODUCER,
        "producerVersion": PRODUCER_VERSION,
        "releaseId": release_id,
        "releaseNonce": release_nonce,
        "status": "verified",
        "railwayProject": args.railway_project,
        "railwayProjectId": args.railway_project_id,
        "railwayEnvironmentId": args.railway_environment_id,
        "postgresServiceId": args.postgres_service_id,
        "atsWorkerServiceId": args.ats_worker_service_id,
        "linkedinWorkerServiceId": args.linkedin_worker_service_id,
        "databaseName": args.database_name,
        "commands": [version_record, status_record, variables_record],
        "railwayCli": {
            "version": version,
            "executable": railway,
            "statusSchema": _RAILWAY_STATUS_SCHEMA,
            "variablesSchema": _RAILWAY_VARIABLES_SCHEMA,
        },
        "environment": "staging",
        "capturedAt": _timestamp(captured_at),
        "expiresAt": _timestamp(completed + _TOPOLOGY_LIFETIME),
    }
    _publish(args.output, receipt, _TOPOLOGY_PURPOSE)


def sign_nonrelease_claim(input_path: Path, output_path: Path) -> None:
    source = regular_file(input_path.resolve(strict=True), "non-release input")
    document = strict_json_loads(stable_read_bytes(source, "non-release input"), "non-release input")
    if not isinstance(document, dict) or document.get("nonRelease") is not True:
        raise RuntimeError("low-level signing is restricted to documents explicitly marked nonRelease=true")
    if "authentication" in document:
        raise RuntimeError("non-release input already contains authentication")
    document = {**document, "producer": NON_RELEASE_PRODUCER}
    _publish(output_path, document, "nonrelease-claims")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    tests = subparsers.add_parser("produce-tests", help="execute an exact approved release test suite")
    tests.add_argument("--suite", choices=sorted(TEST_SUITE_POLICIES), required=True)
    tests.add_argument("--repo", type=Path, required=True)
    tests.add_argument("--release-id", required=True)
    tests.add_argument("--release-nonce", required=True)
    tests.add_argument("--output", type=Path, required=True)

    railway = subparsers.add_parser("produce-railway", help="execute and capture Railway topology observations")
    railway.add_argument("--release-id", required=True)
    railway.add_argument("--release-nonce", required=True)
    railway.add_argument("--working-directory", type=Path, default=Path.cwd())
    railway.add_argument("--railway-project", required=True)
    railway.add_argument("--railway-project-id", required=True)
    railway.add_argument("--railway-environment-id", required=True)
    railway.add_argument("--postgres-service-id", required=True)
    railway.add_argument("--ats-worker-service-id", required=True)
    railway.add_argument("--linkedin-worker-service-id", required=True)
    railway.add_argument("--database-name", required=True)
    railway.add_argument("--output", type=Path, required=True)

    low_level = subparsers.add_parser("sign-nonrelease", help="authenticate a non-release diagnostic claim")
    low_level.add_argument("--input", type=Path, required=True)
    low_level.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.mode == "produce-tests":
            produce_test_evidence(
                suite=args.suite,
                repo=args.repo,
                release_id=args.release_id,
                release_nonce=args.release_nonce,
                output=args.output,
            )
        elif args.mode == "produce-railway":
            produce_railway_evidence(args)
        else:
            sign_nonrelease_claim(args.input, args.output)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"error: {exc}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
