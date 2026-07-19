from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SIGNER = ROOT / "scripts" / "sign-release-attestation.py"
COMMON = ROOT / "scripts" / "release_evidence_common.py"
RELEASE_ID = "applypilot-test-rc1"
RELEASE_NONCE = "release_nonce_0123456789abcdef0123456789abcdef"
KEYS = {
    "runtime": b"runtime-test-attestation-key-material-32-bytes-minimum",
    "brain": b"brain-test-attestation-key-material-32-bytes-minimum",
    "railway": b"railway-topology-attestation-key-material-32-bytes",
    "nonrelease": b"nonrelease-claim-attestation-key-material-32-bytes",
}
ENV = {
    "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64": base64.b64encode(KEYS["runtime"]).decode(),
    "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_ID": "runtime-key-v2",
    "APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_B64": base64.b64encode(KEYS["brain"]).decode(),
    "APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_ID": "brain-key-v2",
    "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64": base64.b64encode(KEYS["railway"]).decode(),
    "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID": "railway-key-v2",
    "APPLYPILOT_NONRELEASE_ATTESTATION_KEY_B64": base64.b64encode(KEYS["nonrelease"]).decode(),
    "APPLYPILOT_NONRELEASE_ATTESTATION_KEY_ID": "nonrelease-key-v1",
}
UUIDS = {
    "project": "11111111-1111-4111-8111-111111111111",
    "environment": "22222222-2222-4222-8222-222222222222",
    "postgres": "33333333-3333-4333-8333-333333333333",
    "control_plane": "44444444-4444-4444-8444-444444444444",
    "gateway": "55555555-5555-4555-8555-555555555555",
}


def _approved_tool_environment() -> dict[str, str]:
    tools = {
        "APPLYPILOT_RUNTIME_TEST_PYTHON": Path(sys.executable).resolve(),
        "APPLYPILOT_RELEASE_GIT": Path(shutil.which("git") or "").resolve(),
    }
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is not None and npm is not None:
        node_path = Path(node).resolve()
        npm_launcher = Path(npm).resolve()
        npm_candidates = (
            node_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
            node_path.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
            npm_launcher,
        )
        npm_cli = next((candidate for candidate in npm_candidates if candidate.suffix == ".js" and candidate.is_file()), None)
        assert npm_cli is not None, f"could not resolve npm-cli.js from {npm_launcher}"
        tools["APPLYPILOT_BRAIN_TEST_NODE"] = node_path
        tools["APPLYPILOT_BRAIN_TEST_NPM_CLI"] = npm_cli.resolve()
        script_shell = os.environ.get("COMSPEC") if os.name == "nt" else shutil.which("sh")
        assert script_shell is not None, "could not resolve the npm script shell"
        tools["APPLYPILOT_BRAIN_TEST_SCRIPT_SHELL"] = Path(script_shell).resolve()
    approved: dict[str, str] = {}
    for prefix, path in tools.items():
        approved[f"{prefix}_PATH"] = str(path)
        approved[f"{prefix}_SHA256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return approved


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(arguments: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SIGNER), *arguments],
        capture_output=True,
        text=True,
        env={**os.environ, **ENV, **_approved_tool_environment(), **(env or {})},
    )


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def _runtime_repo(path: Path, *, failing: bool = False) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "release-test@example.invalid")
    _git(path, "config", "user.name", "Release Test")
    (path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 120\n[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    tests = path / "tests"
    tests.mkdir()
    assertion = "False" if failing else "True"
    (tests / "test_release.py").write_text(
        "import os\n\n"
        "def test_release():\n"
        f"    assert {assertion}\n"
        "    assert os.environ.get('APPLYPILOT_RELEASE_SECRET_MARKER') is None\n",
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "release source")
    return path


def _brain_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "release-test@example.invalid")
    _git(path, "config", "user.name", "Release Test")
    (path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "node -e \"if(process.env.APPLYPILOT_RELEASE_SECRET_MARKER)process.exit(17);process.stdout.write('typed')\"",
                    "test": "node -e \"if(process.env.APPLYPILOT_RELEASE_SECRET_MARKER)process.exit(17);process.stdout.write('tested')\"",
                }
            }
        ),
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "brain release source")
    return path


def test_runtime_producer_executes_exact_suite_and_binds_logs_repo_and_release(tmp_path: Path) -> None:
    repo = _runtime_repo(tmp_path / "runtime")
    output = tmp_path / "runtime-receipt.json"
    result = _run(
        [
            "produce-tests",
            "--suite",
            "runtime",
            "--repo",
            str(repo),
            "--release-id",
            RELEASE_ID,
            "--release-nonce",
            RELEASE_NONCE,
            "--output",
            str(output),
        ],
        env={"APPLYPILOT_RELEASE_SECRET_MARKER": "must-not-reach-python"},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schemaVersion"] == "applypilot_test_receipt_v4"
    assert receipt["receiptPurpose"] == "runtime-tests"
    assert receipt["releaseId"] == RELEASE_ID
    assert receipt["releaseNonce"] == RELEASE_NONCE
    assert receipt["sourceCommitSha"] == _git(repo, "rev-parse", "HEAD")
    assert receipt["sourceTreeSha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert receipt["sourceControl"]["system"] == "git"
    assert receipt["sourceControl"]["executable"]["purpose"] == "release-git"
    assert [record["command"] for record in receipt["commands"]] == [
        "python -m pytest -q",
        "python -m ruff check .",
    ]
    for record in receipt["commands"]:
        assert record["exitCode"] == 0
        assert all(len(record[name]) == 64 for name in ("stdoutSha256", "stderrSha256", "logSha256"))
        assert Path(record["executable"]["path"]).is_absolute()
        assert len(record["executable"]["sha256"]) == 64
        assert record["argv"][0] == record["executable"]["path"]
    assert len(receipt["environment"]["executionPolicySha256"]) == 64
    assert all(len(value) == 64 for value in receipt["environment"]["inheritedEnvironmentSha256"].values())
    assert receipt["authentication"]["keyId"] == ENV["APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_ID"]
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_brain_producer_executes_resolved_npm_commands_in_sanitized_environment(tmp_path: Path) -> None:
    repo = _brain_repo(tmp_path / "brain")
    output = tmp_path / "brain-receipt.json"
    result = _run(
        [
            "produce-tests", "--suite", "brain", "--repo", str(repo), "--release-id", RELEASE_ID,
            "--release-nonce", RELEASE_NONCE, "--output", str(output),
        ],
        env={
            "APPLYPILOT_RELEASE_SECRET_MARKER": "must-not-reach-npm",
            "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64": ENV[
                "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64"
            ],
        },
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert [record["command"] for record in receipt["commands"]] == ["npm run typecheck", "npm test"]
    assert all(Path(record["argv"][0]).is_absolute() for record in receipt["commands"])
    assert all(record["executable"]["path"] == record["argv"][0] for record in receipt["commands"])
    assert all(record["executable"]["purpose"] == "brain-node" for record in receipt["commands"])
    assert all(record["dependencies"]["npmCli"]["purpose"] == "brain-npm-cli" for record in receipt["commands"])
    assert all(record["argv"][1] == record["dependencies"]["npmCli"]["path"] for record in receipt["commands"])
    assert all(
        record["dependencies"]["scriptShell"]["purpose"] == "brain-script-shell"
        and record["argv"][2:4] == ["--script-shell", record["dependencies"]["scriptShell"]["path"]]
        for record in receipt["commands"]
    )
    assert all(
        record["executionEnvironment"]["PATH"] == str(Path(record["executable"]["path"]).parent)
        for record in receipt["commands"]
    )
    assert receipt["authentication"]["keyId"] == ENV["APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_ID"]


def test_brain_producer_uses_pinned_node_and_npm_payload_with_poisoned_path(tmp_path: Path) -> None:
    repo = _brain_repo(tmp_path / "brain-poisoned-path")
    poison = tmp_path / "poison"
    poison.mkdir()
    marker = tmp_path / "ambient-launcher-ran"
    if os.name == "nt":
        (poison / "node.cmd").write_text(f'@echo poisoned>{marker}\r\n@exit /b 91\r\n', encoding="utf-8")
        (poison / "npm.cmd").write_text(f'@echo poisoned>{marker}\r\n@exit /b 92\r\n', encoding="utf-8")
    else:
        for name in ("node", "npm"):
            script = poison / name
            script.write_text(f"#!/bin/sh\necho poisoned > {marker}\nexit 91\n", encoding="utf-8")
            script.chmod(0o755)
    output = tmp_path / "brain-poisoned-path.json"

    result = _run(
        [
            "produce-tests", "--suite", "brain", "--repo", str(repo), "--release-id", RELEASE_ID,
            "--release-nonce", RELEASE_NONCE, "--output", str(output),
        ],
        env={"PATH": str(poison)},
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert all(record["argv"][0] != str(poison / "node") for record in receipt["commands"])
    assert all(record["argv"][1] != str(poison / "npm") for record in receipt["commands"])


def test_executable_path_swap_cannot_yield_a_pre_swap_identity_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load_module(SIGNER, "executable_path_swap_test")
    approved_path = tmp_path / ("python.exe" if os.name == "nt" else "python")
    approved_path.write_bytes(Path(sys.executable).read_bytes())
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"attacker replacement")
    approved_sha256 = hashlib.sha256(approved_path.read_bytes()).hexdigest()
    monkeypatch.setenv("APPLYPILOT_RUNTIME_TEST_PYTHON_PATH", str(approved_path))
    monkeypatch.setenv("APPLYPILOT_RUNTIME_TEST_PYTHON_SHA256", approved_sha256)
    executable = signer._trusted_executable("runtime-python")
    swap_was_blocked = False

    def swap_during_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal swap_was_blocked
        try:
            os.replace(replacement, approved_path)
        except OSError:
            swap_was_blocked = True
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(signer.subprocess, "run", swap_during_run)
    if os.name == "nt":
        record, *_rest = signer._command_result(
            command_id="probe", command="probe", argv=[str(approved_path)], cwd=tmp_path,
            environment={}, executable=executable,
        )
        assert swap_was_blocked
        assert record["executable"]["sha256"] == hashlib.sha256(approved_path.read_bytes()).hexdigest()
    else:
        with pytest.raises(RuntimeError, match="changed during protected execution"):
            signer._command_result(
                command_id="probe", command="probe", argv=[str(approved_path)], cwd=tmp_path,
                environment={}, executable=executable,
            )


def test_npm_script_shell_uses_approved_protected_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load_module(SIGNER, "npm_script_shell_boundary_test")
    identities = {
        "brain-node": {"path": "/approved/node", "purpose": "brain-node", "sha256": "1" * 64},
        "brain-npm-cli": {
            "path": "/approved/npm-cli.js",
            "purpose": "brain-npm-cli",
            "sha256": "2" * 64,
        },
        "brain-script-shell": {
            "path": "/approved/sh",
            "purpose": "brain-script-shell",
            "sha256": "3" * 64,
        },
    }
    descriptors = {"brain-node": 11, "brain-npm-cli": 12, "brain-script-shell": 13}
    monkeypatch.setattr(signer, "_trusted_executable", identities.__getitem__)

    @contextmanager
    def protected(executable: dict[str, str]):
        descriptor = descriptors[executable["purpose"]]
        yield {"path": f"/proc/self/fd/{descriptor}", "passFds": (descriptor,)}

    captured: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(signer, "_protected_executable_execution", protected)
    monkeypatch.setattr(signer.subprocess, "run", run)
    signer._command_result(
        command_id="typecheck",
        command="npm run typecheck",
        argv=[
            identities["brain-node"]["path"],
            identities["brain-npm-cli"]["path"],
            "--script-shell",
            identities["brain-script-shell"]["path"],
            "run",
            "typecheck",
        ],
        cwd=tmp_path,
        environment={"PATH": "/approved"},
        executable=identities["brain-node"],
        dependencies={
            "npmCli": identities["brain-npm-cli"],
            "scriptShell": identities["brain-script-shell"],
        },
    )
    assert captured["argv"] == [
        "/proc/self/fd/11",
        "/proc/self/fd/12",
        "--script-shell",
        "/proc/self/fd/13",
        "run",
        "typecheck",
    ]
    assert captured["kwargs"]["pass_fds"] == (11, 12, 13)


def test_failed_suite_and_dirty_repo_fail_without_publishing(tmp_path: Path) -> None:
    failing = _runtime_repo(tmp_path / "failing", failing=True)
    failing_output = tmp_path / "failed.json"
    arguments = [
        "produce-tests",
        "--suite",
        "runtime",
        "--repo",
        str(failing),
        "--release-id",
        RELEASE_ID,
        "--release-nonce",
        RELEASE_NONCE,
        "--output",
        str(failing_output),
    ]
    failed = _run(arguments)
    assert failed.returncode != 0
    assert "required release command failed" in failed.stderr
    assert not failing_output.exists()

    clean = _runtime_repo(tmp_path / "dirty")
    (clean / "untracked.txt").write_text("dirty", encoding="utf-8")
    dirty_output = tmp_path / "dirty.json"
    arguments[arguments.index(str(failing))] = str(clean)
    arguments[arguments.index(str(failing_output))] = str(dirty_output)
    dirty = _run(arguments)
    assert dirty.returncode != 0
    assert "must be clean" in dirty.stderr
    assert not dirty_output.exists()


def test_pytest_addopts_cannot_hide_a_failing_test(tmp_path: Path) -> None:
    repo = _runtime_repo(tmp_path / "ambient-bypass", failing=True)
    (repo / "tests" / "test_good.py").write_text("def test_good():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add passing decoy")
    output = tmp_path / "ambient-bypass.json"
    result = _run(
        [
            "produce-tests", "--suite", "runtime", "--repo", str(repo), "--release-id", RELEASE_ID,
            "--release-nonce", RELEASE_NONCE, "--output", str(output),
        ],
        env={"PYTEST_ADDOPTS": "tests/test_good.py"},
    )

    assert result.returncode != 0
    assert "prohibited selection/injection variables" in result.stderr
    assert "PYTEST_ADDOPTS" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "variable",
    ["PYTEST_PLUGINS", "NODE_OPTIONS", "NODE_PATH", "TEST_FILTER", "VITEST_RELATED", "NPM_CONFIG_SCRIPT_SHELL"],
)
def test_test_selection_and_injection_environment_is_rejected(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load_module(SIGNER, f"environment_policy_{variable.lower()}")
    monkeypatch.setenv(variable, "attacker-controlled")
    assert variable in signer._rejected_test_environment()


def test_generic_claim_signing_is_nonrelease_only_and_cannot_masquerade_as_receipt(tmp_path: Path) -> None:
    generic = tmp_path / "generic.json"
    output = tmp_path / "generic-signed.json"
    generic.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    legacy = _run(["--input", str(generic), "--output", str(output)])
    assert legacy.returncode != 0
    assert not output.exists()

    rejected = _run(["sign-nonrelease", "--input", str(generic), "--output", str(output)])
    assert rejected.returncode != 0
    assert "nonRelease=true" in rejected.stderr

    generic.write_text(json.dumps({"nonRelease": True, "status": "diagnostic"}), encoding="utf-8")
    signed = _run(["sign-nonrelease", "--input", str(generic), "--output", str(output)])
    assert signed.returncode == 0, signed.stderr
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["producer"] == "applypilot-nonrelease-claim-signer-v1"
    assert document["authentication"]["keyId"] == ENV["APPLYPILOT_NONRELEASE_ATTESTATION_KEY_ID"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"nonRelease":true,"nonRelease":true}', "duplicate object key"),
        ('{"nonRelease":true,"value":NaN}', "non-standard JSON constant"),
        ('{"nonRelease":true,"value":1e999}', "non-finite JSON number"),
        ("[]", "nonRelease=true"),
    ],
)
def test_low_level_parser_is_strict(tmp_path: Path, content: str, message: str) -> None:
    source = tmp_path / "claim.json"
    output = tmp_path / "claim-signed.json"
    source.write_text(content, encoding="utf-8")
    result = _run(["sign-nonrelease", "--input", str(source), "--output", str(output)])
    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


def test_canonical_json_rejects_non_finite_values() -> None:
    common = _load_module(COMMON, "release_evidence_common_canonical_json_test")
    with pytest.raises(ValueError, match="Out of range float values"):
        common.canonical_json({"value": float("inf")})


def _observation_record(command_id: str, command: str, argv: list[str], when: datetime) -> dict[str, Any]:
    return {
        "commandId": command_id,
        "command": command,
        "argv": argv,
        "executable": {
            "path": argv[0],
            "sha256": "4" * 64,
            "purpose": "railway-cli",
            "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
        },
        "exitCode": 0,
        "stdoutSha256": "1" * 64,
        "stderrSha256": "2" * 64,
        "logSha256": "3" * 64,
        "startedAt": when.isoformat().replace("+00:00", "Z"),
        "completedAt": (when + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    }


def _mock_railway_cli(signer: ModuleType, monkeypatch: pytest.MonkeyPatch, when: datetime, tmp_path: Path) -> str:
    railway_path = str((tmp_path / "railway.exe").resolve())
    executable = {
        "path": railway_path,
        "sha256": "4" * 64,
        "purpose": "railway-cli",
        "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
    }
    monkeypatch.setattr(signer, "_trusted_executable", lambda _purpose: executable)

    def command_result(**kwargs: Any):
        record = _observation_record(kwargs["command_id"], kwargs["command"], kwargs["argv"], when)
        return record, b"railway 5.23.0\n", b"", when, when + timedelta(seconds=1)

    monkeypatch.setattr(signer, "_command_result", command_result)
    return railway_path


def _railway_status() -> dict[str, Any]:
    services = [
        {"id": UUIDS["postgres"], "name": "Postgres"},
        {"id": UUIDS["control_plane"], "name": "applypilot-control-plane"},
        {"id": UUIDS["gateway"], "name": "applypilot-tailscale-gateway"},
    ]
    instances = [
        {
            "id": f"instance-{index}", "environmentId": UUIDS["environment"],
            "serviceId": service["id"], "serviceName": service["name"],
        }
        for index, service in enumerate(services)
    ]
    return {
        "id": UUIDS["project"],
        "name": "applypilot-staging",
        "services": {"edges": [{"node": service} for service in services]},
        "environments": {
            "edges": [{"node": {
                "id": UUIDS["environment"], "name": "production",
                "serviceInstances": {"edges": [{"node": instance} for instance in instances]},
            }}]
        },
    }


def _railway_variables(service: str) -> dict[str, str]:
    identities = {
        "postgres": (UUIDS["postgres"], "Postgres", None),
        "control_plane": (UUIDS["control_plane"], "applypilot-control-plane", "control-plane"),
        "gateway": (UUIDS["gateway"], "applypilot-tailscale-gateway", "gateway"),
    }
    service_id, service_name, role = identities[service]
    variables = {
        "RAILWAY_PROJECT_ID": UUIDS["project"],
        "RAILWAY_PROJECT_NAME": "applypilot-staging",
        "RAILWAY_ENVIRONMENT_ID": UUIDS["environment"],
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_ID": service_id,
        "RAILWAY_SERVICE_NAME": service_name,
    }
    if service == "postgres":
        variables.update(PGDATABASE="applypilot_brain_staging", POSTGRES_DB="applypilot_brain_staging")
    else:
        variables["APPLYPILOT_SERVICE_ROLE"] = str(role)
    return variables


def test_railway_producer_captures_exact_commands_and_raw_output_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load_module(SIGNER, "railway_evidence_producer_test")
    monkeypatch.setattr(signer, "purpose_key", lambda purpose: (KEYS["railway"], "railway-key-v2"), raising=False)
    monkeypatch.setenv(
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64", base64.b64encode(KEYS["railway"]).decode()
    )
    monkeypatch.setenv("APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID", "railway-key-v2")
    when = datetime.now(timezone.utc)
    railway_path = _mock_railway_cli(signer, monkeypatch, when, tmp_path)
    documents = [
        _railway_status(),
        _railway_variables("postgres"),
        _railway_variables("control_plane"),
        _railway_variables("gateway"),
    ]
    for document in documents[1:]:
        document["TEST_SECRET_VALUE"] = "must-not-enter-receipt"
    calls = 0

    def observation(**kwargs: Any):
        nonlocal calls
        calls += 1
        document = documents[calls - 1]
        raw = json.dumps(document).encode()
        record = _observation_record(kwargs["command_id"], kwargs["command"], kwargs["argv"], when)
        record["stdoutSha256"] = hashlib.sha256(raw).hexdigest()
        return record, document, when, when + timedelta(seconds=1)

    monkeypatch.setattr(signer, "_railway_observation", observation)
    output = tmp_path / "railway.json"
    args = signer.argparse.Namespace(
        release_id=RELEASE_ID,
        release_nonce=RELEASE_NONCE,
        working_directory=tmp_path,
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        expected_railway_environment_name="production",
        postgres_service_id=UUIDS["postgres"],
        control_plane_service_id=UUIDS["control_plane"],
        control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"],
        gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging",
        output=output,
    )
    signer.produce_railway_evidence(args)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert "must-not-enter-receipt" not in output.read_text(encoding="utf-8")
    assert calls == 4
    assert [record["commandId"] for record in receipt["commands"]] == [
        "railway-version",
        "railway-status",
        "railway-postgres-variables",
        "railway-control-plane-variables",
        "railway-gateway-variables",
    ]
    assert all(len(record["stdoutSha256"]) == 64 for record in receipt["commands"])
    assert receipt["railwayCli"]["version"] == "railway 5.23.0"
    assert receipt["railwayCli"]["executable"]["path"] == railway_path
    assert receipt["releaseStage"] == "staging"
    assert "releaseEnvironment" not in receipt
    assert receipt["expectedRailwayEnvironmentName"] == "production"
    assert receipt["observedRailwayEnvironmentName"] == "production"
    assert receipt["executionBoundary"] == {
        "browserExecutionLocation": "fleet-nodes-only",
        "linkedinExecutionLocation": "owner-home-node-only",
        "railwayBrowserWorkersPermitted": False,
        "runtimeEnforcementProven": False,
    }
    assert receipt["serviceRoles"] == {"controlPlane": "control-plane", "gateway": "gateway"}
    assert receipt["workerBrowserContractMarkersPresent"] == []
    assert "runtime process tree" in receipt["topologyLimitation"]
    assert "atsWorkerServiceId" not in receipt
    assert "linkedinWorkerServiceId" not in receipt


def test_railway_producer_rejects_missing_observed_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = _load_module(SIGNER, "railway_missing_identity_test")
    monkeypatch.setenv(
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64", base64.b64encode(KEYS["railway"]).decode()
    )
    monkeypatch.setenv("APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID", "railway-key-v2")
    when = datetime.now(timezone.utc)
    _mock_railway_cli(signer, monkeypatch, when, tmp_path)

    def observation(**kwargs: Any):
        return _observation_record(kwargs["command_id"], kwargs["command"], kwargs["argv"], when), {}, when, when

    monkeypatch.setattr(signer, "_railway_observation", observation)
    args = signer.argparse.Namespace(
        release_id=RELEASE_ID,
        release_nonce=RELEASE_NONCE,
        working_directory=tmp_path,
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        expected_railway_environment_name="production",
        postgres_service_id=UUIDS["postgres"],
        control_plane_service_id=UUIDS["control_plane"],
        control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"],
        gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging",
        output=tmp_path / "missing.json",
    )
    with pytest.raises(RuntimeError, match="does not match|missing"):
        signer.produce_railway_evidence(args)
    assert not args.output.exists()


def test_railway_identity_in_unrelated_diagnostics_does_not_satisfy_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _load_module(SIGNER, "railway_diagnostic_identity_test")
    monkeypatch.setenv(
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64", base64.b64encode(KEYS["railway"]).decode()
    )
    monkeypatch.setenv("APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID", "railway-key-v2")
    when = datetime.now(timezone.utc)
    _mock_railway_cli(signer, monkeypatch, when, tmp_path)
    documents = [
        {"diagnostics": ["applypilot-staging", *UUIDS.values()]},
        {"diagnostics": ["applypilot_brain_staging", *UUIDS.values()]},
    ]

    def observation(**kwargs: Any):
        document = documents.pop(0)
        return _observation_record(kwargs["command_id"], kwargs["command"], kwargs["argv"], when), document, when, when

    monkeypatch.setattr(signer, "_railway_observation", observation)
    args = signer.argparse.Namespace(
        release_id=RELEASE_ID, release_nonce=RELEASE_NONCE, working_directory=tmp_path,
        railway_project="applypilot-staging", railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"], postgres_service_id=UUIDS["postgres"],
        expected_railway_environment_name="production",
        control_plane_service_id=UUIDS["control_plane"], control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"], gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging", output=tmp_path / "diagnostic.json",
    )
    with pytest.raises(RuntimeError, match="project identity"):
        signer.produce_railway_evidence(args)
    assert not args.output.exists()


def test_railway_structure_requires_linkedin_instance_and_canonical_database(tmp_path: Path) -> None:
    signer = _load_module(SIGNER, "railway_required_relationships_test")
    args = signer.argparse.Namespace(
        railway_project="applypilot-staging", railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"], postgres_service_id=UUIDS["postgres"],
        expected_railway_environment_name="production",
        control_plane_service_id=UUIDS["control_plane"], control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"], gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_clean_20260717_v2",
    )
    status = _railway_status()
    instances = status["environments"]["edges"][0]["node"]["serviceInstances"]["edges"]
    status["environments"]["edges"][0]["node"]["serviceInstances"]["edges"] = [
        edge for edge in instances if edge["node"]["serviceId"] != UUIDS["gateway"]
    ]
    with pytest.raises(RuntimeError, match="exactly the required"):
        signer._validate_railway_status(status, args)

    unexpected = _railway_status()
    unexpected["environments"]["edges"][0]["node"]["serviceInstances"]["edges"].append(
        {
            "node": {
                "id": "unexpected-instance",
                "environmentId": UUIDS["environment"],
                "serviceId": "66666666-6666-4666-8666-666666666666",
                "serviceName": "unexpected-worker",
            }
        }
    )
    with pytest.raises(RuntimeError, match="exactly the required"):
        signer._validate_railway_status(unexpected, args)

    wrong_name = _railway_status()
    for edge in wrong_name["services"]["edges"]:
        if edge["node"]["id"] == UUIDS["control_plane"]:
            edge["node"]["name"] = "unexpected-control-plane"
    for edge in wrong_name["environments"]["edges"][0]["node"]["serviceInstances"]["edges"]:
        if edge["node"]["serviceId"] == UUIDS["control_plane"]:
            edge["node"]["serviceName"] = "unexpected-control-plane"
    with pytest.raises(RuntimeError, match="exact expected name"):
        signer._validate_railway_status(wrong_name, args)

    observed = signer._validate_railway_status(_railway_status(), args)
    variables = _railway_variables("postgres")
    with pytest.raises(RuntimeError, match="PGDATABASE, POSTGRES_DB"):
        signer._validate_railway_service_variables(variables, args, observed, "postgres")


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("control_plane_service_id", UUIDS["postgres"]),
        ("gateway_service_id", UUIDS["postgres"]),
        ("gateway_service_id", UUIDS["control_plane"]),
    ],
)
def test_railway_producer_requires_pairwise_distinct_service_ids(
    field: str, duplicate: str
) -> None:
    signer = _load_module(SIGNER, f"railway_distinct_service_ids_{field}_{duplicate[0]}")
    args = signer.argparse.Namespace(
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        postgres_service_id=UUIDS["postgres"],
        expected_railway_environment_name="production",
        control_plane_service_id=UUIDS["control_plane"],
        control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"],
        gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging",
    )
    setattr(args, field, duplicate)

    with pytest.raises(RuntimeError, match="service IDs must be pairwise distinct"):
        signer._validate_railway_status(_railway_status(), args)


@pytest.mark.parametrize(
    ("service", "mutation", "message"),
    [
        ("control_plane", {"APPLYPILOT_SERVICE_ROLE": "worker"}, "service role"),
        ("gateway", {"APPLYPILOT_WORKER_CONTRACT": "apply"}, "worker/browser contract marker"),
        ("control_plane", {"PLAYWRIGHT_BROWSERS_PATH": "/browsers"}, "worker/browser contract marker"),
        ("postgres", {"APPLYPILOT_WORKER_ID": "railway-worker"}, "worker/browser contract marker"),
    ],
)
def test_railway_control_services_require_exact_roles_and_reject_browser_worker_markers(
    service: str, mutation: dict[str, str], message: str
) -> None:
    signer = _load_module(SIGNER, f"railway_role_marker_{service}_{next(iter(mutation))}")
    args = signer.argparse.Namespace(
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        expected_railway_environment_name="production",
        postgres_service_id=UUIDS["postgres"],
        control_plane_service_id=UUIDS["control_plane"],
        control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"],
        gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging",
    )
    observed = signer._validate_railway_status(_railway_status(), args)
    variables = _railway_variables(service)
    variables.update(mutation)
    with pytest.raises(RuntimeError, match=message):
        signer._validate_railway_service_variables(variables, args, observed, service)


def test_railway_environment_name_must_exactly_match_explicit_staging_policy() -> None:
    signer = _load_module(SIGNER, "railway_environment_name_policy_test")
    args = signer.argparse.Namespace(
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        expected_railway_environment_name="staging",
        postgres_service_id=UUIDS["postgres"],
        control_plane_service_id=UUIDS["control_plane"],
        control_plane_service_name="applypilot-control-plane",
        gateway_service_id=UUIDS["gateway"],
        gateway_service_name="applypilot-tailscale-gateway",
        database_name="applypilot_brain_staging",
    )
    with pytest.raises(RuntimeError, match="environment name"):
        signer._validate_railway_status(_railway_status(), args)


def test_observation_environment_strips_release_and_signing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _load_module(SIGNER, "observation_environment_secret_test")
    markers = {
        "APPLYPILOT_RELEASE_SECRET_MARKER": "release-secret",
        "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64": "signing-secret",
        "RAILWAY_TOKEN": "railway-secret",
        "GITHUB_TOKEN": "verification-secret",
        "ROLLBACK_DATABASE_URL": "rollback-secret",
    }
    for name, value in markers.items():
        monkeypatch.setenv(name, value)
    child, _hashes = signer._observation_environment()
    assert not set(markers).intersection(child)
    assert set(child).issubset(
        set(signer.TEST_ENVIRONMENT_POLICY["inheritedAllowlist"])
        | set(signer.TEST_ENVIRONMENT_POLICY["fixed"])
    )


def test_trusted_executable_hash_mismatch_fails_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _load_module(SIGNER, "trusted_executable_preflight_test")
    monkeypatch.setenv("APPLYPILOT_RUNTIME_TEST_PYTHON_PATH", str(Path(sys.executable).resolve()))
    monkeypatch.setenv("APPLYPILOT_RUNTIME_TEST_PYTHON_SHA256", "0" * 64)
    called = False

    def unexpected_run(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run for an unapproved executable")

    monkeypatch.setattr(signer.subprocess, "run", unexpected_run)
    with pytest.raises(RuntimeError, match="approved SHA-256"):
        signer._trusted_executable("runtime-python")
    assert called is False


def test_atomic_publication_never_exposes_partial_final_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    common = _load_module(COMMON, "release_evidence_common_atomic_test")
    target = tmp_path / "receipt.json"
    observed_before_link: list[bool] = []
    if os.name == "nt":
        original_windows_link = common._windows_link_relative

        def inspect_then_windows_link(descriptor: int, parent: int, destination: str) -> None:
            observed_before_link.append(target.exists())
            original_windows_link(descriptor, parent, destination)

        monkeypatch.setattr(common, "_windows_link_relative", inspect_then_windows_link)
    else:
        original_link = common.os.link

        def inspect_then_link(source: str, destination: str, **kwargs: Any) -> None:
            observed_before_link.append(target.exists())
            original_link(source, destination, **kwargs)

        monkeypatch.setattr(common.os, "link", inspect_then_link)
    common.atomic_write_no_overwrite(target, b"complete")
    assert observed_before_link == [False]
    assert target.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_publication_rejects_windows_reparse_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = _load_module(COMMON, "release_evidence_common_reparse_test")
    target = tmp_path / "receipt.json"
    monkeypatch.setattr(common, "_windows_path_has_reparse_component", lambda _path: True)
    monkeypatch.setattr(common, "_is_windows", lambda: True)
    with pytest.raises(RuntimeError, match="reparse point"):
        common.atomic_write_no_overwrite(target, b"evidence")
    assert not target.exists()


def test_publication_rejects_real_symlink_or_reparse_parent(tmp_path: Path) -> None:
    common = _load_module(COMMON, "release_evidence_common_real_reparse_test")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink/reparse creation is unavailable on this host")
    with pytest.raises(RuntimeError, match="symlink|reparse point|junction"):
        common.atomic_write_no_overwrite(alias / "receipt.json", b"evidence")
    assert not (real_parent / "receipt.json").exists()


def test_publication_fails_closed_without_a_supported_secure_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = _load_module(COMMON, "release_evidence_common_unsupported_backend_test")
    monkeypatch.setattr(common, "_is_windows", lambda: False)
    monkeypatch.setattr(common, "_POSIX_DIR_FD_SUPPORTED", False)
    with pytest.raises(RuntimeError, match="descriptor-relative release-evidence publication"):
        common.atomic_write_no_overwrite(tmp_path / "receipt.json", b"evidence")


def test_atomic_publication_cleans_temp_on_fsync_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    common = _load_module(COMMON, "release_evidence_common_fsync_test")
    target = tmp_path / "receipt.json"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(common.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated storage failure"):
        common.atomic_write_no_overwrite(target, b"partial")
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp-*"))
