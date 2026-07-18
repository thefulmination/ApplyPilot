from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SIGNER = ROOT / "scripts" / "sign-release-attestation.py"
MANIFEST_BUILDER = ROOT / "scripts" / "build-compatibility-manifest.py"
KEY = b"release-attestation-test-key-material-32-bytes-minimum"
KEY_ID = "release-test-key-v1"
KEY_B64 = base64.b64encode(KEY).decode("ascii")
ENV = {
    "APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": KEY_B64,
    "APPLYPILOT_RELEASE_ATTESTATION_KEY_ID": KEY_ID,
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


def _run(input_path: Path, output_path: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = {**os.environ, **ENV, **(env or {})}
    return subprocess.run(
        [sys.executable, str(SIGNER), "--input", str(input_path), "--output", str(output_path)],
        capture_output=True,
        text=True,
        env=process_env,
    )


def _write_unsigned(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _test_receipt() -> dict[str, Any]:
    return {
        "schemaVersion": "applypilot_test_receipt_v1",
        "producer": "applypilot-release-verifier-v1",
        "suiteIdentity": "applypilot-runtime-release-v1",
        "status": "passed",
        "sourceCommitSha": "1" * 40,
        "sourceTreeSha": "2" * 40,
        "commands": [{"command": "python -m pytest -q", "exitCode": 0}],
        "environment": {"runner": "release-verifier", "os": "windows"},
        "startedAt": "2026-07-18T12:00:00Z",
        "completedAt": "2026-07-18T12:15:00Z",
    }


def _topology_receipt() -> dict[str, Any]:
    return {
        "schemaVersion": "applypilot_railway_topology_receipt_v1",
        "producer": "applypilot-railway-topology-verifier-v1",
        "status": "verified",
        "railwayProject": "applypilot-staging",
        "railwayProjectId": UUIDS["project"],
        "railwayEnvironmentId": UUIDS["environment"],
        "postgresServiceId": UUIDS["postgres"],
        "atsWorkerServiceId": UUIDS["ats"],
        "linkedinWorkerServiceId": UUIDS["linkedin"],
        "databaseName": "applypilot_brain_staging",
        "sourceCommand": "railway status --json",
        "environment": "staging",
        "capturedAt": "2026-07-18T12:00:00Z",
        "expiresAt": "2026-07-18T12:30:00Z",
    }


def test_signed_fixtures_pass_committed_manifest_verifiers(tmp_path: Path) -> None:
    manifest = _load_module(MANIFEST_BUILDER, "manifest_verifier_for_signer_test")
    test_input = tmp_path / "test-unsigned.json"
    test_output = tmp_path / "test-signed.json"
    topology_input = tmp_path / "topology-unsigned.json"
    topology_output = tmp_path / "topology-signed.json"
    _write_unsigned(test_input, _test_receipt())
    _write_unsigned(topology_input, _topology_receipt())

    for input_path, output_path in ((test_input, test_output), (topology_input, topology_output)):
        result = _run(input_path, output_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert KEY_B64 not in result.stderr
        assert KEY_B64.encode("ascii") not in output_path.read_bytes()
        if os.name == "posix":
            assert output_path.stat().st_mode & 0o777 == 0o600

    generated_at = datetime(2026, 7, 18, 12, 20, tzinfo=timezone.utc)
    verified_test = manifest._test_receipt(
        test_output,
        "1" * 40,
        "2" * 40,
        "python",
        generated_at,
        KEY,
        KEY_ID,
    )
    assert verified_test["authentication"] == {"algorithm": "HMAC-SHA256", "keyId": KEY_ID}

    args = argparse.Namespace(
        railway_project="applypilot-staging",
        railway_project_id=UUIDS["project"],
        railway_environment_id=UUIDS["environment"],
        postgres_service_id=UUIDS["postgres"],
        ats_worker_service_id=UUIDS["ats"],
        linkedin_worker_service_id=UUIDS["linkedin"],
        database_name="applypilot_brain_staging",
    )
    verified_topology = manifest._topology_receipt(topology_output, args, generated_at, KEY, KEY_ID)
    assert verified_topology["authentication"] == {"algorithm": "HMAC-SHA256", "keyId": KEY_ID}


def test_signature_uses_exact_manifest_canonicalization_and_stdout_is_empty(tmp_path: Path) -> None:
    unsigned = tmp_path / "unsigned.json"
    output = tmp_path / "signed.json"
    document = {"z": "secret-like-sk-example", "unicode": "caf\u00e9", "nested": {"b": 2, "a": 1}}
    _write_unsigned(unsigned, document)

    result = _run(unsigned, output)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    signed = json.loads(output.read_text(encoding="utf-8"))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    expected = base64.b64encode(hmac.digest(KEY, canonical, hashlib.sha256)).decode("ascii")
    assert signed["authentication"] == {
        "algorithm": "HMAC-SHA256",
        "keyId": KEY_ID,
        "signature": expected,
    }
    assert KEY_B64 not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "valid UTF-8 JSON"),
        ("[]", "JSON object"),
        ('{"authentication":null}', "already contains authentication"),
        ('{"a":1,"a":2}', "duplicate object key"),
        ('{"value":NaN}', "non-standard JSON constant"),
    ],
)
def test_rejects_malformed_non_object_authenticated_and_non_strict_json(
    tmp_path: Path, content: str, message: str
) -> None:
    unsigned = tmp_path / "unsigned.json"
    output = tmp_path / "signed.json"
    unsigned.write_text(content, encoding="utf-8")

    result = _run(unsigned, output)

    assert result.returncode != 0
    assert message in result.stderr
    assert result.stdout == ""
    assert not output.exists()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": ""}, "environment variables are required"),
        ({"APPLYPILOT_RELEASE_ATTESTATION_KEY_ID": ""}, "environment variables are required"),
        ({"APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": "%%%"}, "valid base64"),
        (
            {"APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": base64.b64encode(b"short").decode("ascii")},
            "at least 32 bytes",
        ),
    ],
)
def test_rejects_invalid_attestation_configuration(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    unsigned = tmp_path / "unsigned.json"
    output = tmp_path / "signed.json"
    _write_unsigned(unsigned, {"status": "passed"})

    result = _run(unsigned, output, env)

    assert result.returncode != 0
    assert message in result.stderr
    assert result.stdout == ""
    assert KEY_B64 not in result.stderr
    assert not output.exists()


def test_rejects_same_path_and_existing_output(tmp_path: Path) -> None:
    unsigned = tmp_path / "receipt.json"
    existing = tmp_path / "existing.json"
    _write_unsigned(unsigned, {"status": "passed"})
    existing.write_text("preserve", encoding="utf-8")

    same_result = _run(unsigned, unsigned)
    overwrite_result = _run(unsigned, existing)

    assert same_result.returncode != 0
    assert "same file" in same_result.stderr
    assert overwrite_result.returncode != 0
    assert "already exists" in overwrite_result.stderr
    assert json.loads(unsigned.read_text(encoding="utf-8")) == {"status": "passed"}
    assert existing.read_text(encoding="utf-8") == "preserve"


def test_rejects_input_output_and_parent_symlinks(tmp_path: Path) -> None:
    unsigned = tmp_path / "unsigned.json"
    _write_unsigned(unsigned, {"status": "passed"})
    input_link = tmp_path / "input-link.json"
    output_link = tmp_path / "output-link.json"
    parent_link = tmp_path / "parent-link"
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    try:
        input_link.symlink_to(unsigned)
        output_link.symlink_to(tmp_path / "missing-target.json")
        parent_link.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    input_result = _run(input_link, tmp_path / "from-link.json")
    output_result = _run(unsigned, output_link)
    parent_result = _run(unsigned, parent_link / "signed.json")

    assert input_result.returncode != 0 and "symlink" in input_result.stderr
    assert output_result.returncode != 0 and "symlink" in output_result.stderr
    assert parent_result.returncode != 0 and "symlink" in parent_result.stderr
    assert not (real_parent / "signed.json").exists()


def test_tamper_and_wrong_key_fail_committed_manifest_authentication(tmp_path: Path) -> None:
    manifest = _load_module(MANIFEST_BUILDER, "manifest_verifier_for_tamper_test")
    unsigned = tmp_path / "unsigned.json"
    signed_path = tmp_path / "signed.json"
    _write_unsigned(unsigned, _test_receipt())
    result = _run(unsigned, signed_path)
    assert result.returncode == 0, result.stderr

    with pytest.raises(RuntimeError, match="signature is invalid"):
        manifest._authenticated_receipt(signed_path, b"x" * 32, KEY_ID, "test receipt")

    tampered = json.loads(signed_path.read_text(encoding="utf-8"))
    tampered["status"] = "failed"
    signed_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuntimeError, match="signature is invalid"):
        manifest._authenticated_receipt(signed_path, KEY, KEY_ID, "test receipt")


def test_failed_fsync_removes_partial_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = _load_module(SIGNER, "signer_for_fsync_test")
    output = tmp_path / "partial.json"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(signer.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated storage failure"):
        signer._write_exclusive_fsync(output, b"partial")
    assert not output.exists()
