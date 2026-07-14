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


def _new_keypair(root: Path, name: str) -> tuple[Path, Path, str]:
    private = root / f"{name}.private.pem"
    public = root / f"{name}.public.der"
    _run_ps(
        "$rsa=[Security.Cryptography.RSA]::Create(3072)\n"
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
        "hostProvisioningReceiptSha256": "5" * 64,
        "sourceApprovalReceiptSha256": "6" * 64,
        "manifestBeforeSha256": "7" * 64,
        "manifestAfterSha256": "8" * 64,
    }
    value.update(overrides)
    return value


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
            f"-SignaturePath {_ps(candidate.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)}; "
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
        f"-ExpectedReceiptType 'operation-authorization' -ExpectedOperationId {_ps(receipt['operationId'])} "
        "-ExpectedTargetDigest ('1'*64) -ExpectedManifestAfterSha256 ('8'*64)"
    )
    assert result.stdout.strip() == "True"
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
            f"-{parameter} {_ps(forged)}; 'missed' }} catch {{ 'rejected' }}"
        )
        assert result.stdout.strip() == "rejected"
    mismatch = tmp_path / ("f" * 64 + ".json")
    mismatch.write_bytes(content)
    mismatch.with_suffix(".sig").write_bytes(sig_path.read_bytes())
    assert _module(
        f"try {{ Test-PhaseASignedReceipt -ReceiptPath {_ps(mismatch)} "
        f"-SignaturePath {_ps(mismatch.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)}; "
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
        f"-SignaturePath {_ps(path.with_suffix('.sig'))} -SigningSpkiPath {_ps(wrong_public)}; "
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
        f"-SignaturePath {_ps(malformed_path.with_suffix('.sig'))} -SigningSpkiPath {_ps(public)}; "
        "'missed' } catch { 'rejected' }"
    ).stdout.strip() == "rejected"


def test_receipt_generator_never_accepts_private_key_and_verifies_returned_signature(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "offhost")
    inputs = _receipt("credential-revocation", key_hash)
    input_path = tmp_path / "inputs.json"
    input_path.write_bytes(_canonical(inputs))
    unsigned = tmp_path / "unsigned"
    result = _run_ps(
        f"& {_ps(NEW_RECEIPT)} -InputPath {_ps(input_path)} -OutputDirectory {_ps(unsigned)} "
        f"-SigningSpkiPath {_ps(public)} -CreateUnsigned"
    )
    unsigned_path = Path(result.stdout.strip())
    assert unsigned_path.name == _sha(unsigned_path.read_bytes()) + ".json"
    assert "private" not in NEW_RECEIPT.read_text(encoding="utf-8").lower()
    signature = unsigned_path.with_suffix(".sig")
    signature.write_bytes(_sign(private, unsigned_path.read_bytes()))
    verified = _run_ps(
        f"& {_ps(NEW_RECEIPT)} -InputPath {_ps(unsigned_path)} -SignaturePath {_ps(signature)} "
        f"-SigningSpkiPath {_ps(public)} -VerifyReturnedSignature"
    )
    assert verified.stdout.strip() == str(unsigned_path)


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
    destination = tmp_path / "operations"
    destination.mkdir()
    _protect(destination)
    content = _canonical(_receipt("operation-completion", key_hash))
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    signature = source.with_suffix(".sig")
    signature.write_bytes(_sign(private, content))
    result = _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(signature)} -DestinationDirectory {_ps(destination)} "
        f"-SigningSpkiPath {_ps(public)} -CrashAfter {_ps(boundary)}; 'missed' }} "
        "catch { 'crashed' }"
    )
    assert result.stdout.strip() == "crashed"
    final_receipt = destination / source.name
    final_signature = destination / signature.name
    before = tuple(p.read_bytes() if p.exists() else None for p in (final_receipt, final_signature))
    retry = _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(signature)} -DestinationDirectory {_ps(destination)} "
        f"-SigningSpkiPath {_ps(public)} | Out-Null; 'installed' }} catch {{ 'rejected:' + $_.Exception.Message }}"
    ).stdout.strip()
    if (before[0] is None and before[1] is None) or (before[0] is not None and before[1] is not None):
        assert retry == "installed", retry
    else:
        assert retry.startswith("rejected:"), retry
        assert before == tuple(p.read_bytes() if p.exists() else None for p in (final_receipt, final_signature))


def test_install_rejects_unprotected_acl_and_orphan_half_pair(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "install")
    content = _canonical(_receipt("operation-completion", key_hash))
    source = tmp_path / f"{_sha(content)}.json"
    source.write_bytes(content)
    source.with_suffix(".sig").write_bytes(_sign(private, content))
    destination = tmp_path / "operations"
    destination.mkdir()
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -DestinationDirectory {_ps(destination)} "
        f"-SigningSpkiPath {_ps(public)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"
    _protect(destination)
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -DestinationDirectory {_ps(destination)} "
        f"-SigningSpkiPath {_ps(public)} -ExpectedTargetDigest ('f'*64); 'missed' }} "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert not list(destination.iterdir())
    (destination / source.name).write_bytes(content)
    before = (destination / source.name).read_bytes()
    assert _module(
        f"try {{ Install-PhaseASignedReceipt -ReceiptPath {_ps(source)} "
        f"-SignaturePath {_ps(source.with_suffix('.sig'))} -DestinationDirectory {_ps(destination)} "
        f"-SigningSpkiPath {_ps(public)}; 'missed' }} catch {{ 'rejected' }}"
    ).stdout.strip() == "rejected"
    assert (destination / source.name).read_bytes() == before


def test_unc_nonfixed_and_reparse_inputs_are_rejected(tmp_path: Path):
    assert _module(
        "try { Get-PhaseATargetDigest -Path '\\\\localhost\\C$\\ProgramData'; 'missed' } "
        "catch { 'rejected' }"
    ).stdout.strip() == "rejected"
    assert _module(
        "try { Get-PhaseATargetDigest -Path 'A:\\phase-a-evidence'; 'missed' } "
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


def _is_elevated() -> bool:
    return _run_ps(
        "$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    ).stdout.strip().lower() == "true"


@pytest.mark.skipif(not _is_elevated(), reason="requires an elevated Windows token")
def test_elevated_provision_idempotency_and_cleanup():
    parent = Path(os.environ["ProgramData"]) / "ApplyPilot" / "EvidenceTest" / str(uuid.uuid4())
    sid = _current_sid()
    try:
        result = _run_ps(
            f". {_ps(PROVISION)} -DefinitionImport\n"
            f"Invoke-PhaseAEvidenceStoreProvision -StoreRoot {_ps(parent / 'v1')} "
            f"-CanonicalOperatorSid {_ps(sid)} -TestIdentity"
        )
        assert (parent / "v1" / "store.json").is_file()
        assert sorted(p.name for p in (parent / "v1").iterdir() if p.is_dir()) == [
            "adjudications", "bundles", "operations"
        ]
        _run_ps(
            f". {_ps(PROVISION)} -DefinitionImport\n"
            f"Invoke-PhaseAEvidenceStoreProvision -StoreRoot {_ps(parent / 'v1')} "
            f"-CanonicalOperatorSid {_ps(sid)} -TestIdentity | Out-Null"
        )
    finally:
        if parent.exists():
            _run_ps(f"Remove-Item -LiteralPath {_ps(parent)} -Recurse -Force")
    assert not parent.exists()
