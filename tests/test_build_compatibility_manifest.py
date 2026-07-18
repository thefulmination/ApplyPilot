from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-compatibility-manifest.py"
TRUSTED_PRODUCER = "applypilot-release-evidence-producer-v2"
PRODUCER_VERSION = "2.0.0"
RUNTIME_SUITE = "applypilot-runtime-release-v2"
BRAIN_SUITE = "applypilot-brain-release-v2"
RELEASE_ID = "applypilot-test-rc1"
RELEASE_NONCE = "release_nonce_0123456789abcdef0123456789abcdef"
RUNTIME_KEY = b"runtime-test-attestation-key-material-32-bytes-minimum"
BRAIN_KEY = b"brain-test-attestation-key-material-32-bytes-minimum"
TOPOLOGY_KEY = b"railway-topology-attestation-key-material-32-bytes"
RUNTIME_KEY_ID = "runtime-release-test-key-v2"
BRAIN_KEY_ID = "brain-release-test-key-v2"
TOPOLOGY_KEY_ID = "railway-release-test-key-v2"
ATTESTATION_ENV = {
    "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64": base64.b64encode(RUNTIME_KEY).decode(),
    "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_ID": RUNTIME_KEY_ID,
    "APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_B64": base64.b64encode(BRAIN_KEY).decode(),
    "APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_ID": BRAIN_KEY_ID,
    "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64": base64.b64encode(TOPOLOGY_KEY).decode(),
    "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID": TOPOLOGY_KEY_ID,
}
SUITE_POLICIES = {
    RUNTIME_SUITE: {
        "purpose": "runtime-tests",
        "suiteIdentity": RUNTIME_SUITE,
        "commands": (
            ("pytest", "python -m pytest -q", ("-m", "pytest", "-q")),
            ("ruff", "python -m ruff check .", ("-m", "ruff", "check", ".")),
        ),
    },
    BRAIN_SUITE: {
        "purpose": "brain-tests",
        "suiteIdentity": BRAIN_SUITE,
        "commands": (
            ("typecheck", "npm run typecheck", ("npm", "run", "typecheck")),
            ("tests", "npm test", ("npm", "test")),
        ),
    },
}
TEST_ENVIRONMENT_POLICY = {
    "schemaVersion": "applypilot-test-environment-policy-v1",
    "inheritedAllowlist": (
        "APPDATA", "COMSPEC", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOCALAPPDATA", "PATH",
        "PATHEXT", "SSL_CERT_DIR", "SSL_CERT_FILE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "TZ",
        "USERPROFILE", "WINDIR",
    ),
    "fixed": {
        "GIT_CONFIG_GLOBAL": "<OS_DEVNULL>", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_USERCONFIG": "<OS_DEVNULL>",
        "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
    },
    "rejectedExact": (
        "BABEL_ENV", "GREP", "JEST_SHARD", "MOCHA_GREP", "NODE_ENV", "NODE_OPTIONS", "NODE_PATH",
        "ONLY", "PYTHONBREAKPOINT", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONWARNINGS",
        "PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS", "SKIP", "TEST",
        "TEST_FILTER", "TEST_GREP", "TEST_NAME_PATTERN", "TEST_PATH_PATTERN", "TEST_PATTERN", "TESTS",
        "VITEST", "VITEST_RELATED", "VITEST_SHARD",
    ),
    "rejectedPrefixes": ("NPM_CONFIG_",),
}
UUIDS = {
    "project": "11111111-1111-4111-8111-111111111111",
    "environment": "22222222-2222-4222-8222-222222222222",
    "postgres": "33333333-3333-4333-8333-333333333333",
    "control_plane": "44444444-4444-4444-8444-444444444444",
    "gateway": "55555555-5555-4555-8555-555555555555",
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
    key: bytes | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    purpose = document.get("receiptPurpose")
    defaults = {
        "runtime-tests": (RUNTIME_KEY, RUNTIME_KEY_ID),
        "brain-tests": (BRAIN_KEY, BRAIN_KEY_ID),
        "railway-topology": (TOPOLOGY_KEY, TOPOLOGY_KEY_ID),
    }
    default_key, default_id = defaults[purpose]
    key = key or default_key
    key_id = key_id or default_id
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


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _command_record(command_id: str, command: str, argv: list[str], when: datetime) -> dict[str, Any]:
    purpose = "railway-cli"
    if command_id in {"pytest", "ruff"}:
        purpose = "runtime-python"
    elif command_id in {"typecheck", "tests"}:
        purpose = "brain-node"
    record = {
        "commandId": command_id,
        "command": command,
        "argv": argv,
        "executable": {
            "path": argv[0],
            "sha256": "5" * 64,
            "purpose": purpose,
            "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
        },
        "exitCode": 0,
        "stdoutSha256": "6" * 64,
        "stderrSha256": "7" * 64,
        "logSha256": "8" * 64,
        "startedAt": _timestamp(when),
        "completedAt": _timestamp(when + timedelta(seconds=1)),
    }
    if command_id in {"typecheck", "tests"}:
        record["dependencies"] = {
            "npmCli": {
                "path": argv[1],
                "sha256": "9" * 64,
                "purpose": "brain-npm-cli",
                "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
            }
        }
        record["executionEnvironment"] = {"PATH": str(Path(argv[0]).parent)}
    return record


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
    receipt_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    railway_path = str((tmp_path / "railway.exe").resolve())
    _write_signed_receipt(
        topology_receipt,
        {
            "schemaVersion": "applypilot_railway_topology_receipt_v4",
            "receiptPurpose": "railway-topology",
            "producer": TRUSTED_PRODUCER,
            "producerVersion": PRODUCER_VERSION,
            "releaseId": RELEASE_ID,
            "releaseNonce": RELEASE_NONCE,
            "status": "verified",
            "railwayProject": "applypilot-staging",
            "railwayProjectId": UUIDS["project"],
            "railwayEnvironmentId": UUIDS["environment"],
            "expectedRailwayEnvironmentName": "production",
            "observedRailwayEnvironmentName": "production",
            "postgresServiceId": UUIDS["postgres"],
            "controlPlaneServiceId": UUIDS["control_plane"],
            "controlPlaneServiceName": "applypilot-control-plane",
            "gatewayServiceId": UUIDS["gateway"],
            "gatewayServiceName": "applypilot-tailscale-gateway",
            "serviceRoles": {"controlPlane": "control-plane", "gateway": "gateway"},
            "workerBrowserContractMarkersPresent": [],
            "databaseName": "applypilot_brain_staging",
            "commands": [
                _command_record(
                    "railway-version", "railway --version", [railway_path, "--version"], receipt_time
                ),
                _command_record(
                    "railway-status", "railway status --json", [railway_path, "status", "--json"],
                    receipt_time + timedelta(seconds=2),
                ),
                _command_record(
                    "railway-postgres-variables",
                    f"railway variables --service {UUIDS['postgres']} --json",
                    [railway_path, "variables", "--service", UUIDS["postgres"], "--json"],
                    receipt_time + timedelta(seconds=4),
                ),
                _command_record(
                    "railway-control-plane-variables",
                    f"railway variables --service {UUIDS['control_plane']} --json",
                    [railway_path, "variables", "--service", UUIDS["control_plane"], "--json"],
                    receipt_time + timedelta(seconds=6),
                ),
                _command_record(
                    "railway-gateway-variables",
                    f"railway variables --service {UUIDS['gateway']} --json",
                    [railway_path, "variables", "--service", UUIDS["gateway"], "--json"],
                    receipt_time + timedelta(seconds=8),
                ),
            ],
            "railwayCli": {
                "version": "railway 5.23.0",
                "executable": {
                    "path": railway_path,
                    "sha256": "5" * 64,
                    "purpose": "railway-cli",
                    "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
                },
                "statusSchema": "railway-cli-5-project-status-v1",
                "variablesSchema": "railway-cli-5-flat-service-variables-v1",
            },
            "releaseStage": "staging",
            "executionBoundary": {
                "browserExecutionLocation": "fleet-nodes-only",
                "linkedinExecutionLocation": "owner-home-node-only",
                "railwayBrowserWorkersPermitted": False,
                "runtimeEnforcementProven": False,
            },
            "topologyLimitation": (
                "Railway CLI status and variables prove configured service identity and role markers, "
                "not the runtime process tree or fleet-node routing enforcement."
            ),
            "capturedAt": _timestamp(receipt_time),
            "expiresAt": _timestamp(receipt_time + timedelta(minutes=30)),
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
        RELEASE_ID,
        "--release-nonce",
        RELEASE_NONCE,
        "--database-name",
        "applypilot_brain_staging",
        "--artifact-source-root",
        str(source),
        "--sqlite-source",
        str(sqlite),
        "--railway-topology-receipt",
        str(topology_receipt),
        "--railway-project",
        "applypilot-staging",
        "--railway-project-id",
        UUIDS["project"],
        "--railway-environment-id",
        UUIDS["environment"],
        "--expected-railway-environment-name",
        "production",
        "--postgres-service-id",
        UUIDS["postgres"],
        "--control-plane-service-id",
        UUIDS["control_plane"],
        "--control-plane-service-name",
        "applypilot-control-plane",
        "--gateway-service-id",
        UUIDS["gateway"],
        "--gateway-service-name",
        "applypilot-tailscale-gateway",
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
    git = Path(shutil.which("git") or "").resolve()
    tool_env = {
        "APPLYPILOT_RELEASE_GIT_PATH": str(git),
        "APPLYPILOT_RELEASE_GIT_SHA256": hashlib.sha256(git.read_bytes()).hexdigest(),
    }
    process_env = {**os.environ, **ATTESTATION_ENV, **tool_env, **(env or {})}
    return subprocess.run(command, check=check, capture_output=True, text=True, env=process_env)


def _test_receipt(commit: str, tree: str, suite: str) -> dict[str, Any]:
    policy = SUITE_POLICIES[suite]
    when = datetime.now(timezone.utc) - timedelta(minutes=2)
    commands = []
    for index, (command_id, command, arguments) in enumerate(policy["commands"]):
        if policy["purpose"] == "runtime-tests":
            argv = [str(Path(sys.executable).resolve()), *arguments]
        else:
            argv = [
                str((Path.cwd() / "node.exe").resolve()),
                str((Path.cwd() / "npm-cli.js").resolve()),
                *arguments[1:],
            ]
        commands.append(_command_record(command_id, command, argv, when + timedelta(seconds=index * 2)))
    git = Path(shutil.which("git") or "").resolve()
    return {
        "schemaVersion": "applypilot_test_receipt_v4",
        "receiptPurpose": policy["purpose"],
        "producer": TRUSTED_PRODUCER,
        "producerVersion": PRODUCER_VERSION,
        "releaseId": RELEASE_ID,
        "releaseNonce": RELEASE_NONCE,
        "suiteIdentity": suite,
        "suitePolicySha256": hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest(),
        "status": "passed",
        "sourceCommitSha": commit,
        "sourceTreeSha": tree,
        "sourceControl": {
            "system": "git",
            "executable": {
                "path": str(git),
                "sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
                "purpose": "release-git",
                "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
            },
        },
        "commands": commands,
        "environment": {
            "platform": "test-platform",
            "pythonExecutable": str(Path(sys.executable).resolve()),
            "pythonVersion": "3.12",
            "executionPolicySha256": hashlib.sha256(
                json.dumps(TEST_ENVIRONMENT_POLICY, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            ).hexdigest(),
            "inheritedEnvironmentSha256": {"PATH": "4" * 64},
        },
        "startedAt": _timestamp(when),
        "completedAt": commands[-1]["completedAt"],
    }


def test_manifest_generator_emits_only_current_allowlisted_evidence(release_fixture: Fixture) -> None:
    fixture = release_fixture
    document = fixture.document()
    document["brain"]["liveActivationProven"] = True
    document["brain"]["authorityPromotionReceipt"] = {"status": "passed"}
    template = fixture.write_template(document, "stale-template.json")
    output = fixture.root / "manifest.json"

    before = datetime.now(timezone.utc)
    result = _run(fixture.command_for(output="manifest.json", template=template), check=True)
    after = datetime.now(timezone.utc)
    manifest = json.loads(output.read_text(encoding="utf-8"))

    assert json.loads(result.stdout)["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["schemaVersion"] == "applypilot_compatibility_manifest_v4"
    generated_at = datetime.fromisoformat(manifest["generatedAt"].replace("Z", "+00:00"))
    assert before <= generated_at <= after
    assert manifest["releaseNonce"] == RELEASE_NONCE
    assert manifest["repositories"]["python"]["sourceCommitSha"] == fixture.runtime_head
    assert manifest["repositories"]["typescript"]["sourceCommitSha"] == fixture.brain_head
    assert "testedCommitSha" not in manifest["repositories"]["python"]
    assert "testedCommitSha" not in manifest["repositories"]["typescript"]
    assert "liveActivationProven" not in manifest["brain"]["declaration"]
    assert "authorityPromotionReceipt" not in manifest["brain"]["declaration"]
    assert "postgresParity" not in manifest
    assert "authorityPromotionReceipt" not in manifest
    assert "imageDigest" not in manifest["deployment"]
    assert manifest["deployment"]["controlPlaneServiceId"] == UUIDS["control_plane"]
    assert manifest["deployment"]["gatewayServiceId"] == UUIDS["gateway"]
    assert manifest["deployment"]["releaseStage"] == "staging"
    assert "releaseEnvironment" not in manifest["deployment"]
    assert manifest["deployment"]["expectedRailwayEnvironmentName"] == "production"
    assert manifest["deployment"]["observedRailwayEnvironmentName"] == "production"
    assert manifest["deployment"]["executionBoundary"]["browserExecutionLocation"] == "fleet-nodes-only"
    assert manifest["deployment"]["executionBoundary"]["linkedinExecutionLocation"] == "owner-home-node-only"
    assert manifest["deployment"]["executionBoundary"]["railwayBrowserWorkersPermitted"] is False
    assert manifest["deployment"]["executionBoundary"]["runtimeEnforcementProven"] is False
    assert manifest["deployment"]["serviceRoles"] == {
        "controlPlane": "control-plane",
        "gateway": "gateway",
    }
    assert manifest["deployment"]["workerBrowserContractMarkersPresent"] == []
    assert "runtime process tree" in manifest["deployment"]["topologyLimitations"][0]
    assert "atsWorkerServiceId" not in json.dumps(manifest)
    assert "linkedinWorkerServiceId" not in json.dumps(manifest)
    assert manifest["deployment"]["topologyReceipt"]["authentication"] == {
        "algorithm": "HMAC-SHA256",
        "keyId": TOPOLOGY_KEY_ID,
    }
    assert manifest["canonicalSqliteSource"]["quickCheck"] == "ok"
    assert manifest["canonicalSqliteSource"]["schemaVersion"] == 2
    assert manifest["canonicalSqliteSource"]["userVersion"] == 5
    assert manifest["canonicalSqliteSource"]["tableCounts"] == {
        "job decisions": 3,
        "jobs": 2,
    }
    assert len(manifest["canonicalSqliteSource"]["snapshotSha256"]) == 64
    assert manifest["canonicalSqliteSource"]["snapshotBytes"] > 0
    assert "signature" not in json.dumps(manifest)
    assert all(value not in json.dumps(manifest) for key, value in ATTESTATION_ENV.items() if key.endswith("_KEY_B64"))
    assert len(manifest["artifacts"]["files"]) == len(ARTIFACT_ROLES)
    assert manifest["security"]["nonceUniquenessVerified"] is False
    assert manifest["security"]["nonceReplayDefense"] == "external-durable-registry-required"
    assert manifest["verification"]["gitExecutable"]["purpose"] == "release-git"


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
    invalid_receipts.append(("command", bad_command, "approved command set"))
    bad_environment = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_environment["environment"] = {"python": "3.12"}
    invalid_receipts.append(("environment", bad_environment, "environment"))
    bad_time = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    bad_time["completedAt"] = _timestamp(datetime.now(timezone.utc) - timedelta(hours=1))
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


@pytest.mark.parametrize("attack", ["node", "npm_cli", "argv", "path"])
def test_brain_receipt_binds_pinned_node_npm_payload_and_execution_path(
    release_fixture: Fixture, attack: str
) -> None:
    fixture = release_fixture
    receipt = _test_receipt(fixture.brain_head, fixture.brain_tree, BRAIN_SUITE)
    command = receipt["commands"][0]
    if attack == "node":
        command["executable"]["path"] = str((fixture.root / "ambient-node.exe").resolve())
    elif attack == "npm_cli":
        command["dependencies"]["npmCli"]["path"] = str((fixture.root / "ambient-npm-cli.js").resolve())
    elif attack == "argv":
        command["argv"][1] = str((fixture.root / "ambient-npm-cli.js").resolve())
    else:
        command["executionEnvironment"]["PATH"] = str((fixture.root / "poisoned-path").resolve())
    path = fixture.root / f"brain-dependency-{attack}.json"
    _write_signed_receipt(path, receipt)

    result = _run(
        fixture.command_for(
            output=f"brain-dependency-{attack}-output.json",
            extra=["--brain-test-receipt", str(path)],
        )
    )

    assert result.returncode != 0
    assert "approved command set" in result.stderr
    assert not (fixture.root / f"brain-dependency-{attack}-output.json").exists()


@pytest.mark.parametrize(("command", "argv"), [("echo passed", ["echo", "passed"]), ("python --version", [sys.executable, "--version"])])
def test_successful_but_unapproved_commands_cannot_satisfy_release_suite(
    release_fixture: Fixture, command: str, argv: list[str]
) -> None:
    fixture = release_fixture
    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    receipt["commands"][0].update(command=command, argv=argv)
    path = fixture.root / "unapproved-command.json"
    _write_signed_receipt(path, receipt)
    result = _run(
        fixture.command_for(output="unapproved-command-output.json", extra=["--runtime-test-receipt", str(path)])
    )
    assert result.returncode != 0
    assert "approved command set" in result.stderr


@pytest.mark.parametrize("attack", ["unsigned", "wrong_key", "wrong_key_id", "tampered", "stale"])
def test_test_receipt_authentication_rejects_adversarial_claims(release_fixture: Fixture, attack: str) -> None:
    fixture = release_fixture
    path = fixture.root / f"test-receipt-{attack}.json"
    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    if attack == "stale":
        receipt["startedAt"] = _timestamp(datetime.now(timezone.utc) - timedelta(days=2, minutes=1))
        receipt["completedAt"] = _timestamp(datetime.now(timezone.utc) - timedelta(days=2))
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


def test_receipts_cannot_replay_across_release_nonce_or_future_clock(release_fixture: Fixture) -> None:
    fixture = release_fixture
    path = fixture.root / "replay.json"
    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    receipt["releaseNonce"] = "different_nonce_0123456789abcdef0123456789abcdef"
    _write_signed_receipt(path, receipt)
    replay = _run(
        fixture.command_for(output="replay-output.json", extra=["--runtime-test-receipt", str(path)])
    )
    assert replay.returncode != 0
    assert "release purpose, ID, and nonce" in replay.stderr

    receipt = _test_receipt(fixture.runtime_head, fixture.runtime_tree, RUNTIME_SUITE)
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    receipt["startedAt"] = _timestamp(future)
    for index, command in enumerate(receipt["commands"]):
        command["startedAt"] = _timestamp(future + timedelta(seconds=index * 2))
        command["completedAt"] = _timestamp(future + timedelta(seconds=index * 2 + 1))
    receipt["completedAt"] = receipt["commands"][-1]["completedAt"]
    _write_signed_receipt(path, receipt)
    future_result = _run(
        fixture.command_for(output="future-output.json", extra=["--runtime-test-receipt", str(path)])
    )
    assert future_result.returncode != 0
    assert "after manifest generation" in future_result.stderr


def test_generated_at_is_not_a_caller_controlled_argument(release_fixture: Fixture) -> None:
    command = release_fixture.command_for(output="caller-time.json")
    command.extend(["--generated-at", "2000-01-01T00:00:00Z"])
    result = _run(command)
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
    assert not (release_fixture.root / "caller-time.json").exists()


def test_receipt_purpose_keys_must_be_cryptographically_distinct(release_fixture: Fixture) -> None:
    env = {
        "APPLYPILOT_BRAIN_TEST_ATTESTATION_KEY_B64": ATTESTATION_ENV[
            "APPLYPILOT_RUNTIME_TEST_ATTESTATION_KEY_B64"
        ]
    }
    result = _run(release_fixture.command_for(output="reused-key.json"), env=env)
    assert result.returncode != 0
    assert "distinct keys and key IDs" in result.stderr


def test_manifest_strict_json_rejects_duplicate_keys_and_nonstandard_constants(release_fixture: Fixture) -> None:
    duplicate = release_fixture.root / "duplicate-template.json"
    duplicate.write_text('{"schemaVersion":"applypilot_compatibility_manifest_v4","schemaVersion":"x"}', encoding="utf-8")
    duplicate_result = _run(
        release_fixture.command_for(output="duplicate-output.json", template=duplicate)
    )
    assert duplicate_result.returncode != 0
    assert "duplicate object key" in duplicate_result.stderr

    nonstandard = release_fixture.root / "nan-template.json"
    nonstandard.write_text('{"schemaVersion":"applypilot_compatibility_manifest_v4","value":NaN}', encoding="utf-8")
    nan_result = _run(release_fixture.command_for(output="nan-output.json", template=nonstandard))
    assert nan_result.returncode != 0
    assert "non-standard JSON constant" in nan_result.stderr

    overflow = release_fixture.root / "overflow-template.json"
    overflow.write_text(
        '{"schemaVersion":"applypilot_compatibility_manifest_v4","value":1e999}', encoding="utf-8"
    )
    overflow_result = _run(release_fixture.command_for(output="overflow-output.json", template=overflow))
    assert overflow_result.returncode != 0
    assert "non-finite JSON number" in overflow_result.stderr


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("producer", "forged-producer", "producer"),
        ("status", "pending", "status"),
        ("commands", [], "observation commands"),
        ("releaseStage", "production", "environment"),
        ("observedRailwayEnvironmentName", "staging", "environment"),
        ("postgresServiceId", UUIDS["control_plane"], "deployment identity"),
        ("expiresAt", "2000-01-01T00:00:00Z", "stale"),
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

    env = {
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64": "",
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID": "",
    }
    result = _run(fixture.command_for(output="missing-key.json"), env=env)
    assert result.returncode != 0
    assert "environment variables are required" in result.stderr

    short_key_env = {
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64": base64.b64encode(b"too-short").decode(),
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_ID": TOPOLOGY_KEY_ID,
    }
    result = _run(fixture.command_for(output="short-key.json"), env=short_key_env)
    assert result.returncode != 0
    assert "at least 32 bytes" in result.stderr


def test_git_observations_receive_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    markers = {
        "APPLYPILOT_RELEASE_SECRET_MARKER": "release-secret",
        "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION_KEY_B64": "signing-secret",
        "RAILWAY_TOKEN": "railway-secret",
        "GITHUB_TOKEN": "verification-secret",
        "ROLLBACK_DATABASE_URL": "rollback-secret",
    }
    for name, value in markers.items():
        monkeypatch.setenv(name, value)
    executable = {
        "path": str((tmp_path / "git.exe").resolve()),
        "sha256": "5" * 64,
        "purpose": "release-git",
        "trustPolicy": "protected-handle-pre-post-exact-path-sha256-v2",
    }
    monkeypatch.setattr(module, "_trusted_executable", lambda _purpose: executable)

    @contextmanager
    def protected(_executable: dict[str, str]):
        yield {"path": executable["path"], "passFds": ()}

    monkeypatch.setattr(module, "_protected_executable_execution", protected)

    def observe(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        child = kwargs["env"]
        assert not set(markers).intersection(child)
        return subprocess.CompletedProcess(kwargs.get("args", _args[0]), 0, stdout="observed\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", observe)
    assert module._git(tmp_path, "rev-parse", "HEAD") == "observed"


def test_git_replace_refs_cannot_substitute_release_objects(release_fixture: Fixture) -> None:
    fixture = release_fixture
    original_tree = _git(fixture.runtime, "rev-parse", f"{fixture.runtime_head}^{{tree}}")
    _git(fixture.runtime, "replace", fixture.runtime_head, "rollback")

    output = fixture.root / "replace-ref.json"
    _run(fixture.command_for(output=output.name), check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))

    assert manifest["repositories"]["python"]["sourceCommitSha"] == fixture.runtime_head
    assert manifest["repositories"]["python"]["sourceTreeSha"] == original_tree


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


def test_canonical_sqlite_symlink_is_rejected_before_resolution(release_fixture: Fixture) -> None:
    fixture = release_fixture
    alias = fixture.source / "brain-alias.db"
    try:
        alias.symlink_to(fixture.sqlite)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    command = fixture.command_for(output="sqlite-symlink-output.json")
    command[command.index("--sqlite-source") + 1] = str(alias)

    result = _run(command)

    assert result.returncode != 0
    assert "symlink" in result.stderr or "reparse" in result.stderr
    assert not (fixture.root / "sqlite-symlink-output.json").exists()


def test_sqlite_live_wal_sidecars_are_rejected_before_immutable_read_omits_committed_data(
    release_fixture: Fixture,
) -> None:
    fixture = release_fixture
    live = fixture.source / "live-wal.db"
    writer = sqlite3.connect(live)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE committed_only_in_wal (value TEXT)")
        writer.execute("INSERT INTO committed_only_in_wal VALUES ('present')")
        writer.commit()
        assert Path(f"{live}-wal").exists()
        with sqlite3.connect(live) as normal_reader:
            assert normal_reader.execute("SELECT COUNT(*) FROM committed_only_in_wal").fetchone()[0] == 1
        immutable_uri = f"file:{live.as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(immutable_uri, uri=True) as immutable_reader:
            with pytest.raises(sqlite3.DatabaseError):
                immutable_reader.execute("SELECT COUNT(*) FROM committed_only_in_wal").fetchone()

        document = fixture.document()
        document["canonicalSqliteSource"] = {
            "path": str(live),
            "bytes": live.stat().st_size,
            "sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
        }
        template = fixture.write_template(document, "live-wal-template.json")
        command = fixture.command_for(output="live-wal-output.json", template=template)
        command[command.index("--sqlite-source") + 1] = str(live)
        result = _run(command)
        assert result.returncode != 0
        assert "live WAL/SHM sidecars" in result.stderr
        assert not (fixture.root / "live-wal-output.json").exists()
    finally:
        writer.close()


def test_sqlite_late_wal_created_during_final_source_hash_fails_closed(
    release_fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = release_fixture
    module = _load_module()
    original = module._stable_file_record
    source_calls = 0

    def create_late_sidecar(path: Path) -> tuple[int, str]:
        nonlocal source_calls
        result = original(path)
        if Path(path) == fixture.sqlite:
            source_calls += 1
            if source_calls == 2:
                Path(f"{fixture.sqlite}-wal").write_bytes(b"late-wal")
        return result

    monkeypatch.setattr(module, "_stable_file_record", create_late_sidecar)
    with pytest.raises(RuntimeError, match="during final source verification"):
        module._verify_sqlite_source(fixture.document(), fixture.sqlite)
    assert source_calls == 2


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


def test_partial_evidence_pair_fails_closed_and_complete_pair_is_immutable(release_fixture: Fixture) -> None:
    fixture = release_fixture
    output = fixture.root / "repair.json"
    command = fixture.command_for(output="repair.json")
    first = _run(command, check=True)
    result = json.loads(first.stdout)
    sidecar = output.with_name(output.name + ".sha256")
    sidecar.unlink()
    partial = _run(command)
    assert partial.returncode != 0
    assert "refusing to overwrite release evidence" in partial.stderr
    assert hashlib.sha256(output.read_bytes()).hexdigest() == result["sha256"]


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


@pytest.mark.parametrize(
    ("argument", "duplicate"),
    [
        ("--control-plane-service-id", UUIDS["postgres"]),
        ("--gateway-service-id", UUIDS["postgres"]),
        ("--gateway-service-id", UUIDS["control_plane"]),
    ],
)
def test_railway_service_ids_must_be_pairwise_distinct(
    release_fixture: Fixture, argument: str, duplicate: str
) -> None:
    command = release_fixture.command_for(output=f"duplicate-service-{argument[2:]}.json")
    command[command.index(argument) + 1] = duplicate
    result = _run(command)
    assert result.returncode != 0
    assert "service IDs must be pairwise distinct" in result.stderr


def test_expired_topology_receipt_publishes_no_manifest_or_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    output = tmp_path / "expired-at-publication.json"
    expires = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    manifest = {"deployment": {"topologyReceipt": {"expiresAt": _timestamp(expires)}}}

    class Parser:
        @staticmethod
        def parse_args() -> SimpleNamespace:
            return SimpleNamespace(output=output)

    monkeypatch.setattr(module, "_parser", lambda: Parser())
    monkeypatch.setattr(module, "build_manifest", lambda _args: manifest)
    monkeypatch.setattr(
        module,
        "_utc_now",
        lambda: expires + timedelta(milliseconds=1),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="expired before final publication"):
        module.main()
    assert not output.exists()
    assert not output.with_name(output.name + ".sha256").exists()


def _load_module():
    spec = importlib.util.spec_from_file_location("compatibility_manifest_generator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
