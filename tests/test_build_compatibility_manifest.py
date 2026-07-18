from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-compatibility-manifest.py"
TRUSTED_PRODUCER = "applypilot-release-verifier-v1"
RUNTIME_SUITE = "applypilot-runtime-release-v1"
BRAIN_SUITE = "applypilot-brain-release-v1"
TOPOLOGY_PRODUCER = "applypilot-railway-topology-verifier-v1"
ATTESTATION_KEY = b"release-attestation-test-key-material-32-bytes-minimum"
ATTESTATION_KEY_ID = "release-test-key-v1"
ATTESTATION_ENV = {
    "APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": base64.b64encode(ATTESTATION_KEY).decode(),
    "APPLYPILOT_RELEASE_ATTESTATION_KEY_ID": ATTESTATION_KEY_ID,
}
UUIDS = {
    "project": "11111111-1111-4111-8111-111111111111",
    "environment": "22222222-2222-4222-8222-222222222222",
    "postgres": "33333333-3333-4333-8333-333333333333",
    "ats": "44444444-4444-4444-8444-444444444444",
    "linkedin": "55555555-5555-4555-8555-555555555555",
}
ARTIFACT_ROLES = (
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
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _initialize_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")


def _record(role: str, path: Path, root: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "role": role,
        "path": path.relative_to(root).as_posix(),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _brain_metadata() -> dict[str, Any]:
    candidate = {
        "policyVersion": "canonical-v7",
        "kgVersion": "1" * 64,
        "labelSnapshot": "2" * 64,
        "pairwiseSnapshot": "3" * 64,
        "outcomeSnapshot": "4" * 64,
    }
    return {
        "fitPolicyVersion": "fit-policy-v1",
        "fitPromptVersion": "fit-prompt-v1",
        "qualificationModels": ["qualification-v1", "qualification-v2-kg"],
        "preferenceModel": "regularized-bradley-terry-logistic",
        "outcomeModel": "regularized-outcome-logistic",
        "llmScoringModel": "deepseek-v4-pro",
        "alternateLlmScoringModel": "gemini-3.1-pro-preview",
        "embeddingModel": "text-embedding-3-large",
        "decisionPolicies": {
            "status": "draft",
            "count": 14,
            "rowSetSha256": "5" * 64,
            "atsCanaryCandidate": candidate,
            "linkedinCanaryCandidate": {**candidate, "policyVersion": "canonical-v7-linkedin"},
        },
    }


@dataclass
class Fixture:
    root: Path
    runtime: Path
    brain: Path
    source: Path
    sqlite: Path
    topology_receipt: Path
    template: Path
    runtime_head: str
    runtime_tree: str
    brain_head: str
    brain_tree: str
    command: list[str]

    def write_template(self, document: dict[str, Any], name: str) -> Path:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def document(self) -> dict[str, Any]:
        return json.loads(self.template.read_text(encoding="utf-8"))

    def command_for(
        self,
        *,
        output: str,
        template: Path | None = None,
        extra: list[str] | None = None,
    ) -> list[str]:
        command = self.command.copy()
        command[command.index(str(self.template))] = str(template or self.template)
        command[command.index(str(self.root / "manifest.json"))] = str(self.root / output)
        if extra:
            command.extend(extra)
        return command


def _sign_receipt(
    document: dict[str, Any],
    *,
    key: bytes = ATTESTATION_KEY,
    key_id: str = ATTESTATION_KEY_ID,
) -> dict[str, Any]:
    payload = {name: value for name, value in document.items() if name != "authentication"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    signature = base64.b64encode(hmac.digest(key, canonical, hashlib.sha256)).decode()
    return {
        **payload,
        "authentication": {
            "algorithm": "HMAC-SHA256",
            "keyId": key_id,
            "signature": signature,
        },
    }


def _write_signed_receipt(path: Path, document: dict[str, Any], **kwargs: Any) -> None:
    path.write_text(json.dumps(_sign_receipt(document, **kwargs)), encoding="utf-8")


def _build_fixture(tmp_path: Path) -> Fixture:
    runtime = tmp_path / "runtime"
    brain = tmp_path / "brain"
    source = tmp_path / "source"
    _initialize_repo(runtime)
    _initialize_repo(brain)
    source.mkdir()

    (runtime / "pyproject.toml").write_text(
        '[tool.example]\nversion = "9.9.9"\n[project]\nversion = "1.2.3"\n', encoding="utf-8"
    )
    for relative in (
        *(f"src/applypilot/brain/schema_v{version}.sql" for version in range(1, 6)),
        "src/applypilot/fleet/schema.py",
        "src/applypilot/fleet/schema_v3.sql",
        "src/applypilot/fleet/migrations/manifest-v1.json",
    ):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8", newline="\n")
    runtime_rollback = _commit(runtime, "rollback base")
    _git(runtime, "branch", "rollback")
    (runtime / "release-marker.txt").write_text("runtime release\n", encoding="utf-8")
    runtime_head = _commit(runtime, "runtime release")
    _git(runtime, "branch", "codex/runtime")
    runtime_tree = _git(runtime, "rev-parse", "HEAD^{tree}")

    (brain / "package.json").write_text("{}\n", encoding="utf-8")
    brain_rollback = _commit(brain, "rollback base")
    _git(brain, "branch", "rollback")
    (brain / "release-marker.txt").write_text("brain release\n", encoding="utf-8")
    brain_head = _commit(brain, "brain release")
    _git(brain, "branch", "codex/brain")
    brain_tree = _git(brain, "rev-parse", "HEAD^{tree}")

    artifact_records: list[dict[str, Any]] = []
    for index, role in enumerate(ARTIFACT_ROLES[1:], start=1):
        artifact = source / f"artifact-{index}.bin"
        artifact.write_bytes(f"{role}\n".encode())
        artifact_records.append(_record(role, artifact, source))
    inventory = source / "artifact-manifest.json"
    inventory.write_text(
        json.dumps({"schemaVersion": "applypilot_artifact_manifest_v2", "files": artifact_records}),
        encoding="utf-8",
    )
    all_artifacts = [_record("artifact_manifest", inventory, source), *artifact_records]

    sqlite = source / "brain.db"
    with sqlite3.connect(sqlite) as connection:
        connection.execute("PRAGMA user_version = 5")
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute('CREATE TABLE "job decisions" (id INTEGER PRIMARY KEY, job_id INTEGER)')
        connection.executemany("INSERT INTO jobs(value) VALUES (?)", [("a",), ("b",)])
        connection.executemany('INSERT INTO "job decisions"(job_id) VALUES (?)', [(1,), (1,), (2,)])
    sqlite_hash = hashlib.sha256(sqlite.read_bytes()).hexdigest()

    topology_receipt = tmp_path / "railway-topology.json"
    _write_signed_receipt(
        topology_receipt,
        {
            "schemaVersion": "applypilot_railway_topology_receipt_v1",
            "producer": TOPOLOGY_PRODUCER,
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
            "capturedAt": "2026-07-17T19:50:00Z",
            "expiresAt": "2026-07-17T20:20:00Z",
        },
    )

    template = tmp_path / "template.json"
    template_document = {
        "schemaVersion": "applypilot_compatibility_manifest_v2",
        "repositories": {
            "python": {
                "repository": "owner/runtime",
                "branch": "codex/runtime",
                "rollback": {
                    "branch": "rollback",
                    "commitSha": runtime_rollback,
                    "role": "rollback-only",
                },
            },
            "typescript": {
                "repository": "owner/brain",
                "branch": "codex/brain",
                "rollback": {
                    "branch": "rollback",
                    "commitSha": brain_rollback,
                    "role": "reference-only",
                },
            },
        },
        "artifacts": {
            "sourceRoot": "Z:/historical/not-local",
            "distribution": "external-content-addressed-bootstrap-required",
            "files": all_artifacts,
        },
        "canonicalSqliteSource": {
            "path": "Z:/historical/brain.db",
            "bytes": sqlite.stat().st_size,
            "sha256": sqlite_hash,
        },
        "brain": _brain_metadata(),
        "postgresParity": {"status": "stale-must-not-survive"},
        "authorityPromotionReceipt": {"status": "stale-must-not-survive"},
        "deployment": {"imageDigest": "stale-must-not-survive"},
    }
    template.write_text(json.dumps(template_document), encoding="utf-8")

    output = tmp_path / "manifest.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--template",
        str(template),
        "--runtime-repo",
        str(runtime),
        "--brain-repo",
        str(brain),
        "--release-id",
        "applypilot-test-rc1",
        "--database-name",
        "applypilot_brain_staging",
        "--artifact-source-root",
        str(source),
        "--sqlite-source",
        str(sqlite),
        "--railway-topology-receipt",
        str(topology_receipt),
        "--generated-at",
        "2026-07-17T20:00:00Z",
        "--railway-project",
        "applypilot-staging",
        "--railway-project-id",
        UUIDS["project"],
        "--railway-environment-id",
        UUIDS["environment"],
        "--postgres-service-id",
        UUIDS["postgres"],
        "--ats-worker-service-id",
        UUIDS["ats"],
        "--linkedin-worker-service-id",
        UUIDS["linkedin"],
        "--output",
        str(output),
    ]
    return Fixture(
        root=tmp_path,
        runtime=runtime,
        brain=brain,
        source=source,
        sqlite=sqlite,
        topology_receipt=topology_receipt,
        template=template,
        runtime_head=runtime_head,
        runtime_tree=runtime_tree,
        brain_head=brain_head,
        brain_tree=brain_tree,
        command=command,
    )


@pytest.fixture
def release_fixture(tmp_path: Path) -> Fixture:
    return _build_fixture(tmp_path)


def _run(
    command: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = {**os.environ, **ATTESTATION_ENV, **(env or {})}
    return subprocess.run(command, check=check, capture_output=True, text=True, env=process_env)


def _test_receipt(commit: str, tree: str, suite: str) -> dict[str, Any]:
    return {
        "schemaVersion": "applypilot_test_receipt_v1",
        "producer": TRUSTED_PRODUCER,
        "suiteIdentity": suite,
        "status": "passed",
        "sourceCommitSha": commit,
        "sourceTreeSha": tree,
        "commands": [
            {"command": "python -m pytest -q", "exitCode": 0},
            {"command": "python -m ruff check .", "exitCode": 0},
        ],
        "environment": {"runner": "release-verifier", "os": "windows", "python": "3.12"},
        "startedAt": "2026-07-17T19:00:00Z",
        "completedAt": "2026-07-17T19:30:00Z",
    }


def test_manifest_generator_emits_only_current_allowlisted_evidence(release_fixture: Fixture) -> None:
    fixture = release_fixture
    document = fixture.document()
    document["brain"]["liveActivationProven"] = True
    document["brain"]["authorityPromotionReceipt"] = {"status": "passed"}
    template = fixture.write_template(document, "stale-template.json")
    output = fixture.root / "manifest.json"

    result = _run(fixture.command_for(output="manifest.json", template=template), check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))

    assert json.loads(result.stdout)["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["schemaVersion"] == "applypilot_compatibility_manifest_v3"
    assert manifest["generatedAt"] == "2026-07-17T20:00:00.000Z"
    assert manifest["repositories"]["python"]["sourceCommitSha"] == fixture.runtime_head
    assert manifest["repositories"]["typescript"]["sourceCommitSha"] == fixture.brain_head
    assert "testedCommitSha" not in manifest["repositories"]["python"]
    assert "testedCommitSha" not in manifest["repositories"]["typescript"]
    assert "liveActivationProven" not in manifest["brain"]["declaration"]
    assert "authorityPromotionReceipt" not in manifest["brain"]["declaration"]
    assert "postgresParity" not in manifest
    assert "authorityPromotionReceipt" not in manifest
    assert "imageDigest" not in manifest["deployment"]
    assert manifest["deployment"]["atsWorkerServiceId"] == UUIDS["ats"]
    assert manifest["deployment"]["linkedinWorkerServiceId"] == UUIDS["linkedin"]
    assert manifest["deployment"]["topologyReceipt"]["authentication"] == {
        "algorithm": "HMAC-SHA256",
        "keyId": ATTESTATION_KEY_ID,
    }
    assert manifest["canonicalSqliteSource"]["quickCheck"] == "ok"
    assert manifest["canonicalSqliteSource"]["schemaVersion"] == 2
    assert manifest["canonicalSqliteSource"]["userVersion"] == 5
    assert manifest["canonicalSqliteSource"]["tableCounts"] == {
        "job decisions": 3,
        "jobs": 2,
    }
    assert "signature" not in json.dumps(manifest)
    assert ATTESTATION_ENV["APPLYPILOT_RELEASE_ATTESTATION_KEY_B64"] not in json.dumps(manifest)
    assert len(manifest["artifacts"]["files"]) == len(ARTIFACT_ROLES)


def test_schema_bundle_uses_independent_framed_path_and_content_hash(release_fixture: Fixture) -> None:
    fixture = release_fixture
    output = fixture.root / "bundle.json"
    _run(fixture.command_for(output="bundle.json"), check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    reverse = hashlib.sha256()
    records: list[tuple[bytes, bytes]] = []
    for version in range(1, 6):
        relative = f"src/applypilot/brain/schema_v{version}.sql"
        content = subprocess.run(
            ["git", "-C", str(fixture.runtime), "show", f"{fixture.runtime_head}:{relative}"],
            check=True,
            capture_output=True,
        ).stdout
        records.append((relative.encode(), content))
    for target, iterable in ((digest, records), (reverse, reversed(records))):
        for name, content in iterable:
            target.update(len(name).to_bytes(8, "big"))
            target.update(name)
            target.update(len(content).to_bytes(8, "big"))
            target.update(content)
    assert manifest["schemas"]["brainBundle"]["sha256"] == digest.hexdigest()
    assert digest.hexdigest() != reverse.hexdigest()


def test_inventory_manifest_must_reconcile_every_non_manifest_role(release_fixture: Fixture) -> None:
    fixture = release_fixture
    inventory = fixture.source / "artifact-manifest.json"
    internal = json.loads(inventory.read_text(encoding="utf-8"))
    internal["files"].pop()
    inventory.write_text(json.dumps(internal), encoding="utf-8")
    document = fixture.document()
    document["artifacts"]["files"][0] = _record("artifact_manifest", inventory, fixture.source)
    template = fixture.write_template(document, "bad-inventory.json")
    result = _run(fixture.command_for(output="bad-inventory-output.json", template=template))
    assert result.returncode != 0
    assert "artifact manifest does not reconcile" in result.stderr


def test_minimal_or_untrusted_test_receipts_cannot_create_tested_identity(release_fixture: Fixture) -> None:
    fixture = release_fixture
    minimal = fixture.root / "minimal.json"
    minimal.write_text(json.dumps({"status": "passed", "sourceCommitSha": fixture.runtime_head}), encoding="utf-8")
    result = _run(
        fixture.command_for(
            output="minimal-output.json",
            extra=["--runtime-test-receipt", str(minimal)],
        )
    )
    assert result.returncode != 0
    assert "authentication" in result.stderr

    untrusted = fixture.root / "untrusted.json"
    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    receipt["producer"] = "self-authored"
    _write_signed_receipt(untrusted, receipt)
    result = _run(
        fixture.command_for(
            output="untrusted-output.json",
            extra=["--runtime-test-receipt", str(untrusted)],
        )
    )
    assert result.returncode != 0
    assert "trusted producer" in result.stderr


def test_strict_receipts_bind_suite_commands_environment_commit_and_tree(release_fixture: Fixture) -> None:
    fixture = release_fixture
    runtime_receipt = fixture.root / "runtime-tests.json"
    brain_receipt = fixture.root / "brain-tests.json"
    _write_signed_receipt(runtime_receipt, _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE))
    _write_signed_receipt(brain_receipt, _test_receipt(fixture.brain_head, fixture.brain_tree, BRAIN_SUITE))
    output = fixture.root / "tested.json"
    _run(
        fixture.command_for(
            output="tested.json",
            extra=[
                "--runtime-test-receipt",
                str(runtime_receipt),
                "--brain-test-receipt",
                str(brain_receipt),
            ],
        ),
        check=True,
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["repositories"]["python"]["testedTreeSha"] == fixture.runtime_tree
    assert manifest["repositories"]["typescript"]["testedTreeSha"] == fixture.brain_tree
    assert manifest["repositories"]["python"]["testReceipt"]["suiteIdentity"] == RUNTIME_SUITE

    invalid_receipts: list[tuple[str, dict[str, Any], str]] = []
    bad_schema = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_schema["schemaVersion"] = "invented"
    invalid_receipts.append(("schema", bad_schema, "schema version"))
    bad_suite = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_suite["suiteIdentity"] = BRAIN_SUITE
    invalid_receipts.append(("suite", bad_suite, "suite identity"))
    bad_tree = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_tree["sourceTreeSha"] = "f" * 40
    invalid_receipts.append(("tree", bad_tree, "source commit and tree"))
    bad_command = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_command["commands"][1]["exitCode"] = 1
    invalid_receipts.append(("command", bad_command, "successful commands"))
    bad_environment = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_environment["environment"] = {"python": "3.12"}
    invalid_receipts.append(("environment", bad_environment, "environment"))
    bad_time = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_time["completedAt"] = "2026-07-17T18:00:00Z"
    invalid_receipts.append(("timestamp", bad_time, "precedes"))
    for name, bad, message in invalid_receipts:
        _write_signed_receipt(runtime_receipt, bad)
        result = _run(
            fixture.command_for(
                output=f"invalid-receipt-{name}.json",
                extra=["--runtime-test-receipt", str(runtime_receipt)],
            )
        )
        assert result.returncode != 0
        assert message in result.stderr


@pytest.mark.parametrize("attack", ["unsigned", "wrong_key", "wrong_key_id", "tampered", "stale"])
def test_test_receipt_authentication_rejects_adversarial_claims(release_fixture: Fixture, attack: str) -> None:
    fixture = release_fixture
    path = fixture.root / f"test-receipt-{attack}.json"
    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    if attack == "stale":
        receipt["startedAt"] = "2026-07-15T18:00:00Z"
        receipt["completedAt"] = "2026-07-15T18:30:00Z"
    if attack == "unsigned":
        path.write_text(json.dumps(receipt), encoding="utf-8")
    elif attack == "wrong_key":
        _write_signed_receipt(path, receipt, key=b"x" * 32)
    elif attack == "wrong_key_id":
        _write_signed_receipt(path, receipt, key_id="attacker-key")
    else:
        _write_signed_receipt(path, receipt)
        if attack == "tampered":
            signed = json.loads(path.read_text(encoding="utf-8"))
            signed["sourceCommitSha"] = "f" * 40
            path.write_text(json.dumps(signed), encoding="utf-8")
    result = _run(
        fixture.command_for(
            output=f"test-receipt-{attack}-output.json",
            extra=["--runtime-test-receipt", str(path)],
        )
    )
    assert result.returncode != 0
    assert not (fixture.root / f"test-receipt-{attack}-output.json").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("producer", "forged-producer", "producer"),
        ("status", "pending", "status"),
        ("sourceCommand", "railway status", "command"),
        ("environment", "production", "environment"),
        ("postgresServiceId", UUIDS["ats"], "deployment identity"),
        ("expiresAt", "2026-07-17T19:55:00Z", "stale"),
    ],
)
def test_topology_receipt_requires_authenticated_exact_fresh_observation(
    release_fixture: Fixture, field: str, value: str, message: str
) -> None:
    fixture = release_fixture
    receipt = json.loads(fixture.topology_receipt.read_text(encoding="utf-8"))
    receipt.pop("authentication")
    receipt[field] = value
    _write_signed_receipt(fixture.topology_receipt, receipt)
    result = _run(fixture.command_for(output=f"bad-topology-{field}.json"))
    assert result.returncode != 0
    assert message in result.stderr


def test_topology_receipt_tampering_and_missing_key_fail_closed(release_fixture: Fixture) -> None:
    fixture = release_fixture
    receipt = json.loads(fixture.topology_receipt.read_text(encoding="utf-8"))
    receipt["databaseName"] = "attacker_database"
    fixture.topology_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    result = _run(fixture.command_for(output="tampered-topology.json"))
    assert result.returncode != 0
    assert "signature is invalid" in result.stderr

    env = {"APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": "", "APPLYPILOT_RELEASE_ATTESTATION_KEY_ID": ""}
    result = _run(fixture.command_for(output="missing-key.json"), env=env)
    assert result.returncode != 0
    assert "environment variables are required" in result.stderr

    short_key_env = {
        "APPLYPILOT_RELEASE_ATTESTATION_KEY_B64": base64.b64encode(b"too-short").decode(),
        "APPLYPILOT_RELEASE_ATTESTATION_KEY_ID": ATTESTATION_KEY_ID,
    }
    result = _run(fixture.command_for(output="short-key.json"), env=short_key_env)
    assert result.returncode != 0
    assert "at least 32 bytes" in result.stderr


def test_declared_release_branches_must_equal_selected_commit_heads(release_fixture: Fixture) -> None:
    fixture = release_fixture
    _git(fixture.runtime, "branch", "-f", "codex/runtime", "rollback")
    result = _run(fixture.command_for(output="branch-drift.json"))
    assert result.returncode != 0
    assert "branch head does not equal selected commit" in result.stderr


@pytest.mark.parametrize("failure", ["same_commit", "missing_branch", "unreachable_commit"])
def test_rollback_must_be_distinct_and_resolve_from_declared_branch(release_fixture: Fixture, failure: str) -> None:
    fixture = release_fixture
    document = fixture.document()
    rollback = document["repositories"]["python"]["rollback"]
    if failure == "same_commit":
        rollback["commitSha"] = fixture.runtime_head
        rollback["branch"] = _git(fixture.runtime, "branch", "--show-current")
    elif failure == "missing_branch":
        rollback["branch"] = "does-not-exist"
    else:
        other = fixture.root / "other"
        _initialize_repo(other)
        (other / "other.txt").write_text("other\n", encoding="utf-8")
        other_commit = _commit(other, "other")
        _git(fixture.runtime, "fetch", str(other), f"{other_commit}:refs/heads/unrelated")
        rollback["commitSha"] = other_commit
    template = fixture.write_template(document, f"rollback-{failure}.json")
    result = _run(fixture.command_for(output=f"rollback-{failure}-output.json", template=template))
    assert result.returncode != 0
    assert "rollback" in result.stderr.lower()


def test_brain_metadata_requires_typed_versions_models_and_policy_hashes(release_fixture: Fixture) -> None:
    fixture = release_fixture
    document = fixture.document()
    document["brain"]["qualificationModels"] = []
    document["brain"]["decisionPolicies"]["rowSetSha256"] = "not-a-hash"
    template = fixture.write_template(document, "bad-brain.json")
    result = _run(fixture.command_for(output="bad-brain-output.json", template=template))
    assert result.returncode != 0
    assert "brain/model declaration" in result.stderr


def test_sqlite_evidence_is_derived_directly_and_non_sqlite_is_rejected(
    release_fixture: Fixture,
) -> None:
    fixture = release_fixture
    document = fixture.document()
    fabricated = fixture.source / "fabricated.db"
    fabricated.write_text('{"quick_check":"ok","table_counts":{"jobs":999}}', encoding="utf-8")
    document["canonicalSqliteSource"].update(
        bytes=fabricated.stat().st_size,
        sha256=hashlib.sha256(fabricated.read_bytes()).hexdigest(),
        receipt={"quick_check": "ok", "table_counts": {"jobs": 999}},
    )
    template = fixture.write_template(document, "fabricated-sqlite.json")
    command = fixture.command_for(output="fabricated-sqlite-output.json", template=template)
    command[command.index("--sqlite-source") + 1] = str(fabricated)
    result = _run(command)
    assert result.returncode != 0
    assert "not a valid SQLite database" in result.stderr


@pytest.mark.parametrize(
    "bad_path",
    [
        "C:/artifact.bin",
        "C:artifact.bin",
        "//server/share/artifact.bin",
        "/absolute.bin",
        "a/../b.bin",
        "a/./b.bin",
        "a\\b.bin",
    ],
)
def test_artifact_paths_reject_windows_aliases_absolute_dot_and_backslash(
    release_fixture: Fixture, bad_path: str
) -> None:
    fixture = release_fixture
    document = fixture.document()
    document["artifacts"]["files"][1]["path"] = bad_path
    template = fixture.write_template(document, "bad-path.json")
    result = _run(fixture.command_for(output="bad-path-output.json", template=template))
    assert result.returncode != 0
    assert "normalized relative POSIX path" in result.stderr


def test_artifact_inventory_rejects_duplicate_physical_files_and_symlinks(release_fixture: Fixture) -> None:
    fixture = release_fixture
    document = fixture.document()
    original = fixture.source / document["artifacts"]["files"][1]["path"]
    alias = fixture.source / "physical-alias.bin"
    os.link(original, alias)
    replacement = deepcopy(document["artifacts"]["files"][2])
    replacement.update(
        path=alias.name, bytes=original.stat().st_size, sha256=hashlib.sha256(original.read_bytes()).hexdigest()
    )
    document["artifacts"]["files"][2] = replacement
    template = fixture.write_template(document, "physical-alias.json")
    result = _run(fixture.command_for(output="physical-alias-output.json", template=template))
    assert result.returncode != 0
    assert "duplicate physical paths" in result.stderr

    symlink = fixture.source / "symlink.bin"
    try:
        symlink.symlink_to(original)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    document = fixture.document()
    replacement = deepcopy(document["artifacts"]["files"][1])
    replacement["path"] = symlink.name
    document["artifacts"]["files"][1] = replacement
    template = fixture.write_template(document, "symlink.json")
    result = _run(fixture.command_for(output="symlink-output.json", template=template))
    assert result.returncode != 0
    assert "symlink" in result.stderr


def test_template_stable_read_rejects_concurrent_mutation(
    release_fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    path = release_fixture.template
    original = module._read_bytes
    calls = 0

    def mutating_read(target: Path) -> bytes:
        nonlocal calls
        calls += 1
        content = original(target)
        if calls == 1:
            target.write_bytes(content + b" ")
        return content

    monkeypatch.setattr(module, "_read_bytes", mutating_read)
    with pytest.raises(RuntimeError, match="changed while reading"):
        module._stable_read_bytes(path)


def test_large_input_double_hash_rejects_concurrent_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    path = tmp_path / "mutable.bin"
    path.write_bytes(b"first")
    original = module._sha256
    calls = 0

    def mutating_hash(target: Path) -> str:
        nonlocal calls
        calls += 1
        digest = original(target)
        if calls == 1:
            target.write_bytes(b"second")
        return digest

    monkeypatch.setattr(module, "_sha256", mutating_hash)
    with pytest.raises(RuntimeError, match="changed while hashing"):
        module._stable_file_record(path)


def test_interrupted_exclusive_write_removes_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    target = tmp_path / "partial.json"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated storage failure"):
        module._write_exclusive_fsync(target, b"partial evidence")
    assert not target.exists()


def test_partial_evidence_pair_is_repairable_and_complete_pair_is_immutable(release_fixture: Fixture) -> None:
    fixture = release_fixture
    output = fixture.root / "repair.json"
    command = fixture.command_for(output="repair.json")
    first = _run(command, check=True)
    result = json.loads(first.stdout)
    sidecar = output.with_name(output.name + ".sha256")
    sidecar.unlink()
    repaired = _run(command, check=True)
    assert json.loads(repaired.stdout)["sha256"] == result["sha256"]
    assert sidecar.exists()
    complete = _run(command)
    assert complete.returncode != 0
    assert "complete immutable release evidence" in complete.stderr


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--railway-project-id", ""),
        ("--railway-environment-id", "not-a-uuid"),
        ("--postgres-service-id", " "),
        ("--database-name", "bad database"),
    ],
)
def test_deployment_identity_rejects_empty_or_invalid_values(
    release_fixture: Fixture, argument: str, value: str
) -> None:
    command = release_fixture.command_for(output=f"invalid-{argument[2:]}.json")
    command[command.index(argument) + 1] = value
    result = _run(command)
    assert result.returncode != 0
    assert "deployment identity" in result.stderr


def test_ats_and_linkedin_services_must_be_distinct(release_fixture: Fixture) -> None:
    command = release_fixture.command_for(output="same-workers.json")
    command[command.index("--linkedin-worker-service-id") + 1] = UUIDS["ats"]
    result = _run(command)
    assert result.returncode != 0
    assert "ATS and LinkedIn worker service IDs must differ" in result.stderr


def _load_module():
    spec = importlib.util.spec_from_file_location("compatibility_manifest_generator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
