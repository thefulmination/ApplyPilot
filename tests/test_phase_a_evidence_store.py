from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "scripts" / "PhaseAEvidenceStore.psm1"
PROVISION = REPO_ROOT / "scripts" / "provision-phase-a-evidence-store.ps1"
NEW_RECEIPT = REPO_ROOT / "scripts" / "New-PhaseASignedReceipt.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _ps(value: os.PathLike[str] | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(body: str, *, check: bool = True, timeout: int = 45):
    encoded = base64.b64encode(
        ("$ErrorActionPreference='Stop'\n" + body).encode("utf-16-le")
    ).decode("ascii")
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        raise AssertionError(
            f"PowerShell failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
    return result


def _module(body: str, *, check: bool = True):
    return _run_ps(f"Import-Module {_ps(MODULE)} -Force\n{body}", check=check)


def _current_sid() -> str:
    return _run_ps("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").stdout.strip()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    # Fixtures contain only closed-schema ASCII strings, booleans, and arrays.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def _new_keypair(root: Path, name: str, key_size: int = 3072) -> tuple[Path, Path, str]:
    private = root / f"{name}.private.pem"
    public = root / f"{name}.public.der"
    _run_ps(
        f"$rsa=[Security.Cryptography.RSA]::Create({key_size})\n"
        f"[IO.File]::WriteAllText({_ps(private)},$rsa.ExportPkcs8PrivateKeyPem(),"
        "[Text.UTF8Encoding]::new($false))\n"
        f"[IO.File]::WriteAllBytes({_ps(public)},$rsa.ExportSubjectPublicKeyInfo())\n"
        "$rsa.Dispose()"
    )
    return private, public, _sha(public.read_bytes())


def _sign(private: Path, content: bytes) -> bytes:
    content_path = private.with_suffix(".content")
    signature_path = private.with_suffix(".sig")
    content_path.write_bytes(content)
    _run_ps(
        "$rsa=[Security.Cryptography.RSA]::Create()\n"
        f"$rsa.ImportFromPem([IO.File]::ReadAllText({_ps(private)}))\n"
        f"$bytes=[IO.File]::ReadAllBytes({_ps(content_path)})\n"
        "$sig=$rsa.SignData($bytes,[Security.Cryptography.HashAlgorithmName]::SHA256,"
        "[Security.Cryptography.RSASignaturePadding]::Pss)\n"
        f"[IO.File]::WriteAllBytes({_ps(signature_path)},$sig)\n"
        "$rsa.Dispose()"
    )
    return signature_path.read_bytes()


def _protect(path: Path, sid: str | None = None) -> None:
    sid = sid or _current_sid()
    _run_ps(
        f"$p={_ps(path)}\n"
        "$acl=[Security.AccessControl.DirectorySecurity]::new()\n"
        "$acl.SetAccessRuleProtection($true,$false)\n"
        f"$owner=[Security.Principal.SecurityIdentifier]::new({_ps(sid)})\n"
        "$acl.SetOwner($owner)\n"
        "foreach($s in @($owner,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),"
        "[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){"
        "$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($s,'FullControl',"
        "'ContainerInherit,ObjectInherit','None','Allow'))}\n"
        "Set-Acl -LiteralPath $p -AclObject $acl"
    )


def _test_store(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    for name in ("bundles", "adjudications", "operations"):
        (root / name).mkdir()
        _protect(root / name)
    config = b"{}"
    (root / "store.json").write_bytes(config)
    return root, _sha(config)


def _receipt(receipt_type: str, key_hash: str, **overrides):
    value = {
        "schema": "applypilot.phase-a.signed-receipt.v1",
        "receiptType": receipt_type,
        "commit": "d3a08bf9fc7a9fa8b920c3e845a0ab978ab6cf57",
        "signingKeySpkiSha256": key_hash,
        "operationId": str(uuid.uuid4()),
        "targetDigest": "1" * 64,
        "operatorSidDigest": "2" * 64,
        "machineDigest": "3" * 64,
        "storeConfigSha256": "4" * 64,
        "manifestBeforeSha256": "7" * 64,
        "manifestAfterSha256": "8" * 64,
    }
    if receipt_type in {
        "source-approval", "adjudication", "credential-revocation",
        "operation-authorization", "operation-completion",
    }:
        value["hostProvisioningReceiptSha256"] = "5" * 64
    if receipt_type in {"adjudication", "operation-authorization", "operation-completion"}:
        value["sourceApprovalReceiptSha256"] = "6" * 64
    value.update(overrides)
    return value


def _validation_args(receipt: dict[str, str], **overrides: str) -> str:
    pairs = {
        "ExpectedSigningSpkiSha256": receipt["signingKeySpkiSha256"],
        "ExpectedReceiptType": receipt["receiptType"],
        "ExpectedCommit": receipt["commit"],
        "ExpectedOperationId": receipt["operationId"],
        "ExpectedTargetDigest": receipt["targetDigest"],
        "ExpectedOperatorSidDigest": receipt["operatorSidDigest"],
        "ExpectedMachineDigest": receipt["machineDigest"],
        "ExpectedStoreConfigSha256": receipt["storeConfigSha256"],
        "ExpectedManifestBeforeSha256": receipt["manifestBeforeSha256"],
        "ExpectedManifestAfterSha256": receipt["manifestAfterSha256"],
    }
    if "hostProvisioningReceiptSha256" in receipt:
        pairs["ExpectedHostProvisioningReceiptSha256"] = receipt["hostProvisioningReceiptSha256"]
    if "sourceApprovalReceiptSha256" in receipt:
        pairs["ExpectedSourceApprovalReceiptSha256"] = receipt["sourceApprovalReceiptSha256"]
    pairs.update(overrides)
    return " ".join(f"-{name} {_ps(value)}" for name, value in pairs.items())


def _generator_args(receipt: dict[str, str], public: Path) -> str:
    names = {
        "ReceiptType": receipt["receiptType"], "Commit": receipt["commit"],
        "SigningSpkiPath": str(public),
        "ExpectedSigningSpkiSha256": receipt["signingKeySpkiSha256"],
        "OperationId": receipt["operationId"], "TargetDigest": receipt["targetDigest"],
        "OperatorSidDigest": receipt["operatorSidDigest"], "MachineDigest": receipt["machineDigest"],
        "StoreConfigSha256": receipt["storeConfigSha256"],
        "ManifestBeforeSha256": receipt["manifestBeforeSha256"],
        "ManifestAfterSha256": receipt["manifestAfterSha256"],
    }
    if "hostProvisioningReceiptSha256" in receipt:
        names["HostProvisioningReceiptSha256"] = receipt["hostProvisioningReceiptSha256"]
    if "sourceApprovalReceiptSha256" in receipt:
        names["SourceApprovalReceiptSha256"] = receipt["sourceApprovalReceiptSha256"]
    return " ".join(f"-{name} {_ps(value)}" for name, value in names.items())


def _preprovisioned_store(tmp_path: Path) -> dict[str, object]:
    base = tmp_path / "fixture"
    base.mkdir(parents=True)
    _protect(base)
    signing_private, signing_public, signing_hash = _new_keypair(base, "signing")
    recovery_private, recovery_public, recovery_hash = _new_keypair(base, "recovery")
    root = base / "v1"
    root.mkdir()
    _protect(root)
    for name in ("bundles", "adjudications", "operations"):
        (root / name).mkdir()
        _protect(root / name)
    sid = _current_sid()
    target = _module(f"Get-PhaseATargetDigest -Path {_ps(root)}").stdout.strip()
    operator = _sha(b"applypilot.phase-a.operator-sid.v1\0" + sid.encode("ascii"))
    machine_guid = "01234567-89ab-cdef-0123-456789abcdef"
    smbios_uuid = "fedcba98-7654-3210-fedc-ba9876543210"
    machine = _sha(
        b"applypilot.phase-a.machine.v1\0"
        + uuid.UUID(machine_guid).bytes_le
        + uuid.UUID(smbios_uuid).bytes_le
    )
    security = _module(f"Get-PhaseASecurityDescriptorHash -Path {_ps(root)}").stdout.strip()
    commit = "d3a08bf9fc7a9fa8b920c3e845a0ab978ab6cf57"
    config = {
        "schema": "applypilot.phase-a.evidence-store.v1",
        "approvedCommit": commit,
        "targetDigest": target,
        "operatorSidDigest": operator,
        "machineDigest": machine,
        "securityDescriptorSha256": security,
        "signingSpkiSha256": signing_hash,
        "recoverySigningSpkiSha256": recovery_hash,
    }
    config_bytes = _canonical(config)
    (root / "store.json").write_bytes(config_bytes)
    _protect(root / "store.json")
    config_hash = _sha(config_bytes)
    operation = str(uuid.uuid4())
    host = _receipt(
        "host-provisioning",
        recovery_hash,
        commit=commit,
        operationId=operation,
        targetDigest=target,
        operatorSidDigest=operator,
        machineDigest=machine,
        storeConfigSha256=config_hash,
    )
    host_bytes = _canonical(host)
    host_path = root / "operations" / f"{_sha(host_bytes)}.json"
    host_path.write_bytes(host_bytes)
    host_path.with_suffix(".sig").write_bytes(_sign(recovery_private, host_bytes))
    _protect(host_path)
    _protect(host_path.with_suffix(".sig"))
    return {
        "root": root, "sid": sid, "target": target, "operator": operator,
        "machine": machine, "machine_guid": machine_guid, "smbios_uuid": smbios_uuid,
        "commit": commit, "config_hash": config_hash, "operation": operation,
        "before": host["manifestBeforeSha256"], "after": host["manifestAfterSha256"],
        "signing_private": signing_private, "signing_public": signing_public,
        "signing_hash": signing_hash, "recovery_private": recovery_private,
        "recovery_public": recovery_public, "recovery_hash": recovery_hash,
        "host_path": host_path, "host_hash": host_path.stem,
    }


def _assert_store_body(fixture: dict[str, object]) -> str:
    return (
        f"Assert-PhaseAEvidenceStore -StoreRoot {_ps(fixture['root'])} "
        f"-CanonicalOperatorSid {_ps(fixture['sid'])} -ExpectedCommit {_ps(fixture['commit'])} "
        f"-SigningSpkiPath {_ps(fixture['signing_public'])} "
        f"-RecoverySigningSpkiPath {_ps(fixture['recovery_public'])} "
        f"-SigningSpkiSha256 {_ps(fixture['signing_hash'])} "
        f"-RecoverySigningSpkiSha256 {_ps(fixture['recovery_hash'])} "
        f"-CustodyOperationId {_ps(fixture['operation'])} "
        f"-CustodyManifestBeforeSha256 {_ps(fixture['before'])} "
        f"-CustodyManifestAfterSha256 {_ps(fixture['after'])} "
        f"-ExpectedTargetDigest {_ps(fixture['target'])} "
        f"-ExpectedMachineDigest {_ps(fixture['machine'])} -AncestorBoundary {_ps(fixture['root'].parent)} "
        "-DefinitionImport | ConvertTo-Json -Compress"
    )


def _assert_store(fixture: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return _module(_assert_store_body(fixture))


def _cleanup_fixture(tmp_path: Path) -> dict[str, object]:
    private, public, key_hash = _new_keypair(tmp_path, "cleanup")
    parent = tmp_path / "evidence"
    parent.mkdir()
    stage = parent / f".provisioning-{uuid.uuid4()}"
    (stage / "nested").mkdir(parents=True)
    (stage / "nested" / "one.txt").write_text("one", encoding="utf-8")
    (stage / "two.txt").write_text("two", encoding="utf-8")
    bootstrap = tmp_path / "bootstrap-operations"
    bootstrap.mkdir()
    _protect(bootstrap)
    before = json.loads(
        _module(f"Get-PhaseADirectoryManifest -Root {_ps(parent)} | ConvertTo-Json -Depth 16 -Compress").stdout
    )
    before_bytes = _canonical(before)
    stage_name = stage.name
    after = {
        "schema": "applypilot.phase-a.directory-manifest.v1",
        "entries": [
            entry for entry in before["entries"]
            if entry["relativePath"] != stage_name
            and not entry["relativePath"].startswith(stage_name + "/")
        ],
    }
    after_bytes = _canonical(after)
    after_path = tmp_path / "expected-after.json"
    after_path.write_bytes(after_bytes)
    target = _module(f"Get-PhaseATargetDigest -Path {_ps(stage)}").stdout.strip()
    values = {
        "commit": "d3a08bf9fc7a9fa8b920c3e845a0ab978ab6cf57",
        "operation": str(uuid.uuid4()), "target": target,
        "operator": "2" * 64, "machine": "3" * 64, "store": "4" * 64,
        "host": "5" * 64, "source": "6" * 64,
        "before": _sha(before_bytes), "after": _sha(after_bytes),
    }
    paths = {}
    for receipt_type in ("operation-authorization", "operation-completion"):
        receipt = _receipt(
            receipt_type, key_hash, commit=values["commit"], operationId=values["operation"],
            targetDigest=target, operatorSidDigest=values["operator"], machineDigest=values["machine"],
            storeConfigSha256=values["store"], hostProvisioningReceiptSha256=values["host"],
            sourceApprovalReceiptSha256=values["source"], manifestBeforeSha256=values["before"],
            manifestAfterSha256=values["after"],
        )
        data = _canonical(receipt)
        path = bootstrap / f"{_sha(data)}.json"
        path.write_bytes(data)
        path.with_suffix(".sig").write_bytes(_sign(private, data))
        _protect(path)
        _protect(path.with_suffix(".sig"))
        paths[receipt_type] = path
    return {
        "stage": stage, "parent": parent, "bootstrap": bootstrap, "after_path": after_path,
        "public": public, "key_hash": key_hash, **values,
        "authorization": paths["operation-authorization"],
        "completion": paths["operation-completion"],
    }


def _cleanup_body(f: dict[str, object], extra: str = "") -> str:
    return (
        f". {_ps(PROVISION)} -DefinitionImport; Invoke-PhaseAProvisioningCleanup "
        f"-StagingPath {_ps(f['stage'])} -CanonicalOperatorSid {_ps(_current_sid())} "
        f"-RecoverySigningSpkiPath {_ps(f['public'])} -RecoverySigningSpkiSha256 {_ps(f['key_hash'])} "
        f"-ExpectedCommit {_ps(f['commit'])} -ExpectedOperationId {_ps(f['operation'])} "
        f"-ExpectedTargetDigest {_ps(f['target'])} -ExpectedOperatorSidDigest {_ps(f['operator'])} "
        f"-ExpectedMachineDigest {_ps(f['machine'])} -ExpectedStoreConfigSha256 {_ps(f['store'])} "
        f"-ExpectedHostProvisioningReceiptSha256 {_ps(f['host'])} "
        f"-ExpectedSourceApprovalReceiptSha256 {_ps(f['source'])} "
        f"-ExpectedManifestBeforeSha256 {_ps(f['before'])} -ExpectedManifestAfterSha256 {_ps(f['after'])} "
        f"-AuthorizationReceiptPath {_ps(f['authorization'])} "
        f"-AuthorizationSignaturePath {_ps(f['authorization'].with_suffix('.sig'))} "
        f"-CompletionReceiptPath {_ps(f['completion'])} "
        f"-CompletionSignaturePath {_ps(f['completion'].with_suffix('.sig'))} "
        f"-ExpectedAfterManifestPath {_ps(f['after_path'])} -TestBootstrapRoot {_ps(f['bootstrap'])} {extra}"
    )


def _provision_body(tmp_path: Path, mode: str, evidence_base: Path | None = None) -> tuple[str, Path]:
    signing_private, signing_public, signing_hash = _new_keypair(tmp_path, "provision-signing")
    recovery_private, recovery_public, recovery_hash = _new_keypair(tmp_path, "provision-recovery")
    base = evidence_base or (tmp_path / "evidence")
    base.mkdir()
    _protect(base)
    final = base / "v1"
    material = tmp_path / "material"
    operation = str(uuid.uuid4())
    callback = (
        "$materializer={param($ctx) "
        f"$output={_ps(material)};"
        f"$receipt=& {_ps(NEW_RECEIPT)} -ReceiptType host-provisioning -Commit $ctx.Commit "
        f"-SigningSpkiPath {_ps(recovery_public)} -ExpectedSigningSpkiSha256 $ctx.RecoverySigningSpkiSha256 "
        "-OperationId $ctx.OperationId -TargetDigest $ctx.TargetDigest -OperatorSidDigest $ctx.OperatorSidDigest "
        "-MachineDigest $ctx.MachineDigest -StoreConfigSha256 $ctx.StoreConfigSha256 "
        "-ManifestBeforeSha256 $ctx.ManifestBeforeSha256 -ManifestAfterSha256 $ctx.ManifestAfterSha256 "
        "-CreateUnsigned -OutputDirectory $output;"
        f"$rsa=[Security.Cryptography.RSA]::Create();$rsa.ImportFromPem([IO.File]::ReadAllText({_ps(recovery_private)}));"
        "$bytes=[IO.File]::ReadAllBytes($receipt);"
        "$signature=$rsa.SignData($bytes,[Security.Cryptography.HashAlgorithmName]::SHA256,"
        "[Security.Cryptography.RSASignaturePadding]::Pss);$rsa.Dispose();"
        "$sig=[IO.Path]::ChangeExtension($receipt,'sig');[IO.File]::WriteAllBytes($sig,$signature);"
        + ("[IO.File]::WriteAllBytes($sig,[byte[]]::new(384));" if mode == "invalid" else "")
        + "[pscustomobject]@{ReceiptPath=$receipt;SignaturePath=$sig}};"
    )
    invoke = (
        f". {_ps(PROVISION)} -DefinitionImport;{callback}"
        f"Invoke-PhaseAEvidenceStoreProvision -StoreRoot {_ps(final)} -CanonicalOperatorSid {_ps(_current_sid())} "
        "-ExpectedCommit d3a08bf9fc7a9fa8b920c3e845a0ab978ab6cf57 "
        f"-SigningSpkiPath {_ps(signing_public)} -RecoverySigningSpkiPath {_ps(recovery_public)} "
        f"-SigningSpkiSha256 {signing_hash} -RecoverySigningSpkiSha256 {recovery_hash} "
        f"-CustodyOperationId {operation} -CustodyManifestBeforeSha256 {'7' * 64} "
        f"-CustodyManifestAfterSha256 {'8' * 64} -HostReceiptMaterializer $materializer "
        "-TestMachineGuid 01234567-89ab-cdef-0123-456789abcdef "
        "-TestSmbiosUuid fedcba98-7654-3210-fedc-ba9876543210 "
        f"-TestAncestorBoundary {_ps(base)} -DefinitionImport "
        + ("-CrashBeforePublication " if mode == "crash" else "")
    )
    return invoke, final


def test_owned_files_and_exported_surface_exist():
    assert PROVISION.is_file()
    assert NEW_RECEIPT.is_file()
    result = _module(
        "(Get-Module PhaseAEvidenceStore).ExportedCommands.Keys|Sort-Object|ConvertTo-Json -Compress"
    )
    assert json.loads(result.stdout) == sorted(
        [
            "Assert-PhaseAEvidenceStore",
            "Get-PhaseADirectoryManifest",
            "Get-PhaseAMachineDigest",
            "Get-PhaseAOperatorSidDigest",
            "Get-PhaseASecurityDescriptorHash",
            "Get-PhaseATargetDigest",
            "Install-PhaseASignedReceipt",
            "Test-PhaseASignedReceipt",
        ]
    )


def test_review_corrections_are_structural_contracts():
    module = MODULE.read_text(encoding="utf-8")
    provision = PROVISION.read_text(encoding="utf-8")
    generator = NEW_RECEIPT.read_text(encoding="utf-8")

    assert "$env:ProgramData" not in module + provision
    assert "[Environment]::GetFolderPath" in module + provision
    assert "Remove-Item -LiteralPath $stage -Recurse" not in provision
    assert "[IO.File]::ReadAllBytes" not in module + generator
    assert "[Parameter(Mandatory)][string]$ExpectedCommit" in module
    assert "[Parameter(Mandatory)][string]$StoreRoot" not in provision.split(
        "function Invoke-PhaseAEvidenceStoreProvision", 1
    )[0]
    assert "[Parameter(Mandatory)][string]$InputPath" not in generator
    assert "[ValidateSet('source-approval'" in generator
    assert "DestinationDirectory" not in "\n".join(
        line for line in module.splitlines() if "Install-PhaseASignedReceipt" in line or "param(" in line
    )


def test_production_entrypoint_has_no_root_override():
    result = _run_ps(
        f"try {{ & {_ps(PROVISION)} -StoreRoot 'C:\\redirected' -CanonicalOperatorSid {_ps(_current_sid())}; "
        "'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"


def test_operator_digest_domain_and_current_token_must_match():
    sid = _current_sid()
    expected = _sha(b"applypilot.phase-a.operator-sid.v1\0" + sid.encode("ascii"))
    result = _module(
        f"Get-PhaseAOperatorSidDigest -CanonicalOperatorSid {_ps(sid)}"
    )
    assert result.stdout.strip() == expected
    wrong = _module(
        "try { Get-PhaseAOperatorSidDigest -CanonicalOperatorSid 'S-1-5-18'; 'missed' } "
        "catch { 'rejected' }"
    )
    assert wrong.stdout.strip() == "rejected"


def test_machine_digest_exact_contract_and_invalid_identity_rejected():
    machine = "01234567-89ab-cdef-0123-456789abcdef"
    smbios = "fedcba98-7654-3210-fedc-ba9876543210"
    expected_input = (
        b"applypilot.phase-a.machine.v1\0"
        + uuid.UUID(machine).bytes_le
        + uuid.UUID(smbios).bytes_le
    )
    result = _module(
        f"Get-PhaseAMachineDigest -MachineGuid {_ps(machine)} -SmbiosUuid {_ps(smbios)} "
        "-DefinitionImport"
    )
    assert result.stdout.strip() == _sha(expected_input)
    for bad in ["not-a-guid", "00000000-0000-0000-0000-000000000000"]:
        result = _module(
            f"try {{ Get-PhaseAMachineDigest -MachineGuid {_ps(bad)} "
            f"-SmbiosUuid {_ps(smbios)} -DefinitionImport; 'missed' }} catch {{ 'rejected' }}"
        )
        assert result.stdout.strip() == "rejected"


def test_canonical_json_rejects_duplicate_unknown_and_noncanonical_receipts(tmp_path: Path):
    _, public, key_hash = _new_keypair(tmp_path, "key")
    base = _receipt("source-approval", key_hash)
    good = _canonical(base)
    good_path = tmp_path / f"{_sha(good)}.json"
    good_path.write_bytes(good)
    sig_path = good_path.with_suffix(".sig")
    sig_path.write_bytes(b"x" * 384)
    for text in [
        good.decode().replace("{", '{"unknown":true,', 1),
        good.decode().replace('"schema":', '"schema":"x","schema":', 1),
        json.dumps(base, indent=2),
    ]:
        candidate = tmp_path / f"{_sha(text.encode())}.json"
        candidate.write_text(text, encoding="utf-8")
        candidate.with_suffix(".sig").write_bytes(b"x" * 384)
        result = _module(
            f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(candidate)} "
            f"-SignaturePath {_ps(candidate.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)} "
            f"{_validation_args(base)}; "
            "'missed' } catch { 'rejected' }"
        )
        assert result.stdout.strip() == "rejected"


def test_signed_receipt_signature_filename_and_binding_validation(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "recovery")
    receipt = _receipt("operation-authorization", key_hash)
    content = _canonical(receipt)
    receipt_path = tmp_path / f"{_sha(content)}.json"
    receipt_path.write_bytes(content)
    sig_path = receipt_path.with_suffix(".sig")
    sig_path.write_bytes(_sign(private, content))
    result = _module(
        f"Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
        f"-SignaturePath {_ps(sig_path)} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(receipt)}"
    )
    assert result.stdout.strip() == "True"
    missing_commit = _validation_args(receipt).replace(
        f"-ExpectedCommit {_ps(receipt['commit'])}", ""
    )
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
        f"-SignaturePath {_ps(sig_path)} -SigningSpkiPath {_ps(public)} {missing_commit}; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    for parameter, forged in [
        ("ExpectedOperationId", str(uuid.uuid4())),
        ("ExpectedTargetDigest", "a" * 64),
        ("ExpectedManifestAfterSha256", "b" * 64),
        ("ExpectedHostProvisioningReceiptSha256", "c" * 64),
        ("ExpectedStoreConfigSha256", "d" * 64),
        ("ExpectedSourceApprovalReceiptSha256", "e" * 64),
    ]:
        result = _module(
            f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
            f"-SignaturePath {_ps(sig_path)} -SigningSpkiPath {_ps(public)} "
            f"{_validation_args(receipt, **{parameter: forged})}; 'missed' }} catch {{ 'rejected' }}"
        )
        assert result.stdout.strip() == "rejected"
    mismatch = tmp_path / ("f" * 64 + ".json")
    mismatch.write_bytes(content)
    mismatch.with_suffix(".sig").write_bytes(sig_path.read_bytes())
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(mismatch)} "
        f"-SignaturePath {_ps(mismatch.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(receipt)}; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"


def test_wrong_signing_key_and_malformed_digest_fail_closed(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "correct")
    _, wrong_public, _ = _new_keypair(tmp_path, "wrong")
    receipt = _receipt("adjudication", key_hash)
    content = _canonical(receipt)
    path = tmp_path / f"{_sha(content)}.json"
    path.write_bytes(content)
    path.with_suffix(".sig").write_bytes(_sign(private, content))
    result = _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(path)} "
        f"-SignaturePath {_ps(path.with_suffix('.sig'))} -SigningSpkiPath {_ps(wrong_public)} "
        f"{_validation_args(receipt)}; "
        "'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"

    malformed = _receipt("adjudication", key_hash, manifestAfterSha256="A" * 64)
    malformed_bytes = _canonical(malformed)
    malformed_path = tmp_path / f"{_sha(malformed_bytes)}.json"
    malformed_path.write_bytes(malformed_bytes)
    malformed_path.with_suffix(".sig").write_bytes(_sign(private, malformed_bytes))
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(malformed_path)} "
        f"-SignaturePath {_ps(malformed_path.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(malformed)}; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"


def test_spki_replacement_cannot_split_hash_import_and_signature_key(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "receipt-key")
    _, replacement, replacement_hash = _new_keypair(tmp_path, "anchor-key")
    receipt = _receipt("operation-authorization", key_hash)
    content = _canonical(receipt)
    receipt_path = tmp_path / f"{_sha(content)}.json"
    receipt_path.write_bytes(content)
    receipt_path.with_suffix(".sig").write_bytes(_sign(private, content))
    moved = tmp_path / "original-key.der"
    marker = tmp_path / "race-entered.txt"
    split_args = _validation_args(
        receipt, ExpectedSigningSpkiSha256=replacement_hash
    )
    command = (
        f"$swap={{param($path) Set-Content -LiteralPath {_ps(marker)} -Value entered;"
        f"Move-Item -LiteralPath $path -Destination {_ps(moved)};"
        f"Copy-Item -LiteralPath {_ps(replacement)} -Destination $path}};"
        "try { Test-PhaseASignedReceipt "
        f"-ReceiptPath {_ps(receipt_path)} -SignaturePath {_ps(receipt_path.with_suffix('.sig'))} "
        f"-SigningSpkiPath {_ps(public)} {split_args} "
        "-BeforeSpkiRevalidation $swap -DefinitionImport; 'missed' } catch { 'rejected' }"
    )
    result = _module(command)
    assert marker.is_file(), "race hook must execute between held-byte read and final identity check"
    assert result.stdout.strip() == "rejected"
    assert public.is_file()


@pytest.mark.parametrize("key_form", ["rsa-2048", "trailing-der"])
def test_spki_requires_exact_rsa3072_canonical_der(tmp_path: Path, key_form: str):
    key_size = 2048 if key_form == "rsa-2048" else 3072
    private, public, _ = _new_keypair(tmp_path, key_form, key_size)
    if key_form == "trailing-der":
        public.write_bytes(public.read_bytes() + b"\x00")
    key_hash = _sha(public.read_bytes())
    receipt = _receipt("host-provisioning", key_hash)
    content = _canonical(receipt)
    receipt_path = tmp_path / f"{_sha(content)}.json"
    receipt_path.write_bytes(content)
    receipt_path.with_suffix(".sig").write_bytes(_sign(private, content))
    result = _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
        f"-SignaturePath {_ps(receipt_path.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(receipt)}; 'missed' }} catch {{ 'rejected' }}"
    )
    assert result.stdout.strip() == "rejected"


def test_receipt_generator_never_accepts_private_key_and_verifies_returned_signature(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "offhost")
    inputs = _receipt("credential-revocation", key_hash)
    unsigned = tmp_path / "unsigned"
    result = _run_ps(
        f"& {_ps(NEW_RECEIPT)} {_generator_args(inputs, public)} "
        f"-OutputDirectory {_ps(unsigned)} -CreateUnsigned"
    )
    unsigned_path = Path(result.stdout.strip())
    assert unsigned_path.name == _sha(unsigned_path.read_bytes()) + ".json"
    assert "private" not in NEW_RECEIPT.read_text(encoding="utf-8").lower()
    signature = unsigned_path.with_suffix(".sig")
    signature.write_bytes(_sign(private, unsigned_path.read_bytes()))
    verified = _run_ps(
        f"& {_ps(NEW_RECEIPT)} {_generator_args(inputs, public)} -ReceiptPath {_ps(unsigned_path)} "
        f"-SignaturePath {_ps(signature)} -VerifyReturnedSignature"
    )
    assert verified.stdout.strip() == str(unsigned_path)


def test_generator_constructs_all_six_closed_schemas(tmp_path: Path):
    _, public, key_hash = _new_keypair(tmp_path, "schemas")
    expected_fields = {
        "host-provisioning": 11,
        "source-approval": 12,
        "adjudication": 13,
        "credential-revocation": 12,
        "operation-authorization": 13,
        "operation-completion": 13,
    }
    for receipt_type, field_count in expected_fields.items():
        receipt = _receipt(receipt_type, key_hash)
        output = tmp_path / receipt_type
        result = _run_ps(
            f"& {_ps(NEW_RECEIPT)} {_generator_args(receipt, public)} "
            f"-OutputDirectory {_ps(output)} -CreateUnsigned"
        )
        path = Path(result.stdout.strip())
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert len(payload) == field_count
        assert payload == receipt


def test_nonadjacent_pair_and_non_32_byte_pss_salt_rejected(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "pss")
    receipt = _receipt("host-provisioning", key_hash)
    content = _canonical(receipt)
    receipt_path = tmp_path / f"{_sha(content)}.json"
    receipt_path.write_bytes(content)
    other = tmp_path / "other"
    other.mkdir()
    nonadjacent = other / f"{_sha(content)}.sig"
    nonadjacent.write_bytes(_sign(private, content))
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
        f"-SignaturePath {_ps(nonadjacent)} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(receipt)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"

    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    wrong_salt = key.sign(
        content,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=20),
        hashes.SHA256(),
    )
    signature = receipt_path.with_suffix(".sig")
    signature.write_bytes(wrong_salt)
    assert len(wrong_salt) == 384
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt_path)} "
        f"-SignaturePath {_ps(signature)} -SigningSpkiPath {_ps(public)} "
        f"{_validation_args(receipt)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"


@pytest.mark.parametrize(
    "boundary",
    [
        "after-receipt-stage",
        "after-signature-stage",
        "after-receipt-rename",
        "after-signature-rename",
        "before-pair-revalidation",
    ],
)
def test_install_receipt_crash_boundaries_never_overwrite(tmp_path: Path, boundary: str):
    private, public, key_hash = _new_keypair(tmp_path, boundary)
    store, config_hash = _test_store(tmp_path / "store")
    destination = store / "operations"
    receipt = _receipt("operation-completion", key_hash, storeConfigSha256=config_hash)
    content = _canonical(receipt)
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    signature = source.with_suffix(".sig")
    signature.write_bytes(_sign(private, content))
    result = _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(signature)} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)} -DefinitionImport "
        f"-CrashAfter {_ps(boundary)}; 'missed' }} "
        "catch { 'crashed' }"
    )
    assert result.stdout.strip() == "crashed"
    final_receipt = destination / source.name
    final_signature = destination / signature.name
    before = tuple(p.read_bytes() if p.exists() else None for p in (final_receipt, final_signature))
    retry = _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(signature)} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)} -DefinitionImport | Out-Null; "
        "'installed' } catch { 'rejected:' + $_.Exception.Message }"
    ).stdout.strip()
    if (before[0] is None and before[1] is None) or (before[0] is not None and before[1] is not None):
        assert retry == "installed", retry
    else:
        assert retry.startswith("rejected:"), retry
        assert before == tuple(p.read_bytes() if p.exists() else None for p in (final_receipt, final_signature))


def test_install_rejects_unprotected_acl_and_orphan_half_pair(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "install")
    store, config_hash = _test_store(tmp_path / "store")
    destination = store / "operations"
    receipt = _receipt("operation-completion", key_hash, storeConfigSha256=config_hash)
    content = _canonical(receipt)
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    source.with_suffix(".sig").write_bytes(_sign(private, content))
    _run_ps(f"& icacls.exe {_ps(destination)} /inheritance:e | Out-Null")
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)} -DefinitionImport; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    _protect(destination)
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt, ExpectedTargetDigest='f'*64)} "
        "-DefinitionImport; 'missed' } "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert not list(destination.iterdir())
    (destination / source.name).write_bytes(content)
    before = (destination / source.name).read_bytes()
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)} -DefinitionImport; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert (destination / source.name).read_bytes() == before


def test_install_detects_final_receipt_replacement_race(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "replace")
    store, config_hash = _test_store(tmp_path / "store")
    receipt = _receipt("operation-completion", key_hash, storeConfigSha256=config_hash)
    content = _canonical(receipt)
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    signature = source.with_suffix(".sig")
    signature.write_bytes(_sign(private, content))
    result = _module(
        "$replace={param($pair) $bytes=[IO.File]::ReadAllBytes($pair.ReceiptPath);"
        "Remove-Item -LiteralPath $pair.ReceiptPath -Force;"
        "[IO.File]::WriteAllBytes($pair.ReceiptPath,$bytes)};"
        "try { Install-PhaseASignedReceipt "
        f"-ReceiptPath {_ps(source)} -SignaturePath {_ps(signature)} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)} -DefinitionImport "
        "-BeforeFinalPairRevalidation $replace; 'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"


def test_install_rejects_arbitrary_same_named_destination(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "destination")
    store, config_hash = _test_store(tmp_path / "arbitrary" / "v1")
    receipt = _receipt("operation-completion", key_hash, storeConfigSha256=config_hash)
    content = _canonical(receipt)
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    source.with_suffix(".sig").write_bytes(_sign(private, content))
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -StoreRoot {_ps(store)} "
        f"-SigningSpkiPath {_ps(public)} {_validation_args(receipt)}; 'missed' }} "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert not list((store / "operations").iterdir())


def test_unc_nonfixed_and_reparse_inputs_are_rejected(tmp_path: Path):
    assert _module(
        "try { Get-PhaseATargetDigest -Path '\\\\localhost\\C$\\ProgramData'; 'missed' } "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert _module(
        "try { Get-PhaseATargetDigest -Path 'A:\\phase-a-evidence'; 'missed' } "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert _module(
        f"$m=Get-Module PhaseAEvidenceStore;try {{ & $m {{param($p) Assert-PhaseALocalNtfsPath "
        f"$p -DefinitionDriveFormat FAT32 -DefinitionImport}} {_ps(tmp_path)}; 'missed' }} "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert _module(
        f"try {{ Get-PhaseATargetDigest -Path {_ps(junction)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"


def test_assert_store_is_statically_non_mutating():
    body = _run_ps(
        f"Import-Module {_ps(MODULE)} -Force;"
        "(Get-Command Assert-PhaseAEvidenceStore).ScriptBlock.Ast.Extent.Text"
    ).stdout
    for mutator in ["Set-Acl", "Remove-Item", "New-Item", "Move-Item", "Rename-"]:
        assert mutator not in body


def test_directory_manifest_is_path_redacted_sorted_and_rejects_reparse(tmp_path: Path):
    root = tmp_path / "manifest"
    root.mkdir()
    (root / "b").mkdir()
    (root / "b" / "two.txt").write_bytes(b"two")
    (root / "one.txt").write_bytes(b"one")
    result = _module(
        f"Get-PhaseADirectoryManifest -Root {_ps(root)} | ConvertTo-Json -Depth 8 -Compress"
    )
    manifest = json.loads(result.stdout)
    encoded = json.dumps(manifest)
    assert str(tmp_path) not in encoded
    assert [entry["relativePath"] for entry in manifest["entries"]] == ["b", "b/two.txt", "one.txt"]
    os.symlink(root / "one.txt", root / "link.txt")
    assert _module(
        f"try {{ Get-PhaseADirectoryManifest -Root {_ps(root)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"


def test_valid_preprovisioned_store_passes_full_validator(tmp_path: Path):
    fixture = _preprovisioned_store(tmp_path)
    payload = json.loads(_assert_store(fixture).stdout)
    assert payload["Valid"] is True
    assert payload["HostProvisioningReceiptSha256"] == fixture["host_hash"]


@pytest.mark.parametrize("mutation", ["unexpected-root", "store-acl", "missing-pair", "duplicate-host", "reparse", "duplicate-ace", "hardlink"])
def test_full_store_tree_and_acl_mutations_fail_closed(tmp_path: Path, mutation: str):
    fixture = _preprovisioned_store(tmp_path)
    root = fixture["root"]
    if mutation == "unexpected-root":
        (root / "extra.txt").write_text("unexpected", encoding="utf-8")
    elif mutation == "store-acl":
        _run_ps(f"& icacls.exe {_ps(root / 'store.json')} /inheritance:e | Out-Null")
    elif mutation == "missing-pair":
        (fixture["host_path"].with_suffix(".sig")).unlink()
    elif mutation == "duplicate-host":
        host = json.loads(fixture["host_path"].read_text(encoding="utf-8"))
        host["operationId"] = str(uuid.uuid4())
        data = _canonical(host)
        path = root / "operations" / f"{_sha(data)}.json"
        path.write_bytes(data)
        path.with_suffix(".sig").write_bytes(_sign(fixture["recovery_private"], data))
        _protect(path)
        _protect(path.with_suffix(".sig"))
    elif mutation == "reparse":
        os.symlink(root / "store.json", root / "operations" / "linked.json")
    elif mutation == "duplicate-ace":
        _run_ps(f"& icacls.exe {_ps(root / 'operations')} /grant '*{fixture['sid']}:(R)' | Out-Null")
    else:
        os.link(fixture["host_path"], fixture["root"].parent / "second-link.json")
    result = _module("try { " + _assert_store_body(fixture) + "; 'missed' } catch { 'rejected' }")
    assert result.stdout.strip() == "rejected"


def test_untrusted_acl_and_ancestor_delete_child_fail_closed(tmp_path: Path):
    fixture = _preprovisioned_store(tmp_path)
    operations = fixture["root"] / "operations"
    _run_ps(f"& icacls.exe {_ps(operations)} /grant '*S-1-1-0:(OI)(CI)(R)' | Out-Null")
    with pytest.raises(AssertionError):
        _assert_store(fixture)

    fixture = _preprovisioned_store(tmp_path / "second")
    parent = fixture["root"].parent
    _run_ps(f"& icacls.exe {_ps(parent)} /grant '*S-1-1-0:(DC)' | Out-Null")
    with pytest.raises(AssertionError):
        _assert_store(fixture)


def test_native_cleanup_rejects_wrong_identity(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    result = _module(
        f"$m=Get-Module PhaseAEvidenceStore;$identity=& $m {{param($p)"
        "$lease=Open-PhaseAValidatedDirectoryLease $p;try{Get-PhaseAFileIdentity $lease}finally{$lease.Dispose()}} "
        f"{_ps(root)};try {{ [ApplyPilot.PhaseA.EvidenceNative]::DeleteTreeNoFollow("
        f"{_ps(root)},[uint64]$identity.VolumeSerialNumber,('0'*32),-1);'missed' }} catch {{ 'rejected' }}"
    )
    assert result.stdout.strip() == "rejected"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_authorization_and_partial_crash_do_not_replay(tmp_path: Path):
    fixture = _cleanup_fixture(tmp_path)
    wrong = _cleanup_body(fixture).replace(
        f"-ExpectedTargetDigest {_ps(fixture['target'])}",
        f"-ExpectedTargetDigest {_ps('f' * 64)}",
    )
    assert _run_ps("try { " + wrong + "; 'missed' } catch { 'rejected' }").stdout.strip() == "rejected"
    assert fixture["stage"].exists()

    assert _run_ps(
        "try { " + _cleanup_body(fixture, "-CrashAfterEntries 1") + "; 'missed' } catch { 'crashed' }"
    ).stdout.strip() == "crashed"
    state = sorted(str(path.relative_to(fixture["stage"])) for path in fixture["stage"].rglob("*"))
    assert _run_ps("try { " + _cleanup_body(fixture) + "; 'missed' } catch { 'rejected' }").stdout.strip() == "rejected"
    assert state == sorted(str(path.relative_to(fixture["stage"])) for path in fixture["stage"].rglob("*"))


def test_cleanup_post_mutation_crash_resumes_to_completion(tmp_path: Path):
    fixture = _cleanup_fixture(tmp_path)
    crash = _run_ps(
        "try { " + _cleanup_body(fixture, "-CrashAfterMutation") + "; 'missed' } "
        "catch { 'crashed:' + $_.Exception.Message }"
    ).stdout.strip()
    assert crash == "crashed:Injected cleanup crash after mutation.", crash
    assert not fixture["stage"].exists()
    result = _run_ps(_cleanup_body(fixture) + " | ConvertTo-Json -Compress")
    payload = json.loads(result.stdout)
    assert payload["OperationId"] == fixture["operation"]
    assert payload["ManifestAfterSha256"] == fixture["after"]


def test_cleanup_rejects_replacement_between_authorization_and_delete(tmp_path: Path):
    fixture = _cleanup_fixture(tmp_path)
    moved = fixture["parent"] / "moved-original"
    prefix = (
        f"$replace={{param($stage) Move-Item -LiteralPath $stage -Destination {_ps(moved)};"
        "$null=New-Item -ItemType Directory -Path $stage;Set-Content -LiteralPath "
        "(Join-Path $stage 'replacement.txt') -Value replacement};"
    )
    result = _run_ps(
        prefix + "try { " + _cleanup_body(fixture, "-BeforeCleanupDelete $replace")
        + "; 'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"
    assert moved.is_dir()
    assert (fixture["stage"] / "replacement.txt").is_file()


@pytest.mark.parametrize(
    "pair_name,suffix,mutation",
    [
        ("authorization", ".json", "untrusted"),
        ("authorization", ".sig", "untrusted"),
        ("completion", ".json", "untrusted"),
        ("completion", ".sig", "untrusted"),
        ("authorization", ".json", "duplicate"),
        ("completion", ".sig", "duplicate"),
    ],
)
def test_cleanup_rejects_bootstrap_pair_file_acl_before_mutation(
    tmp_path: Path, pair_name: str, suffix: str, mutation: str
):
    fixture = _cleanup_fixture(tmp_path)
    path = Path(fixture[pair_name]).with_suffix(suffix)
    if mutation == "untrusted":
        _run_ps(f"& icacls.exe {_ps(path)} /grant '*S-1-1-0:(F)' | Out-Null")
    else:
        _run_ps(f"& icacls.exe {_ps(path)} /deny '*{_current_sid()}:(R)' | Out-Null")

    result = _run_ps(
        "try { " + _cleanup_body(fixture) + "; 'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"
    assert fixture["stage"].is_dir()
    assert (fixture["stage"] / "two.txt").read_text(encoding="utf-8") == "two"


def test_cleanup_rejects_bootstrap_receipt_replacement_before_mutation(tmp_path: Path):
    fixture = _cleanup_fixture(tmp_path)
    receipt = Path(fixture["authorization"])
    moved = receipt.with_name("held-authorization.json")
    prefix = (
        "$replaceReceipt={param($stage) "
        f"Move-Item -LiteralPath {_ps(receipt)} -Destination {_ps(moved)};"
        f"Copy-Item -LiteralPath {_ps(moved)} -Destination {_ps(receipt)};"
        f"& icacls.exe {_ps(receipt)} /grant '*S-1-1-0:(F)' | Out-Null}};"
    )
    result = _run_ps(
        prefix + "try { " + _cleanup_body(fixture, "-BeforeCleanupDelete $replaceReceipt")
        + "; 'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"
    assert fixture["stage"].is_dir()
    assert (fixture["stage"] / "two.txt").is_file()


def test_definition_provision_fully_validates_before_publish_and_is_idempotent(tmp_path: Path):
    body, final = _provision_body(tmp_path, "valid")
    result = _run_ps(body + "| ConvertTo-Json -Compress", timeout=90)
    assert json.loads(result.stdout)["Valid"] is True
    assert final.is_dir()
    result = _run_ps(body + "| ConvertTo-Json -Compress", timeout=90)
    assert json.loads(result.stdout)["Valid"] is True


@pytest.mark.parametrize("mode", ["invalid", "crash"])
def test_definition_provision_never_publishes_invalid_or_prepublication_crash(tmp_path: Path, mode: str):
    body, final = _provision_body(tmp_path, mode)
    result = _run_ps("try { " + body + "; 'missed' } catch { 'rejected' }", timeout=90)
    assert result.stdout.strip() == "rejected"
    assert not final.exists()


def test_definition_provision_never_repairs_invalid_existing_v1(tmp_path: Path):
    body, final = _provision_body(tmp_path, "valid")
    final.mkdir()
    marker = final / "invalid.marker"
    marker.write_text("retain", encoding="utf-8")
    result = _run_ps("try { " + body + "; 'missed' } catch { 'rejected' }", timeout=90)
    assert result.stdout.strip() == "rejected"
    assert marker.read_text(encoding="utf-8") == "retain"

def _is_elevated() -> bool:
    return _run_ps(
        "$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    ).stdout.strip().lower() == "true"


@pytest.mark.skipif(not _is_elevated(), reason="requires an elevated Windows token")
def test_elevated_provision_idempotency_and_cleanup(tmp_path: Path):
    parent = Path(os.environ["ProgramData"]) / "ApplyPilot" / "EvidenceTest" / str(uuid.uuid4())
    try:
        body, final = _provision_body(tmp_path, "valid", parent)
        result = _run_ps(body + "| ConvertTo-Json -Compress", timeout=90)
        assert json.loads(result.stdout)["Valid"] is True
        assert (parent / "v1" / "store.json").is_file()
        assert sorted(p.name for p in (parent / "v1").iterdir() if p.is_dir()) == [
            "adjudications", "bundles", "operations"
        ]
        _run_ps(body + "| Out-Null", timeout=90)
        _run_ps(f"& icacls.exe {_ps(final)} /setowner '*S-1-5-32-544' /T /C | Out-Null")
        assert _run_ps(
            "try { " + body + "; 'missed' } catch { 'rejected' }", timeout=90
        ).stdout.strip() == "rejected"
    finally:
        if parent.exists():
            _run_ps(f"Remove-Item -LiteralPath {_ps(parent)} -Recurse -Force")
    assert not parent.exists()
