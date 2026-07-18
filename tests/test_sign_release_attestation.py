from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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
    "ats": "44444444-4444-4444-8444-444444444444",
    "linkedin": "55555555-5555-4555-8555-555555555555",
}


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
        env={**os.environ, **ENV, **(env or {})},
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
    (tests / "test_release.py").write_text(f"def test_release():\n    assert {assertion}\n", encoding="utf-8")
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
                    "typecheck": "node -e \"process.stdout.write('typed')\"",
                    "test": "node -e \"process.stdout.write('tested')\"",
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
        ]
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schemaVersion"] == "applypilot_test_receipt_v2"
    assert receipt["receiptPurpose"] == "runtime-tests"
    assert receipt["releaseId"] == RELEASE_ID
    assert receipt["releaseNonce"] == RELEASE_NONCE
    assert receipt["sourceCommitSha"] == _git(repo, "rev-parse", "HEAD")
    assert receipt["sourceTreeSha"] == _git(repo, "rev-parse", "HEAD^{tree}")
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
        ]
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert [record["command"] for record in receipt["commands"]] == ["npm run typecheck", "npm test"]
    assert all(Path(record["argv"][0]).is_absolute() for record in receipt["commands"])
    assert all(record["executable"]["path"] == record["argv"][0] for record in receipt["commands"])
    assert receipt["authentication"]["keyId"] == ENV["APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_ID"]


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


def _observation_record(command_id: str, command: str, argv: list[str], when: datetime) -> dict[str, Any]:
    return {
        "commandId": command_id,
        "command": command,
        "argv": argv,
        "executable": {"path": argv[0], "sha256": "4" * 64},
        "exitCode": 0,
        "stdoutSha256": "1" * 64,
        "stderrSha256": "2" * 64,
        "logSha256": "3" * 64,
        "startedAt": when.isoformat().replace("+00:00", "Z"),
        "completedAt": (when + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    }


def _mock_railway_cli(signer: ModuleType, monkeypatch: pytest.MonkeyPatch, when: datetime, tmp_path: Path) -> str:
    railway_path = str((tmp_path / "railway.exe").resolve())
    executable = {"path": railway_path, "sha256": "4" * 64}
    monkeypatch.setattr(signer, "_resolved_executable", lambda _command: executable)

    def command_result(**kwargs: Any):
        record = _observation_record(kwargs["command_id"], kwargs["command"], kwargs["argv"], when)
        return record, b"railway 5.23.0\n", b"", when, when + timedelta(seconds=1)

    monkeypatch.setattr(signer, "_command_result", command_result)
    return railway_path


def _railway_status() -> dict[str, Any]:
    services = [
        {"id": UUIDS["postgres"], "name": "Postgres"},
        {"id": UUIDS["ats"], "name": "fleet-worker"},
        {"id": UUIDS["linkedin"], "name": "linkedin-worker"},
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


def _railway_variables() -> dict[str, str]:
    return {
        "RAILWAY_PROJECT_ID": UUIDS["project"],
        "RAILWAY_PROJECT_NAME": "applypilot-staging",
        "RAILWAY_ENVIRONMENT_ID": UUIDS["environment"],
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_ID": UUIDS["postgres"],
        "RAILWAY_SERVICE_NAME": "Postgres",
        "PGDATABASE": "applypilot_brain_staging",
        "POSTGRES_DB": "applypilot_brain_staging",
    }


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
    status = _railway_status()
    variables = _railway_variables()
    calls = 0

    def observation(**kwargs: Any):
        nonlocal calls
        calls += 1
        document = status if calls == 1 else variables
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
        postgres_service_id=UUIDS["postgres"],
        ats_worker_service_id=UUIDS["ats"],
        linkedin_worker_service_id=UUIDS["linkedin"],
        database_name="applypilot_brain_staging",
        output=output,
    )
    signer.produce_railway_evidence(args)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert calls == 2
    assert [record["commandId"] for record in receipt["commands"]] == [
        "railway-version",
        "railway-status",
        "railway-postgres-variables",
    ]
    assert all(len(record["stdoutSha256"]) == 64 for record in receipt["commands"])
    assert receipt["railwayCli"]["version"] == "railway 5.23.0"
    assert receipt["railwayCli"]["executable"]["path"] == railway_path


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
        postgres_service_id=UUIDS["postgres"],
        ats_worker_service_id=UUIDS["ats"],
        linkedin_worker_service_id=UUIDS["linkedin"],
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
        ats_worker_service_id=UUIDS["ats"], linkedin_worker_service_id=UUIDS["linkedin"],
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
        ats_worker_service_id=UUIDS["ats"], linkedin_worker_service_id=UUIDS["linkedin"],
        database_name="applypilot_brain_clean_20260717_v2",
    )
    status = _railway_status()
    instances = status["environments"]["edges"][0]["node"]["serviceInstances"]["edges"]
    status["environments"]["edges"][0]["node"]["serviceInstances"]["edges"] = [
        edge for edge in instances if edge["node"]["serviceId"] != UUIDS["linkedin"]
    ]
    with pytest.raises(RuntimeError, match="missing a required Postgres, ATS, or LinkedIn"):
        signer._validate_railway_status(status, args)

    observed = signer._validate_railway_status(_railway_status(), args)
    variables = _railway_variables()
    with pytest.raises(RuntimeError, match="PGDATABASE, POSTGRES_DB"):
        signer._validate_railway_variables(variables, args, observed)


def test_atomic_publication_never_exposes_partial_final_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    common = _load_module(COMMON, "release_evidence_common_atomic_test")
    target = tmp_path / "receipt.json"
    original_link = common.os.link
    observed_before_link: list[bool] = []

    def inspect_then_link(source: Path, destination: Path) -> None:
        observed_before_link.append(destination.exists())
        original_link(source, destination)

    monkeypatch.setattr(common.os, "link", inspect_then_link)
    common.atomic_write_no_overwrite(target, b"complete")
    assert observed_before_link == [False]
    assert target.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".*.tmp-*"))


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
