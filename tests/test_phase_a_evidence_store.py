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


def _sign_with_salt(private: Path, content: bytes, salt_length: int) -> bytes:
    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    return key.sign(
        content,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt_length),
        hashes.SHA256(),
    )


def _protect(path: Path, sid: str | None = None) -> None:
    sid = sid or _current_sid()
    file_switch = "$true" if path.is_file() else "$false"
    _module(
        "$m=Get-Module PhaseAEvidenceStore;"
        f"& $m {{param($p,$s,$f)Set-PhaseAProtectedAcl $p $s -File:$f}} {_ps(path)} {_ps(sid)} {file_switch}"
    )


def _anchor_fixture(root: Path) -> dict[str, Path | str]:
    signing_private, signing_spki, signing_hash = _new_keypair(root, "operator-signing")
    recovery_private, recovery_spki, recovery_hash = _new_keypair(root, "recovery-encryption")
    signing_meta = root / "operator-signing-key.json"
    recovery_meta = root / "recovery-encryption-key.json"
    signing_meta.write_bytes(_canonical({
        "schemaVersion": 1,
        "keyPurpose": "applypilot.phase-a.operator-receipt-signing",
        "spkiFile": "operator-signing-key.spki.der",
        "spkiSha256": signing_hash,
    }))
    recovery_meta.write_bytes(_canonical({
        "schemaVersion": 1,
        "keyPurpose": "applypilot.phase-a.recovery-oaep-encryption",
        "spkiFile": "recovery-encryption-key.spki.der",
        "spkiSha256": recovery_hash,
    }))
    return {
        "signing_private": signing_private, "signing_spki": signing_spki,
        "signing_hash": signing_hash, "signing_meta": signing_meta,
        "recovery_private": recovery_private, "recovery_spki": recovery_spki,
        "recovery_hash": recovery_hash, "recovery_meta": recovery_meta,
    }


def test_owned_files_and_exported_surface_exist():
    assert PROVISION.is_file()
    assert NEW_RECEIPT.is_file()
    result = _module(
        "(Get-Module PhaseAEvidenceStore).ExportedCommands.Keys|Sort-Object|ConvertTo-Json -Compress"
    )
    assert json.loads(result.stdout) == sorted(
            [
                "Assert-PhaseAEvidenceStore",
                "ConvertTo-PhaseACanonicalJsonBytes",
                "Get-PhaseAAuthenticatedBundleCandidates",
                "Get-PhaseADirectoryManifest",
            "Get-PhaseAMachineDigest",
            "Get-PhaseAOperatorSidDigest",
            "Get-PhaseASecurityDescriptorHash",
                "Get-PhaseATargetDigest",
                "Get-PhaseAProductionAnchors",
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
    assert "'applypilot.phase-a.runtime-source-approval'" in generator
    assert "CandidateBundleSha256" not in generator
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


def test_quality_contract_fixed_anchor_paths_and_absent_production_gate():
    source = MODULE.read_text(encoding="utf-8")
    assert "config', 'phase-a', 'operator-signing-key.json" in source
    assert "config', 'phase-a', 'operator-signing-key.spki.der" in source
    assert "config', 'phase-a', 'recovery-encryption-key.json" in source
    assert "config', 'phase-a', 'recovery-encryption-key.spki.der" in source
    assert "ProductionSigningSpkiSha256 = $null" not in source
    result = _module(
        "try { Get-PhaseAProductionAnchors | Out-Null; 'missed' } catch { 'rejected' }"
    )
    assert result.stdout.strip() == "rejected"


def test_distinct_committed_anchor_metadata_loads_and_key_reuse_fails(tmp_path: Path):
    anchors = _anchor_fixture(tmp_path)
    command = (
        f"Get-PhaseAProductionAnchors -OperatorSigningMetadataPath {_ps(anchors['signing_meta'])} "
        f"-OperatorSigningSpkiPath {_ps(anchors['signing_spki'])} "
        f"-RecoveryEncryptionMetadataPath {_ps(anchors['recovery_meta'])} "
        f"-RecoveryEncryptionSpkiPath {_ps(anchors['recovery_spki'])} -DefinitionImport"
    )
    loaded = json.loads(_module(command + "|ConvertTo-Json -Depth 5 -Compress").stdout)
    assert loaded["OperatorSigning"]["SpkiSha256"] == anchors["signing_hash"]
    assert loaded["RecoveryEncryption"]["SpkiSha256"] == anchors["recovery_hash"]
    reused = _module(
        "try{" + command.replace(str(anchors["recovery_spki"]), str(anchors["signing_spki"]))
        + "|Out-Null;'missed'}catch{'rejected'}"
    )
    assert reused.stdout.strip() == "rejected"


def test_quality_contract_canonical_json_safe_integer_boundary_and_float_rejection():
    expected = b'{"maximum":9007199254740991,"minimum":-9007199254740991}'
    result = _module(
        "$v=[ordered]@{maximum=[int64]9007199254740991;minimum=[int64]-9007199254740991};"
        "[Convert]::ToBase64String((ConvertTo-PhaseACanonicalJsonBytes $v))"
    )
    assert base64.b64decode(result.stdout.strip()) == expected
    for expression in (
        "[ordered]@{value=[int64]9007199254740992}",
        "[ordered]@{value=[double]1.5}",
        "[ordered]@{value=[double]1.0}",
    ):
        rejected = _module(
            f"try {{ ConvertTo-PhaseACanonicalJsonBytes ({expression}) | Out-Null; 'missed' }} "
            "catch { 'rejected' }"
        )
        assert rejected.stdout.strip() == "rejected"


def test_quality_contract_source_approval_exact_independent_canonical_bytes(tmp_path: Path):
    _, public, key_hash = _new_keypair(tmp_path, "operator")
    output = tmp_path / "unsigned"
    spec_task = "11111111-1111-4111-8111-111111111111"
    quality_task = "22222222-2222-4222-8222-222222222222"
    commit = "a" * 40
    tree = "b" * 40
    plan = "c" * 64
    nonce = "d" * 64
    critical_a = "e" * 64
    critical_z = "f" * 64
    command = (
        f"$critical=@{{'z.ps1'={_ps(critical_z)};'a.py'={_ps(critical_a)}}};"
        f"& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.runtime-source-approval "
        f"-ApprovedCommit {_ps(commit)} -ApprovedTree {_ps(tree)} -PlanSha256 {_ps(plan)} "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-SpecReviewTaskId {_ps(spec_task)} -QualityReviewTaskId {_ps(quality_task)} "
        f"-CriticalFileSha256 $critical -Nonce {_ps(nonce)} -CreatedAtUtc '2026-07-14T12:34:56Z' "
        f"-CreateUnsigned -OutputDirectory {_ps(output)}"
    )
    path = Path(_run_ps(command).stdout.strip())
    expected = (
        '{"approvedCommit":"' + commit + '","approvedTree":"' + tree
        + '","createdAtUtc":"2026-07-14T12:34:56Z","criticalFileSha256":'
        + '{"a.py":"' + critical_a + '","z.ps1":"' + critical_z + '"},'
        + '"nonce":"' + nonce + '","operatorSigningKeySpkiSha256":"' + key_hash
        + '","planSha256":"' + plan + '","qualityReview":{"result":"APPROVED",'
        + '"taskId":"' + quality_task + '"},"receiptType":"applypilot.phase-a.runtime-source-approval",'
        + '"schemaVersion":1,"specReview":{"result":"APPROVED","taskId":"' + spec_task + '"}}'
    ).encode("ascii")
    assert path.read_bytes() == expected


def test_quality_contract_other_schema_independent_canonical_bytes(tmp_path: Path):
    _, public, key_hash = _new_keypair(tmp_path, "operator")
    sid = _current_sid()
    output = tmp_path / "schemas"
    output.mkdir()
    h = {letter: letter * 64 for letter in "abcdef"}
    commit = "1" * 40
    created = "2026-07-14T12:34:56Z"
    candidate_store = tmp_path / "candidate-store"
    candidate_store.mkdir()
    _protect(candidate_store)
    for leaf in ("bundles", "adjudications", "operations"):
        (candidate_store / leaf).mkdir()
        _protect(candidate_store / leaf)
    for preimage in (h["e"], h["f"]):
        bundle = candidate_store / "bundles" / f"{h['a']}-{preimage}.apeb"
        bundle.write_bytes(preimage.encode("ascii"))
        _protect(bundle)
    authenticator = (
        "$auth={param($c)$candidate=if($c.PreimageSha256 -ceq '" + h["e"] + "'){'"
        + h["b"] + "'}else{'" + h["c"] + "'};[ordered]@{sourceIdentityDigest=$c.SourceIdentityDigest;"
        "preimageSha256=$c.PreimageSha256;candidateBundleSha256=$candidate}};"
    )
    cases = [
        (
            "applypilot.phase-a.evidence-adjudication",
            f"-SourceIdentityDigest {_ps(h['a'])} -SelectedBundleSha256 {_ps(h['b'])} "
            f"-StoreRoot {_ps(candidate_store)} -CanonicalOperatorSid {_ps(sid)} "
            f"-BundleAuthenticator $auth -DefinitionImport -Nonce {_ps(h['d'])}",
            f'{{"candidateBundleSha256":["{h["b"]}","{h["c"]}"],"createdAtUtc":"{created}",'
            f'"nonce":"{h["d"]}","operatorSigningKeySpkiSha256":"{key_hash}",'
            f'"receiptType":"applypilot.phase-a.evidence-adjudication","schemaVersion":1,'
            f'"selectedBundleSha256":"{h["b"]}","sourceIdentityDigest":"{h["a"]}"}}',
        ),
        (
            "applypilot.phase-a.credential-revocation",
            f"-ApprovedCommit {_ps(commit)} -CredentialReferenceDigest {_ps(h['a'])} "
            f"-ProviderClass api-key -RevokedAtUtc '2026-07-14T12:30:00Z' "
            f"-StaleProbeAtUtc '2026-07-14T12:31:00Z' -ProviderEvidenceSha256 {_ps(h['b'])} "
            f"-MachineIdentityDigest {_ps(h['c'])} -Nonce {_ps(h['d'])}",
            f'{{"approvedCommit":"{commit}","credentialReferenceDigest":"{h["a"]}",'
            f'"machineIdentityDigest":"{h["c"]}","nonce":"{h["d"]}",'
            f'"operatorSigningKeySpkiSha256":"{key_hash}","providerClass":"api-key",'
            f'"providerEvidenceSha256":"{h["b"]}","receiptType":"applypilot.phase-a.credential-revocation",'
            '"revokedAtUtc":"2026-07-14T12:30:00Z","schemaVersion":1,'
            '"staleProbeAtUtc":"2026-07-14T12:31:00Z","staleProbeResult":"DENIED"}',
        ),
        (
            "applypilot.phase-a.provisioning-cleanup-authorization",
            f"-ApprovedCommit {_ps(commit)} -OperationId {_ps(h['a'])} -TargetIdentityDigest {_ps(h['b'])} "
            f"-BeforeManifestSha256 {_ps(h['c'])} -ExpectedAfterManifestSha256 {_ps(h['d'])} "
            f"-EvidenceBundleSha256 {_ps('0'*64)} -CredentialInventoryRoot {_ps('0'*64)} "
            f"-CredentialRevocationSetRoot {_ps('0'*64)} -OperatorSid {_ps(sid)}",
            f'{{"approvedCommit":"{commit}","beforeManifestSha256":"{h["c"]}","createdAtUtc":"{created}",'
            f'"credentialInventoryRoot":"{"0"*64}","credentialRevocationSetRoot":"{"0"*64}",'
            f'"evidenceBundleSha256":"{"0"*64}","expectedAfterManifestSha256":"{h["d"]}",'
            f'"operationId":"{h["a"]}","operatorSid":"{sid}",'
            f'"operatorSigningKeySpkiSha256":"{key_hash}",'
            '"receiptType":"applypilot.phase-a.provisioning-cleanup-authorization","schemaVersion":1,'
            f'"targetIdentityDigest":"{h["b"]}"}}',
        ),
        (
            "applypilot.phase-a.provisioning-cleanup-completion",
            f"-ApprovedCommit {_ps(commit)} -OperationId {_ps(h['a'])} "
            f"-AuthorizationReceiptSha256 {_ps(h['b'])} -ActualAfterManifestSha256 {_ps(h['c'])} "
            f"-ExpectedAfterManifestSha256 {_ps(h['c'])}",
            f'{{"actualAfterManifestSha256":"{h["c"]}","approvedCommit":"{commit}",'
            f'"authorizationReceiptSha256":"{h["b"]}","createdAtUtc":"{created}",'
            f'"operationId":"{h["a"]}","operatorSigningKeySpkiSha256":"{key_hash}",'
            '"receiptType":"applypilot.phase-a.provisioning-cleanup-completion","result":"COMPLETE",'
            '"schemaVersion":1}',
        ),
        (
            "applypilot.phase-a.host-provisioning",
            f"-ApprovedCommit {_ps(commit)} -SourceApprovalReceiptSha256 {_ps(h['a'])} "
            f"-MachineIdentityDigest {_ps(h['b'])} -StoreConfigSha256 {_ps(h['c'])} "
            f"-StoreTreeManifestSha256 {_ps(h['d'])} -RecoveryKeySpkiSha256 {_ps(h['e'])} "
            f"-OperatorSidDigest {_ps(h['f'])}",
            f'{{"approvedCommit":"{commit}","createdAtUtc":"{created}",'
            f'"machineIdentityDigest":"{h["b"]}","operatorSidDigest":"{h["f"]}",'
            f'"operatorSigningKeySpkiSha256":"{key_hash}",'
            '"receiptType":"applypilot.phase-a.host-provisioning",'
            f'"recoveryKeySpkiSha256":"{h["e"]}","result":"COMPLETE","schemaVersion":1,'
            f'"sourceApprovalReceiptSha256":"{h["a"]}","storeConfigSha256":"{h["c"]}",'
            f'"storeTreeManifestSha256":"{h["d"]}"}}',
        ),
    ]
    for index, (receipt_type, arguments, expected) in enumerate(cases):
        case_output = output / str(index)
        result = _run_ps(
            authenticator + f"& {_ps(NEW_RECEIPT)} -ReceiptType {_ps(receipt_type)} "
            f"-OperatorSigningSpkiPath {_ps(public)} "
            f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} {arguments} "
            f"-CreatedAtUtc {_ps(created)} -CreateUnsigned -OutputDirectory {_ps(case_output)}"
        )
        assert Path(result.stdout.strip()).read_bytes() == expected.encode("ascii")


def test_quality_contract_verify_uses_caller_bindings_and_completion_authority(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "operator")
    output = tmp_path / "verify"
    common = (
        f"-ReceiptType applypilot.phase-a.provisioning-cleanup-completion "
        f"-OperatorSigningSpkiPath {_ps(public)} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ApprovedCommit {_ps('1'*40)} -OperationId {_ps('a'*64)} "
        f"-AuthorizationReceiptSha256 {_ps('b'*64)} -ActualAfterManifestSha256 {_ps('c'*64)} "
        f"-ExpectedAfterManifestSha256 {_ps('c'*64)} -CreatedAtUtc '2026-07-14T12:34:56Z'"
    )
    receipt = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} {common} -CreateUnsigned -OutputDirectory {_ps(output)}"
    ).stdout.strip())
    signature = receipt.with_suffix(".sig")
    signature.write_bytes(_sign(private, receipt.read_bytes()))
    verified = _run_ps(
        f"& {_ps(NEW_RECEIPT)} {common} -VerifyReturnedSignature "
        f"-ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)}"
    )
    assert Path(verified.stdout.strip()) == receipt
    signature.write_bytes(_sign_with_salt(private, receipt.read_bytes(), 20))
    wrong_salt = _run_ps(
        f"try {{ & {_ps(NEW_RECEIPT)} {common} -VerifyReturnedSignature "
        f"-ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)}; 'missed' }} catch {{ 'rejected' }}"
    )
    assert wrong_salt.stdout.strip() == "rejected"
    signature.write_bytes(_sign(private, receipt.read_bytes()))
    _, replacement_key, _ = _new_keypair(tmp_path, "replacement")
    expected = receipt.read_text(encoding="utf-8").replace("'", "''")
    race = _module(
        f"$expected='{expected}'|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$race={{param($p)Move-Item -LiteralPath $p -Destination ($p+'.old');Copy-Item {_ps(replacement_key)} $p}};"
        f"try{{Test-PhaseASignedReceipt -ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)} "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        "-ExpectedReceiptType applypilot.phase-a.provisioning-cleanup-completion -ExpectedBindings $expected "
        f"-ExpectedAuthorizedAfterManifestSha256 {_ps('c'*64)} -BeforeSpkiRevalidation $race -DefinitionImport;"
        "'missed'}catch{'rejected'}"
    )
    assert race.stdout.strip() == "rejected"
    wrong_authority = common.replace("-ExpectedAfterManifestSha256 '" + "c" * 64 + "'", "-ExpectedAfterManifestSha256 '" + "d" * 64 + "'")
    result = _run_ps(
        f"try {{ & {_ps(NEW_RECEIPT)} {wrong_authority} -VerifyReturnedSignature "
        f"-ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)}; 'missed' }} catch {{ 'rejected' }}"
    )
    assert result.stdout.strip() == "rejected"


@pytest.mark.parametrize("boundary", [
    "after-receipt-stage", "after-signature-stage", "after-receipt-rename",
    "after-signature-rename", "before-pair-revalidation",
])
def test_quality_install_holds_source_and_resumes_exact_half_pair(tmp_path: Path, boundary: str):
    private, public, key_hash = _new_keypair(tmp_path, "operator")
    source_dir = tmp_path / "source"
    args = (
        f"-ReceiptType applypilot.phase-a.credential-revocation "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ApprovedCommit {_ps('1'*40)} -CredentialReferenceDigest {_ps('a'*64)} "
        f"-ProviderClass api-key -RevokedAtUtc '2026-07-14T12:30:00Z' "
        f"-StaleProbeAtUtc '2026-07-14T12:31:00Z' -ProviderEvidenceSha256 {_ps('b'*64)} "
        f"-MachineIdentityDigest {_ps('c'*64)} -Nonce {_ps('d'*64)}"
    )
    receipt = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} {args} -CreateUnsigned -OutputDirectory {_ps(source_dir)}"
    ).stdout.strip())
    signature = receipt.with_suffix(".sig")
    signature.write_bytes(_sign(private, receipt.read_bytes()))
    _protect(receipt)
    _protect(signature)
    store = tmp_path / "store"
    store.mkdir()
    _protect(store)
    for leaf in ("bundles", "adjudications", "operations"):
        (store / leaf).mkdir()
        _protect(store / leaf)
    expected = receipt.read_text(encoding="utf-8").replace("'", "''")
    install = (
        f"$expected='{expected}'|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"Install-PhaseASignedReceipt -ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)} "
        f"-StoreRoot {_ps(store)} -OperatorSigningSpkiPath {_ps(public)} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        "-ExpectedReceiptType applypilot.phase-a.credential-revocation -ExpectedBindings $expected "
        "-DefinitionImport "
    )
    crashed = _module(
        "try { " + install + f"-CrashAfter {boundary}; 'missed' }} catch {{ 'crashed' }}"
    )
    assert crashed.stdout.strip() == "crashed"
    final_receipt = store / "operations" / receipt.name
    final_signature = final_receipt.with_suffix(".sig")
    assert final_receipt.is_file() is (boundary != "after-receipt-stage")
    assert final_signature.is_file() is (boundary in {"after-signature-rename", "before-pair-revalidation"})
    result = _module(install + "| ConvertTo-Json -Compress")
    assert json.loads(result.stdout)["SignaturePath"] == str(final_signature)
    assert final_signature.read_bytes() == signature.read_bytes()
    moved = tmp_path / "moved-source.json"
    race = (
        f"$race={{param($pair) Move-Item -LiteralPath {_ps(receipt)} -Destination {_ps(moved)}}};"
    )
    rejected = _module(
        race + "try { " + install + "-BeforeFinalPairRevalidation $race; 'missed' } catch { 'rejected' }"
    )
    assert rejected.stdout.strip() == "rejected"
    assert receipt.is_file()


def test_quality_stage_created_protected_and_same_handle_renamed(tmp_path: Path):
    stage = tmp_path / ".provisioning-11111111-1111-4111-8111-111111111111"
    final = tmp_path / "v1"
    moved = tmp_path / "moved-stage"
    sid = _current_sid()
    body = (
        f"$m=Get-Module PhaseAEvidenceStore;& $m {{param($stage,$final,$moved,$sid)"
        "$sd=Get-PhaseAProtectedSecurityDescriptorBytes $sid;"
        "$h=[ApplyPilot.PhaseA.EvidenceNative]::CreateProtectedDirectory($stage,$sd);"
        "try{$before=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($h);"
        "Assert-PhaseAProtectedAcl $stage $sid;"
        "Move-Item -LiteralPath $stage -Destination $moved;"
        "$null=New-Item -ItemType Directory -Path $stage;Set-Content -LiteralPath (Join-Path $stage 'replacement') -Value replacement;"
        "[ApplyPilot.PhaseA.EvidenceNative]::RenameDirectoryHandleNoReplace($h,$final);"
        "$after=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($h);"
        "[pscustomobject]@{Before=$before.FileId;After=$after.FileId;Final=$after.FinalPath}|ConvertTo-Json -Compress"
        "}finally{$h.Dispose()}} "
        f"{_ps(stage)} {_ps(final)} {_ps(moved)} {_ps(sid)}"
    )
    payload = json.loads(_module(body).stdout)
    assert payload["Before"] == payload["After"]
    assert Path(payload["Final"]) == final
    assert final.is_dir()
    assert (stage / "replacement").is_file()


def test_bundle_names_staging_residue_and_store_derived_adjudication(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    _protect(root)
    for leaf in ("bundles", "adjudications", "operations"):
        (root / leaf).mkdir()
        _protect(root / leaf)
    source, preimage, candidate = "a" * 64, "b" * 64, "c" * 64
    bundle = root / "bundles" / f"{source}-{preimage}.apeb"
    bundle.write_bytes(b"opaque ciphertext")
    _protect(bundle)
    residue = root / "bundles" / ".staging-11111111-1111-4111-8111-111111111111"
    residue.write_bytes(b"partial ciphertext")
    _protect(residue)
    inventory = _module(
        f"$m=Get-Module PhaseAEvidenceStore;& $m {{param($r,$s)@(Get-PhaseAReceiptInventory $r $s).Count}} "
        f"{_ps(root)} {_ps(_current_sid())}"
    )
    assert inventory.stdout.strip() == "0"
    auth = (
        "$auth={param($c)[ordered]@{sourceIdentityDigest=$c.SourceIdentityDigest;"
        "preimageSha256=$c.PreimageSha256;candidateBundleSha256='" + candidate + "'}};"
    )
    derived = _module(
        auth + f"@(Get-PhaseAAuthenticatedBundleCandidates -StoreRoot {_ps(root)} "
        f"-CanonicalOperatorSid {_ps(_current_sid())} -SourceIdentityDigest {_ps(source)} "
        "-BundleAuthenticator $auth -DefinitionImport) -join ','"
    )
    assert derived.stdout.strip() == candidate
    production = _module(
        f"try {{ Get-PhaseAAuthenticatedBundleCandidates -StoreRoot {_ps(root)} "
        f"-CanonicalOperatorSid {_ps(_current_sid())} -SourceIdentityDigest {_ps(source)} "
        "| Out-Null; 'missed' } catch { 'rejected' }"
    )
    assert production.stdout.strip() == "rejected"
    unexpected = root / "bundles" / f"{source}.apeb"
    unexpected.write_bytes(b"wrong")
    _protect(unexpected)
    rejected = _module(
        f"$m=Get-Module PhaseAEvidenceStore;try {{& $m {{param($r,$s)Get-PhaseAReceiptInventory $r $s}} "
        f"{_ps(root)} {_ps(_current_sid())};'missed'}}catch{{'rejected'}}"
    )
    assert rejected.stdout.strip() == "rejected"


def test_manifest_is_redacted_streamed_and_detects_identity_race(tmp_path: Path):
    root = tmp_path / "manifest"
    root.mkdir()
    (root / "secret-name.txt").write_bytes(b"x" * (5 * 1024 * 1024))
    payload = json.loads(_module(
        f"Get-PhaseADirectoryManifest -Root {_ps(root)} -MaximumFileSize 6291456 | ConvertTo-Json -Depth 8 -Compress"
    ).stdout)
    encoded = json.dumps(payload, separators=(",", ":"))
    assert "secret-name" not in encoded and '"relativePath":' not in encoded
    assert payload["entries"][0]["size"] == 5 * 1024 * 1024
    assert list(payload["entries"][0]) == [
        "relativePathDigest", "objectIdentityDigest", "kind", "contentSha256",
        "securityDescriptorSha256", "size",
    ]
    race = _module(
        f"$hook={{param($p,$h)Move-Item -LiteralPath $p -Destination ($p+'.moved')}};"
        f"try{{Get-PhaseADirectoryManifest -Root {_ps(root)} -BeforeObjectRevalidation $hook -DefinitionImport|Out-Null;'missed'}}catch{{'rejected'}}"
    )
    assert race.stdout.strip() == "rejected"


@pytest.mark.parametrize("mutation", ["everyone", "duplicate", "owner", "reparse"])
def test_exact_acl_owner_and_no_reparse_fail_closed(tmp_path: Path, mutation: str):
    target = tmp_path / "protected"
    target.mkdir()
    _protect(target)
    sid = _current_sid()
    expected_sid = sid
    if mutation == "everyone":
        _run_ps(f"& icacls.exe {_ps(target)} /grant '*S-1-1-0:(F)' | Out-Null")
    elif mutation == "duplicate":
        _run_ps(f"& icacls.exe {_ps(target)} /grant '*{sid}:(RX)' | Out-Null")
    elif mutation == "owner":
        expected_sid = "S-1-5-18"
    else:
        target.rmdir()
        _run_ps(f"New-Item -ItemType SymbolicLink -Path {_ps(target)} -Target {_ps(tmp_path)}|Out-Null")
    result = _module(
        f"$m=Get-Module PhaseAEvidenceStore;try{{& $m {{param($p,$s)Assert-PhaseAProtectedAcl $p $s}} "
        f"{_ps(target)} {_ps(expected_sid)};'missed'}}catch{{'rejected'}}"
    )
    assert result.stdout.strip() == "rejected"


@pytest.mark.parametrize("key_form", ["rsa2048", "trailing_der", "replacement"])
def test_operator_signing_spki_is_one_held_canonical_rsa3072_identity(tmp_path: Path, key_form: str):
    _, public, key_hash = _new_keypair(tmp_path, "operator", 2048 if key_form == "rsa2048" else 3072)
    if key_form == "trailing_der":
        public.write_bytes(public.read_bytes() + b"\x00")
        key_hash = _sha(public.read_bytes())
    body = (
        "$m=Get-Module PhaseAEvidenceStore;try { & $m {param($p,$h,$form)"
        "$read=Read-PhaseAValidatedBytes $p;"
        "if($form -eq 'replacement'){Move-Item -LiteralPath $p -Destination ($p+'.old');[IO.File]::WriteAllBytes($p,$read.Bytes)};"
        "$rsa=Import-PhaseAOperatorSigningSpkiBytes $read.Bytes $h;$rsa.Dispose();'accepted'"
        "} " + f"{_ps(public)} {_ps(key_hash)} {_ps(key_form)}"
        " } catch { 'rejected' }"
    )
    result = _module(body)
    assert result.stdout.strip() == ("accepted" if key_form == "replacement" else "rejected")


def test_provisioning_uses_protected_creation_and_same_handle_publication_only():
    source = PROVISION.read_text(encoding="utf-8")
    assert "CreateProtectedDirectory($stage,$sd)" in source
    assert "RenameDirectoryHandleNoReplace($stageHandle,$final)" in source
    assert "RenameDirectoryNoReplace" not in source
    assert "Move-Item" not in source
    assert "Assert-PhaseAEvidenceStore -StoreRoot $stage" in source
    assert source.index("Assert-PhaseAEvidenceStore -StoreRoot $stage") < source.index(
        "RenameDirectoryHandleNoReplace($stageHandle,$final)"
    )


def test_cleanup_is_two_phase_and_secure_handle_bound_only():
    source = PROVISION.read_text(encoding="utf-8")
    assert "Completion cannot predate mutation." in source
    assert "State='COMPLETION_REQUIRED'" in source
    assert "DeleteTreeNoFollow" in source
    assert "Remove-Item" not in source
    assert source.index("DeleteTreeNoFollow") < source.index("COMPLETION_REQUIRED")


def test_definition_provision_validates_before_publish_is_idempotent_and_crash_safe(tmp_path: Path):
    anchors = _anchor_fixture(tmp_path)
    sid = _current_sid()
    source_dir = tmp_path / "source"
    source_args = (
        f"-ReceiptType applypilot.phase-a.runtime-source-approval "
        f"-OperatorSigningSpkiPath {_ps(anchors['signing_spki'])} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(anchors['signing_hash'])} "
        f"-ApprovedCommit {_ps('1'*40)} -ApprovedTree {_ps('2'*40)} -PlanSha256 {_ps('3'*64)} "
        "-SpecReviewTaskId '11111111-1111-4111-8111-111111111111' "
        "-QualityReviewTaskId '22222222-2222-4222-8222-222222222222' "
        f"-CriticalFileSha256 @{{'scripts/a.ps1'={_ps('4'*64)}}} -Nonce {_ps('5'*64)} "
        "-CreatedAtUtc '2026-07-14T12:00:00Z'"
    )
    source_receipt = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} {source_args} -CreateUnsigned -OutputDirectory {_ps(source_dir)}"
    ).stdout.strip())
    source_signature = source_receipt.with_suffix(".sig")
    source_signature.write_bytes(_sign(anchors["signing_private"], source_receipt.read_bytes()))
    _protect(source_receipt)
    _protect(source_signature)
    base = tmp_path / "evidence"
    base.mkdir()
    _protect(base)
    final = base / "v1"
    material = tmp_path / "host-material"
    common = (
        f". {_ps(PROVISION)} -DefinitionImport;"
        f"$source=Get-Content -LiteralPath {_ps(source_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$expected=@{{{_ps(source_receipt.stem)}=$source}};"
        "$materializer={param($ctx)"
        "$binding=[ordered]@{schemaVersion=1;receiptType='applypilot.phase-a.host-provisioning';"
        "approvedCommit=$ctx.ApprovedCommit;sourceApprovalReceiptSha256=$ctx.SourceApprovalReceiptSha256;"
        "operatorSigningKeySpkiSha256=$ctx.OperatorSigningKeySpkiSha256;machineIdentityDigest=$ctx.MachineIdentityDigest;"
        "storeConfigSha256=$ctx.StoreConfigSha256;storeTreeManifestSha256=$ctx.StoreTreeManifestSha256;"
        "recoveryKeySpkiSha256=$ctx.RecoveryKeySpkiSha256;operatorSidDigest=$ctx.OperatorSidDigest;"
        "result='COMPLETE';createdAtUtc='2026-07-14T12:01:00Z'};"
        f"$receipt=& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.host-provisioning "
        f"-OperatorSigningSpkiPath {_ps(anchors['signing_spki'])} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(anchors['signing_hash'])} "
        "-ApprovedCommit $ctx.ApprovedCommit -SourceApprovalReceiptSha256 $ctx.SourceApprovalReceiptSha256 "
        "-MachineIdentityDigest $ctx.MachineIdentityDigest -StoreConfigSha256 $ctx.StoreConfigSha256 "
        "-StoreTreeManifestSha256 $ctx.StoreTreeManifestSha256 -RecoveryKeySpkiSha256 $ctx.RecoveryKeySpkiSha256 "
        "-OperatorSidDigest $ctx.OperatorSidDigest -CreatedAtUtc '2026-07-14T12:01:00Z' "
        f"-CreateUnsigned -OutputDirectory {_ps(material)};"
        f"$rsa=[Security.Cryptography.RSA]::Create();$rsa.ImportFromPem([IO.File]::ReadAllText({_ps(anchors['signing_private'])}));"
        "$bytes=[IO.File]::ReadAllBytes($receipt);$sig=$rsa.SignData($bytes,[Security.Cryptography.HashAlgorithmName]::SHA256,"
        "[Security.Cryptography.RSASignaturePadding]::Pss);$rsa.Dispose();$signature=[IO.Path]::ChangeExtension($receipt,'sig');"
        "[IO.File]::WriteAllBytes($signature,$sig);$m=Get-Module PhaseAEvidenceStore;"
        f"& $m {{param($p,$s)Set-PhaseAProtectedAcl $p $s -File}} $receipt {_ps(sid)};"
        f"& $m {{param($p,$s)Set-PhaseAProtectedAcl $p $s -File}} $signature {_ps(sid)};"
        "$expected[[IO.Path]::GetFileNameWithoutExtension($receipt)]=$binding;"
        "[pscustomobject]@{ReceiptPath=$receipt;SignaturePath=$signature}};"
    )
    invoke = (
        f"Invoke-PhaseAEvidenceStoreProvision -StoreRoot {_ps(final)} -CanonicalOperatorSid {_ps(sid)} "
        f"-ExpectedCommit {_ps('1'*40)} -ExpectedReceiptBindingsByHash $expected "
        f"-OperatorSigningMetadataPath {_ps(anchors['signing_meta'])} -OperatorSigningSpkiPath {_ps(anchors['signing_spki'])} "
        f"-RecoveryEncryptionMetadataPath {_ps(anchors['recovery_meta'])} -RecoveryEncryptionSpkiPath {_ps(anchors['recovery_spki'])} "
        f"-SourceApprovalReceiptPath {_ps(source_receipt)} -SourceApprovalSignaturePath {_ps(source_signature)} "
        "$materializer=$materializer -HostReceiptMaterializer $materializer "
        "-TestMachineGuid '01234567-89ab-cdef-0123-456789abcdef' "
        "-TestSmbiosUuid 'fedcba98-7654-3210-fedc-ba9876543210' "
        f"-TestAncestorBoundary {_ps(base)} -DefinitionImport"
    ).replace("$materializer=$materializer ", "")
    first = json.loads(_run_ps(common + invoke + "|ConvertTo-Json -Compress", timeout=120).stdout)
    assert first["Valid"] is True and final.is_dir()
    host_receipt = next(
        path for path in (final / "operations").glob("*.json") if path.stem != source_receipt.stem
    )
    existing_authority = (
        f"$hostBinding=Get-Content -LiteralPath {_ps(host_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$expected[{_ps(host_receipt.stem)}]=$hostBinding;"
    )
    second = json.loads(_run_ps(common + existing_authority + invoke + "|ConvertTo-Json -Compress", timeout=120).stdout)
    assert second["Valid"] is True
    crash_base = tmp_path / "crash-evidence"
    crash_base.mkdir()
    _protect(crash_base)
    crash_final = crash_base / "v1"
    crash_invoke = invoke.replace(str(final), str(crash_final)).replace(str(base), str(crash_base))
    crashed = _run_ps(
        common + "try{" + crash_invoke + " -CrashBeforePublication|Out-Null;'missed'}catch{'crashed'}",
        timeout=120,
    )
    assert crashed.stdout.strip() == "crashed"
    assert not crash_final.exists()


def test_cleanup_requires_post_mutation_completion_then_resumes(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "cleanup-signing")
    sid = _current_sid()
    parent = tmp_path / "stages"
    parent.mkdir()
    stage = parent / ".provisioning-11111111-1111-4111-8111-111111111111"
    (stage / "nested").mkdir(parents=True)
    (stage / "nested" / "payload.bin").write_bytes(b"payload")
    before = json.loads(_module(
        f"Get-PhaseADirectoryManifest -Root {_ps(parent)}|ConvertTo-Json -Depth 12 -Compress"
    ).stdout)
    stage_manifest = json.loads(_module(
        f"Get-PhaseADirectoryManifest -Root {_ps(stage)}|ConvertTo-Json -Depth 12 -Compress"
    ).stdout)
    removed = {stage_manifest["baseRootIdentityDigest"]} | {
        entry["objectIdentityDigest"] for entry in stage_manifest["entries"]
    }
    after = {
        "schemaVersion": 1,
        "manifestType": "applypilot.phase-a.directory-manifest",
        "baseRootIdentityDigest": before["baseRootIdentityDigest"],
        "entries": [entry for entry in before["entries"] if entry["objectIdentityDigest"] not in removed],
    }
    expected_after = tmp_path / "expected-after.json"
    expected_after.write_bytes(_canonical(after))
    target = _module(f"Get-PhaseATargetDigest -Path {_ps(stage)}").stdout.strip()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    _protect(bootstrap)
    operation = "a" * 64
    auth_args = (
        "-ReceiptType applypilot.phase-a.provisioning-cleanup-authorization "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ApprovedCommit {_ps('1'*40)} -OperationId {_ps(operation)} -TargetIdentityDigest {_ps(target)} "
        f"-BeforeManifestSha256 {_ps(_sha(_canonical(before)))} -ExpectedAfterManifestSha256 {_ps(_sha(_canonical(after)))} "
        f"-EvidenceBundleSha256 {_ps('0'*64)} -CredentialInventoryRoot {_ps('0'*64)} "
        f"-CredentialRevocationSetRoot {_ps('0'*64)} -OperatorSid {_ps(sid)} "
        "-CreatedAtUtc '2026-07-14T12:00:00Z'"
    )
    authorization = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} {auth_args} -CreateUnsigned -OutputDirectory {_ps(bootstrap)}"
    ).stdout.strip())
    authorization_sig = authorization.with_suffix(".sig")
    authorization_sig.write_bytes(_sign(private, authorization.read_bytes()))
    _protect(authorization)
    _protect(authorization_sig)
    common = (
        f". {_ps(PROVISION)} -DefinitionImport;"
        f"$expected=Get-Content -LiteralPath {_ps(authorization)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"Invoke-PhaseAProvisioningCleanup -StagingPath {_ps(stage)} -CanonicalOperatorSid {_ps(sid)} "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ExpectedCommit {_ps('1'*40)} -AuthorizationReceiptPath {_ps(authorization)} "
        f"-AuthorizationSignaturePath {_ps(authorization_sig)} -ExpectedAuthorizationBindings $expected "
        f"-ExpectedAfterManifestPath {_ps(expected_after)} -TestBootstrapRoot {_ps(bootstrap)} -DefinitionImport"
    )
    first = json.loads(_run_ps(common + "|ConvertTo-Json -Compress", timeout=120).stdout)
    assert first["State"] == "COMPLETION_REQUIRED"
    assert not stage.exists()
    request = Path(first["CompletionRequestPath"])
    request_sig = request.with_suffix(".sig")
    request_sig.write_bytes(_sign(private, request.read_bytes()))
    _protect(request_sig)
    resume = common + (
        f" -CompletionReceiptPath {_ps(request)} -CompletionSignaturePath {_ps(request_sig)} "
        f"-CompletionRequestPath {_ps(request)}"
    )
    completed = json.loads(_run_ps(resume + "|ConvertTo-Json -Compress", timeout=120).stdout)
    assert completed["State"] == "COMPLETE"
    replay = json.loads(_run_ps(common + "|ConvertTo-Json -Compress", timeout=120).stdout)
    assert replay["State"] == "COMPLETION_REQUIRED"
    assert Path(replay["CompletionRequestPath"]) == request
    assert not stage.exists()
