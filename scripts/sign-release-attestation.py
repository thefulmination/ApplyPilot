#!/usr/bin/env python3
"""Produce authenticated ApplyPilot release evidence from executed observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from contextlib import ExitStack
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
    observation_environment,
    protected_executable_execution,
    regular_file,
    sign_receipt,
    stable_read_bytes,
    strict_json_loads,
    trusted_executable,
    validate_release_binding,
)


_TOPOLOGY_PURPOSE = "railway-topology"
_TOPOLOGY_LIFETIME = timedelta(minutes=30)
_RAILWAY_STATUS_SCHEMA = "railway-cli-5-project-status-v1"
_RAILWAY_VARIABLES_SCHEMA = "railway-cli-5-flat-service-variables-v1"
_WORKER_BROWSER_CONTRACT_MARKERS = frozenset(
    {
        "APPLYPILOT_APPLY_WORKER_DIR",
        "APPLYPILOT_BROWSER_LOCK_DIR",
        "APPLYPILOT_CHROME_SLOT",
        "APPLYPILOT_CHROME_WORKER_DIR",
        "APPLYPILOT_LINKEDIN_BROWSERS",
        "APPLYPILOT_LINKEDIN_RESOLVE_BROWSER",
        "APPLYPILOT_LINKEDIN_RESOLVE_WORKER_ID",
        "APPLYPILOT_WORKER_CONTRACT",
        "APPLYPILOT_WORKER_ID",
        "APPLY_WORKER_DIR",
        "CHROME_WORKER_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "WORKER_LABEL",
        "WORKER_SLOT",
    }
)
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


_trusted_executable = trusted_executable
_observation_environment = observation_environment
_protected_executable_execution = protected_executable_execution


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
    return _observation_environment()


def _git(
    repo: Path,
    *arguments: str,
    environment: dict[str, str],
    executable: dict[str, str],
) -> str:
    if _trusted_executable("release-git") != executable:
        raise RuntimeError("approved Git executable identity changed before observation")
    recorded_argv = [
            executable["path"],
            "-c",
            "core.autocrlf=true" if os.name == "nt" else "core.autocrlf=input",
            "-c",
            "core.filemode=false" if os.name == "nt" else "core.filemode=true",
            "-C",
            str(repo),
            *arguments,
        ]
    with _protected_executable_execution(executable) as boundary:
        actual_argv = [boundary["path"], *recorded_argv[1:]]
        subprocess_options: dict[str, Any] = {}
        if boundary["passFds"]:
            subprocess_options["pass_fds"] = boundary["passFds"]
        result = subprocess.run(
            actual_argv,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            **subprocess_options,
        )
    if _trusted_executable("release-git") != executable:
        raise RuntimeError("approved Git executable identity changed after observation")
    return result.stdout.strip()


def _assert_clean_identity(
    repo: Path,
    environment: dict[str, str],
    git: dict[str, str],
) -> tuple[str, str]:
    if not repo.is_dir():
        raise RuntimeError(f"release repository is not a directory: {repo}")
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        environment=environment,
        executable=git,
    )
    if status:
        raise RuntimeError(f"release repository must be clean before and after evidence production: {repo}")
    commit = _git(repo, "rev-parse", "HEAD^{commit}", environment=environment, executable=git)
    tree = _git(repo, "rev-parse", "HEAD^{tree}", environment=environment, executable=git)
    return commit, tree


def _command_result(
    *,
    command_id: str,
    command: str,
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    executable: dict[str, str],
    dependencies: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, Any], bytes, bytes, datetime, datetime]:
    if _trusted_executable(executable["purpose"]) != executable:
        raise RuntimeError(f"approved {executable['purpose']} executable identity changed before observation")
    dependencies = dependencies or {}
    with ExitStack() as stack:
        boundaries = {
            executable["path"]: stack.enter_context(_protected_executable_execution(executable))
        }
        for dependency in dependencies.values():
            boundaries[dependency["path"]] = stack.enter_context(
                _protected_executable_execution(dependency)
            )
        actual_argv = [boundaries.get(value, {"path": value})["path"] for value in argv]
        pass_fds = tuple(
            descriptor
            for boundary in boundaries.values()
            for descriptor in boundary["passFds"]
        )
        subprocess_options: dict[str, Any] = {}
        if pass_fds:
            subprocess_options["pass_fds"] = pass_fds
        started = _utc_now()
        result = subprocess.run(
            actual_argv,
            cwd=cwd,
            capture_output=True,
            check=False,
            env=environment,
            **subprocess_options,
        )
        completed = _utc_now()
    if _trusted_executable(executable["purpose"]) != executable or any(
        _trusted_executable(dependency["purpose"]) != dependency
        for dependency in dependencies.values()
    ):
        raise RuntimeError("approved executable identity changed after observation")
    record = {
        "commandId": command_id,
        "command": command,
        "argv": argv,
        "executable": executable,
        "exitCode": result.returncode,
        "stdoutSha256": _sha256(result.stdout),
        "stderrSha256": _sha256(result.stderr),
        "logSha256": _framed_log_digest(result.stdout, result.stderr, result.returncode),
        "startedAt": _timestamp(started),
        "completedAt": _timestamp(completed),
    }
    if dependencies:
        record["dependencies"] = dependencies
        record["executionEnvironment"] = {"PATH": environment.get("PATH", "")}
    return record, result.stdout, result.stderr, started, completed


def _publish(path: Path, receipt: dict[str, Any], purpose: str) -> None:
    encoded = (
        json.dumps(
            sign_receipt(receipt, purpose),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()
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
    git = _trusted_executable("release-git")
    commit_before, tree_before = _assert_clean_identity(repo, environment, git)
    policy_hash = _sha256(canonical_json(policy))
    execution_policy_hash = _sha256(canonical_json(TEST_ENVIRONMENT_POLICY))
    records: list[dict[str, Any]] = []
    first_started: datetime | None = None
    last_completed: datetime | None = None
    for command_id, command, arguments in policy["commands"]:
        if arguments[0] == "npm":
            executable = _trusted_executable("brain-node")
            npm_cli = _trusted_executable("brain-npm-cli")
            argv = [executable["path"], npm_cli["path"]]
            command_environment = {**environment, "PATH": str(Path(executable["path"]).parent)}
            script_shell = _trusted_executable("brain-script-shell")
            argv.extend(("--script-shell", script_shell["path"]))
            dependencies = {"npmCli": npm_cli, "scriptShell": script_shell}
            argv.extend(arguments[1:])
        else:
            executable = _trusted_executable("runtime-python")
            if os.path.normcase(executable["path"]) != os.path.normcase(str(Path(sys.executable).resolve())):
                raise RuntimeError("runtime-python approved path must equal the current Python interpreter")
            argv = [executable["path"], *arguments]
            command_environment = environment
            dependencies = None
        record, _stdout, _stderr, started, completed = _command_result(
            command_id=command_id,
            command=command,
            argv=argv,
            cwd=repo,
            environment=command_environment,
            executable=executable,
            dependencies=dependencies,
        )
        records.append(record)
        first_started = first_started or started
        last_completed = completed
        if record["exitCode"] != 0:
            raise RuntimeError(f"required release command failed: {command}")
    commit_after, tree_after = _assert_clean_identity(repo, environment, git)
    if _trusted_executable("release-git") != git:
        raise RuntimeError("approved Git executable identity changed during evidence production")
    if (commit_before, tree_before) != (commit_after, tree_after):
        raise RuntimeError("release repository identity changed during evidence production")
    assert first_started is not None and last_completed is not None
    receipt = {
        "schemaVersion": "applypilot_test_receipt_v4",
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
        "sourceControl": {"system": "git", "executable": git},
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
        args.control_plane_service_id,
        args.gateway_service_id,
    }
    if len(expected_service_ids) != 3:
        raise RuntimeError("PostgreSQL, control-plane, and gateway service IDs must be pairwise distinct")
    if not expected_service_ids.issubset(services):
        raise RuntimeError("Railway status target services are missing from the project service relationship")
    environments = _unique_by_id(_edges(document.get("environments"), "environments"), "environments")
    target = environments.get(args.railway_environment_id)
    if target is None:
        raise RuntimeError("Railway status target environment is missing from the project")
    environment_name = target.get("name")
    if not isinstance(environment_name, str) or not environment_name:
        raise RuntimeError("Railway status target environment name is missing")
    if environment_name != args.expected_railway_environment_name:
        raise RuntimeError(
            "Railway status target environment name does not match the explicitly expected staging environment name"
        )
    instances = _edges(target.get("serviceInstances"), "target environment serviceInstances")
    by_service: dict[str, dict[str, Any]] = {}
    for instance in instances:
        service_id = instance.get("serviceId")
        if instance.get("environmentId") != args.railway_environment_id:
            raise RuntimeError("Railway service instance is bound to the wrong environment")
        if not isinstance(service_id, str) or not service_id or service_id in by_service:
            raise RuntimeError("Railway target environment has a missing or duplicate service instance")
        by_service[service_id] = instance
    if set(by_service) != expected_service_ids:
        raise RuntimeError(
            "Railway target environment must contain exactly the required Postgres, control-plane, and gateway "
            "service instances"
        )
    for service_id in expected_service_ids:
        project_name = services[service_id].get("name")
        instance_name = by_service[service_id].get("serviceName")
        if not isinstance(project_name, str) or not project_name or instance_name != project_name:
            raise RuntimeError("Railway project service and target-environment instance names disagree")
    expected_names = {
        args.control_plane_service_id: args.control_plane_service_name,
        args.gateway_service_id: args.gateway_service_name,
    }
    for service_id, expected_name in expected_names.items():
        if services[service_id].get("name") != expected_name:
            raise RuntimeError("Railway control-plane or gateway service name does not match the exact expected name")
    return {
        "environmentName": environment_name,
        "postgresServiceName": str(services[args.postgres_service_id]["name"]),
        "controlPlaneServiceName": str(services[args.control_plane_service_id]["name"]),
        "gatewayServiceName": str(services[args.gateway_service_id]["name"]),
    }


def _validate_railway_service_variables(
    document: Any,
    args: argparse.Namespace,
    observed: dict[str, str],
    service: str,
) -> None:
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in document.items()
    ):
        raise RuntimeError(f"Railway variables do not match the supported {_RAILWAY_VARIABLES_SCHEMA} shape")
    service_policy = {
        "postgres": (
            args.postgres_service_id,
            observed["postgresServiceName"],
            None,
        ),
        "control_plane": (
            args.control_plane_service_id,
            observed["controlPlaneServiceName"],
            "control-plane",
        ),
        "gateway": (
            args.gateway_service_id,
            observed["gatewayServiceName"],
            "gateway",
        ),
    }
    try:
        service_id, service_name, role = service_policy[service]
    except KeyError as exc:
        raise RuntimeError(f"unsupported Railway service observation: {service}") from exc
    expected = {
        "RAILWAY_PROJECT_ID": args.railway_project_id,
        "RAILWAY_PROJECT_NAME": args.railway_project,
        "RAILWAY_ENVIRONMENT_ID": args.railway_environment_id,
        "RAILWAY_ENVIRONMENT_NAME": args.expected_railway_environment_name,
        "RAILWAY_SERVICE_ID": service_id,
        "RAILWAY_SERVICE_NAME": service_name,
    }
    prohibited = sorted(_WORKER_BROWSER_CONTRACT_MARKERS.intersection(document))
    if prohibited:
        raise RuntimeError(
            f"Railway {service_name} exposes prohibited worker/browser contract markers: "
            + ", ".join(prohibited)
        )
    if service == "postgres":
        expected.update(PGDATABASE=args.database_name, POSTGRES_DB=args.database_name)
    else:
        expected["APPLYPILOT_SERVICE_ROLE"] = str(role)
    mismatches = sorted(name for name, value in expected.items() if document.get(name) != value)
    if mismatches:
        if "APPLYPILOT_SERVICE_ROLE" in mismatches:
            raise RuntimeError(f"Railway {service_name} service role does not match the exact expected role")
        raise RuntimeError(
            f"Railway {service_name} variables do not match required structural metadata: "
            + ", ".join(mismatches)
        )


def _railway_observation(
    *,
    command_id: str,
    command: str,
    argv: list[str],
    cwd: Path,
    executable: dict[str, str],
    environment: dict[str, str],
) -> tuple[dict[str, Any], Any, datetime, datetime]:
    record, stdout, _stderr, started, completed = _command_result(
        command_id=command_id,
        command=command,
        argv=argv,
        cwd=cwd,
        executable=executable,
        environment=environment,
    )
    if record["exitCode"] != 0:
        raise RuntimeError(f"required Railway observation failed: {command}")
    document = strict_json_loads(stdout, f"{command} output")
    return record, document, started, completed


def produce_railway_evidence(args: argparse.Namespace) -> None:
    release_id, release_nonce = validate_release_binding(args.release_id, args.release_nonce)
    cwd = args.working_directory.resolve(strict=True)
    environment, _inherited_environment_hashes = _observation_environment()
    railway = _trusted_executable("railway-cli")
    version_record, version_stdout, _stderr, captured_at, _ = _command_result(
        command_id="railway-version",
        command="railway --version",
        argv=[railway["path"], "--version"],
        cwd=cwd,
        executable=railway,
        environment=environment,
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
        environment=environment,
    )
    observed = _validate_railway_status(status_document, args)
    postgres_variables_command = f"railway variables --service {args.postgres_service_id} --json"
    postgres_variables_record, postgres_variables_document, _, _ = _railway_observation(
        command_id="railway-postgres-variables",
        command=postgres_variables_command,
        argv=[railway["path"], "variables", "--service", args.postgres_service_id, "--json"],
        cwd=cwd,
        executable=railway,
        environment=environment,
    )
    _validate_railway_service_variables(postgres_variables_document, args, observed, "postgres")
    control_variables_command = f"railway variables --service {args.control_plane_service_id} --json"
    control_variables_record, control_variables_document, _, _ = _railway_observation(
        command_id="railway-control-plane-variables",
        command=control_variables_command,
        argv=[railway["path"], "variables", "--service", args.control_plane_service_id, "--json"],
        cwd=cwd,
        executable=railway,
        environment=environment,
    )
    _validate_railway_service_variables(control_variables_document, args, observed, "control_plane")
    gateway_variables_command = f"railway variables --service {args.gateway_service_id} --json"
    gateway_variables_record, gateway_variables_document, _, completed = _railway_observation(
        command_id="railway-gateway-variables",
        command=gateway_variables_command,
        argv=[railway["path"], "variables", "--service", args.gateway_service_id, "--json"],
        cwd=cwd,
        executable=railway,
        environment=environment,
    )
    _validate_railway_service_variables(gateway_variables_document, args, observed, "gateway")
    receipt = {
        "schemaVersion": "applypilot_railway_topology_receipt_v4",
        "receiptPurpose": _TOPOLOGY_PURPOSE,
        "producer": PRODUCER,
        "producerVersion": PRODUCER_VERSION,
        "releaseId": release_id,
        "releaseNonce": release_nonce,
        "status": "verified",
        "railwayProject": args.railway_project,
        "railwayProjectId": args.railway_project_id,
        "railwayEnvironmentId": args.railway_environment_id,
        "expectedRailwayEnvironmentName": args.expected_railway_environment_name,
        "observedRailwayEnvironmentName": observed["environmentName"],
        "postgresServiceId": args.postgres_service_id,
        "controlPlaneServiceId": args.control_plane_service_id,
        "controlPlaneServiceName": args.control_plane_service_name,
        "gatewayServiceId": args.gateway_service_id,
        "gatewayServiceName": args.gateway_service_name,
        "serviceRoles": {"controlPlane": "control-plane", "gateway": "gateway"},
        "workerBrowserContractMarkersPresent": [],
        "databaseName": args.database_name,
        "commands": [
            version_record,
            status_record,
            postgres_variables_record,
            control_variables_record,
            gateway_variables_record,
        ],
        "railwayCli": {
            "version": version,
            "executable": railway,
            "statusSchema": _RAILWAY_STATUS_SCHEMA,
            "variablesSchema": _RAILWAY_VARIABLES_SCHEMA,
        },
        "releaseStage": "staging",
        "executionBoundary": _EXECUTION_BOUNDARY,
        "topologyLimitation": _TOPOLOGY_LIMITATION,
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
    railway.add_argument("--expected-railway-environment-name", required=True)
    railway.add_argument("--postgres-service-id", required=True)
    railway.add_argument("--control-plane-service-id", required=True)
    railway.add_argument("--control-plane-service-name", required=True)
    railway.add_argument("--gateway-service-id", required=True)
    railway.add_argument("--gateway-service-name", required=True)
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
