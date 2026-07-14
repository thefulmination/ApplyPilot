from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
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

REPARSE_MUTATOR_TYPE = r"""
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace ApplyPilot.PhaseA.Tests
{
    public static class ReparseMutator
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileDispositionInformation
        {
            [MarshalAs(UnmanagedType.U1)] public bool DeleteFile;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFileW(string path, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DeviceIoControl(IntPtr handle, uint code, byte[] input,
            uint inputSize, IntPtr output, uint outputSize, out uint returned, IntPtr overlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetFileInformationByHandle(IntPtr handle, int informationClass,
            ref FileDispositionInformation information, uint size);

        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetSecurityDescriptorDacl(IntPtr descriptor,
            [MarshalAs(UnmanagedType.Bool)] out bool present, out IntPtr dacl,
            [MarshalAs(UnmanagedType.Bool)] out bool defaulted);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint SetNamedSecurityInfoW(string path, int objectType,
            uint securityInfo, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

        private static void Put(byte[] buffer, int offset, byte[] value)
        {
            Buffer.BlockCopy(value, 0, buffer, offset, value.Length);
        }

        public static int TrySetJunction(string path, string target)
        {
            IntPtr handle = CreateFileW(Path.GetFullPath(path), 0x40000000, 7,
                IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (handle == new IntPtr(-1)) return Marshal.GetLastWin32Error();
            try
            {
                string print = Path.GetFullPath(target).TrimEnd('\\');
                string substitute = "\\??\\" + print;
                byte[] substituteBytes = Encoding.Unicode.GetBytes(substitute);
                byte[] printBytes = Encoding.Unicode.GetBytes(print);
                int pathBytes = substituteBytes.Length + 2 + printBytes.Length + 2;
                byte[] buffer = new byte[16 + pathBytes];
                Put(buffer, 0, BitConverter.GetBytes(0xA0000003U));
                Put(buffer, 4, BitConverter.GetBytes(checked((ushort)(8 + pathBytes))));
                Put(buffer, 8, BitConverter.GetBytes((ushort)0));
                Put(buffer, 10, BitConverter.GetBytes(checked((ushort)substituteBytes.Length)));
                Put(buffer, 12, BitConverter.GetBytes(checked((ushort)(substituteBytes.Length + 2))));
                Put(buffer, 14, BitConverter.GetBytes(checked((ushort)printBytes.Length)));
                Put(buffer, 16, substituteBytes);
                Put(buffer, 18 + substituteBytes.Length, printBytes);
                uint returned;
                if (!DeviceIoControl(handle, 0x000900A4, buffer, (uint)buffer.Length,
                    IntPtr.Zero, 0, out returned, IntPtr.Zero))
                    return Marshal.GetLastWin32Error();
                return 0;
            }
            finally { CloseHandle(handle); }
        }

        public static void MarkDirectoryDeletePending(string path)
        {
            IntPtr handle = CreateFileW(Path.GetFullPath(path), 0x00010000, 5,
                IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
            if (handle == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                var information = new FileDispositionInformation { DeleteFile = true };
                if (!SetFileInformationByHandle(handle, 4, ref information,
                    (uint)Marshal.SizeOf<FileDispositionInformation>()))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally { CloseHandle(handle); }
        }

        public static void SetPathDacl(string path, byte[] securityDescriptor)
        {
            GCHandle pinned = GCHandle.Alloc(securityDescriptor, GCHandleType.Pinned);
            try
            {
                bool present, defaulted;
                IntPtr dacl;
                if (!GetSecurityDescriptorDacl(pinned.AddrOfPinnedObject(), out present,
                    out dacl, out defaulted))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if (!present || dacl == IntPtr.Zero)
                    throw new InvalidOperationException("Test descriptor has no DACL.");
                uint error = SetNamedSecurityInfoW(Path.GetFullPath(path), 1,
                    0x80000004, IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
                if (error != 0) throw new Win32Exception((int)error);
            }
            finally { pinned.Free(); }
        }
    }
}
"""


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


def _production_writer_failure_probe(
    temp_root: Path,
    failure_point: str,
    *,
    cleanup_fails: bool = False,
) -> tuple[Path, dict[str, object]]:
    root = temp_root / f"writer-{failure_point}"
    root.mkdir()
    _protect(root)
    stage = root / f".{failure_point}.receipt-stage"
    body = (
        "$m=Get-Module PhaseAEvidenceStore;"
        "& $m {param($stage,$root,$sid,$point,$cleanupFails)"
        "$state=[pscustomobject]@{Point=$point;CleanupFails=$cleanupFails;DeleteCalls=0};"
        "$streamCommand={param($Handle)"
        "$inner=$Handle.OpenWriteStream();"
        "$wrapper=[pscustomobject]@{Inner=$inner;Point=$state.Point};"
        "$wrapper|Add-Member ScriptMethod Write {param($data)"
        "if($this.Point -ceq 'write'){throw [IO.IOException]::new('injected write failure')}"
        "$this.Inner.Write([byte[]]$data)};"
        "$wrapper|Add-Member ScriptMethod Flush {param($toDisk)"
        "if($this.Point -ceq 'flush'){throw [IO.IOException]::new('injected flush failure')}"
        "$this.Inner.Flush([bool]$toDisk)};"
        "$wrapper|Add-Member ScriptMethod Dispose {"
        "$this.Inner.Dispose();if($this.Point -ceq 'close'){"
        "throw [IO.IOException]::new('injected close failure')}};"
        "return $wrapper}.GetNewClosure();"
        "$aclCommand={param($Handle,$OperatorSid)throw 'injected acl failure'}.GetNewClosure();"
        "$readCommand={param($Handle,$Path)throw 'injected validation failure'}.GetNewClosure();"
        "$deleteCommand={param($Handle)"
        "$state.DeleteCalls++;if($state.CleanupFails){"
        "throw 'injected cleanup failure'}"
        "[ApplyPilot.PhaseA.WindowsFile]::SetDeletionDisposition($Handle)}.GetNewClosure();"
        "$writerArgs=@{OpenWriteStreamCommand=$streamCommand;DeleteFileCommand=$deleteCommand};"
        "if($state.Point -ceq 'acl'){$writerArgs.AssertFileAclCommand=$aclCommand};"
        "if($state.Point -ceq 'validation'){$writerArgs.ReadHeldFileCommand=$readCommand};"
        "$bytes=[Text.Encoding]::UTF8.GetBytes('production-stage');$caught=$null;"
        "try{Write-PhaseACreateNew $stage $bytes $sid @writerArgs;"
        "$caught='missed'}"
        "catch{$caught=$_.Exception;};"
        "$messages=@();if($caught -is [AggregateException]){"
        "$messages=@($caught.Flatten().InnerExceptions|ForEach-Object Message)}"
        "elseif($caught -is [Exception]){$messages=@($caught.Message)}"
        "elseif($caught){$messages=@([string]$caught)};"
        "[pscustomobject]@{Type=if($caught){$caught.GetType().FullName}else{'none'};"
        "Message=if($caught -is [Exception]){$caught.Message}else{[string]$caught};Messages=$messages;"
        "DeleteCalls=$state.DeleteCalls;Exists=(Test-Path -LiteralPath $stage)}"
        "|ConvertTo-Json -Depth 6 -Compress} "
        f"{_ps(stage)} {_ps(root)} {_ps(_current_sid())} {_ps(failure_point)} "
        + ("$true" if cleanup_fails else "$false")
    )
    return stage, json.loads(_module(body).stdout)


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


def test_production_receipt_staging_uses_only_validated_handle_primitives():
    module = MODULE.read_text(encoding="utf-8")
    writer = module.split("function Write-PhaseACreateNew", 1)[1].split(
        "function Open-PhaseAExpectedProtectedFile", 1
    )[0]
    installer = module.split("function Install-PhaseASignedReceipt", 1)[1].split(
        "function Get-PhaseAReceiptInventory", 1
    )[0]

    assert "New-PhaseAValidatedFile" in writer
    assert "Open-PhaseAValidatedFileWriteStream" in writer
    assert "Set-PhaseAFileDeletionDisposition" in writer
    assert "Assert-PhaseAProtectedFileHandleAcl" in writer
    assert "Set-PhaseAProtectedAcl" not in writer
    assert "[IO.FileStream]::new" not in writer
    assert "-AuthorizedBasenamePattern" not in writer + installer
    assert "-AuthorizedRenameBasename" in installer
    assert "Rename-PhaseAFileNoReplace" in installer
    assert "[ApplyPilot.PhaseA.EvidenceNative]::RenameFileNoReplace" not in installer


def test_production_stage_writer_creates_protected_file_and_writes_via_duplicate(
    tmp_path: Path,
):
    root = tmp_path / "writer"
    root.mkdir()
    _protect(root)
    stage = root / f".{'a' * 64}.receipt-stage"
    body = (
        "$m=Get-Module PhaseAEvidenceStore;"
        "& $m {param($stage,$root,$sid)"
        "$calls=[pscustomobject]@{New=0;Stream=0;DescriptorExact=$false};"
        "$expected=Get-PhaseAProtectedSecurityDescriptorBytes $sid -File;"
        "$newCommand={param($arguments)"
        "$calls.New++;"
        "$calls.DescriptorExact="
        "[Security.Cryptography.CryptographicOperations]::FixedTimeEquals("
        "[byte[]]$arguments.SecurityDescriptor,[byte[]]$expected);"
        "[ApplyPilot.PhaseA.WindowsFile]::NewValidatedFile("
        "[string]$arguments.Path,[string]$arguments.Access,[string]$arguments.AuthorizedRoot,"
        "[string]$arguments.AuthorizedBasename,$null,[byte[]]$arguments.SecurityDescriptor)}"
        ".GetNewClosure();"
        "$streamCommand={param($Handle)"
        "$calls.Stream++;"
        "$Handle.OpenWriteStream()}.GetNewClosure();"
        "$bytes=[Text.Encoding]::UTF8.GetBytes('production-stage');"
        "Write-PhaseACreateNew $stage $bytes $sid -NewFileCommand $newCommand "
        "-OpenWriteStreamCommand $streamCommand;"
        "$held=Open-PhaseAValidatedFile $stage Read $root ([IO.Path]::GetFileName($stage));"
        "try{Assert-PhaseAProtectedFileHandleAcl $held $sid;"
        "$read=Read-PhaseABytesFromHeldHandle $held $stage;"
        "[pscustomobject]@{New=$calls.New;Stream=$calls.Stream;"
        "DescriptorExact=$calls.DescriptorExact;"
        "Bytes=[Text.Encoding]::UTF8.GetString($read.Bytes)}|ConvertTo-Json -Compress}"
        "finally{$held.Dispose()}} "
        f"{_ps(stage)} {_ps(root)} {_ps(_current_sid())}"
    )
    result = json.loads(_module(body).stdout)
    assert result == {
        "New": 1,
        "Stream": 1,
        "DescriptorExact": True,
        "Bytes": "production-stage",
    }


@pytest.mark.parametrize("failure_point", ["write", "flush", "close", "validation", "acl"])
def test_production_stage_writer_failure_deletes_by_handle(
    tmp_path: Path,
    failure_point: str,
):
    stage, result = _production_writer_failure_probe(tmp_path, failure_point)
    assert result["DeleteCalls"] == 1
    assert result["Exists"] is False
    assert f"injected {failure_point} failure" in " ".join(result["Messages"])
    assert not stage.exists()


def test_production_stage_writer_cleanup_failure_preserves_both_errors_and_residue(
    tmp_path: Path,
):
    stage, result = _production_writer_failure_probe(
        tmp_path,
        "flush",
        cleanup_fails=True,
    )
    assert result["Type"] == "System.AggregateException"
    assert result["DeleteCalls"] == 1
    assert result["Exists"] is True
    assert "injected flush failure" in " ".join(result["Messages"])
    assert "injected cleanup failure" in " ".join(result["Messages"])

    retry = _module(
        "$m=Get-Module PhaseAEvidenceStore;"
        f"& $m {{param($p,$s)try{{Write-PhaseACreateNew $p "
        "([Text.Encoding]::UTF8.GetBytes('retry')) $s;'missed'}catch{'rejected'}} "
        f"{_ps(stage)} {_ps(_current_sid())}"
    )
    assert retry.stdout.strip() == "rejected"
    assert stage.is_file()


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
            f"-DefinitionBundleAuthenticator $auth -DefinitionImport -Nonce {_ps(h['d'])}",
            f'{{"candidateBundleSha256":["{h["b"]}","{h["c"]}"],"createdAtUtc":"{created}",'
            f'"nonce":"{h["d"]}","operatorSigningKeySpkiSha256":"{key_hash}",'
            f'"receiptType":"applypilot.phase-a.evidence-adjudication","schemaVersion":1,'
            f'"selectedBundleSha256":"{h["b"]}","sourceIdentityDigest":"{h["a"]}"}}',
        ),
        (
            "applypilot.phase-a.credential-revocation",
            f"-ApprovedCommit {_ps(commit)} -CredentialReferenceDigest {_ps(h['a'])} "
            f"-ProviderClass postgres -RevokedAtUtc '2026-07-14T12:30:00Z' "
            f"-StaleProbeAtUtc '2026-07-14T12:31:00Z' -ProviderEvidenceSha256 {_ps(h['b'])} "
            f"-MachineIdentityDigest {_ps(h['c'])} -Nonce {_ps(h['d'])}",
            f'{{"approvedCommit":"{commit}","credentialReferenceDigest":"{h["a"]}",'
            f'"machineIdentityDigest":"{h["c"]}","nonce":"{h["d"]}",'
            f'"operatorSigningKeySpkiSha256":"{key_hash}","providerClass":"postgres",'
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
        f"-ProviderClass postgres -RevokedAtUtc '2026-07-14T12:30:00Z' "
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
        "-DefinitionBundleAuthenticator $auth -DefinitionImport) -join ','"
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


@pytest.mark.parametrize("mutation", ["everyone", "duplicate", "owner"])
def test_exact_acl_and_owner_fail_closed(tmp_path: Path, mutation: str):
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
    result = _module(
        f"$m=Get-Module PhaseAEvidenceStore;try{{& $m {{param($p,$s)Assert-PhaseAProtectedAcl $p $s}} "
        f"{_ps(target)} {_ps(expected_sid)};'missed'}}catch{{'rejected'}}"
    )
    assert result.stdout.strip() == "rejected"


def test_directory_reparse_with_exact_acl_fails_closed(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "protected"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    sid = _current_sid()
    fixture = json.loads(_module(
        "$m=Get-Module PhaseAEvidenceStore;"
        "& $m {param($p,$s)"
        "$security=New-PhaseAProtectedSecurity $s;"
        "Set-Acl -LiteralPath $p -AclObject $security;"
        "$item=Get-Item -LiteralPath $p -Force;"
        "$acl=Get-Acl -LiteralPath $p;"
        "$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]));"
        "$trusted=@($s,'S-1-5-18','S-1-5-32-544');"
        "$inheritance=[Security.AccessControl.InheritanceFlags]::ContainerInherit -bor "
        "[Security.AccessControl.InheritanceFlags]::ObjectInherit;"
        "$exact=$rules.Count -eq 3;"
        "foreach($trustedSid in $trusted){"
        "$matching=@($rules|Where-Object{$_.IdentityReference.Value -ceq $trustedSid});"
        "if($matching.Count -ne 1){$exact=$false;continue};"
        "$rule=$matching[0];"
        "if($rule.IsInherited -or "
        "$rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or "
        "$rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or "
        "$rule.InheritanceFlags -ne $inheritance -or "
        "$rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None){$exact=$false}"
        "};"
        "[pscustomobject]@{"
        "Reparse=(($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0);"
        "Protected=$acl.AreAccessRulesProtected;"
        "Owner=([Security.Principal.NTAccount]$acl.Owner).Translate("
        "[Security.Principal.SecurityIdentifier]).Value;"
        "ExactRules=$exact"
        "}|ConvertTo-Json -Compress"
        f"}} {_ps(junction)} {_ps(sid)}"
    ).stdout)
    assert fixture == {
        "Reparse": True,
        "Protected": True,
        "Owner": sid,
        "ExactRules": True,
    }

    result = _module(
        f"$m=Get-Module PhaseAEvidenceStore;try{{& $m {{param($p,$s)Assert-PhaseAProtectedAcl $p $s}} "
        f"{_ps(junction)} {_ps(sid)};'missed'}}catch{{'rejected'}}"
    )
    assert result.stdout.strip() == "rejected"


def test_directory_reparse_race_cannot_be_accepted(tmp_path: Path):
    protected = tmp_path / "protected"
    protected.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    _protect(protected)
    sid = _current_sid()
    body = (
        "Add-Type -TypeDefinition @'\n"
        + REPARSE_MUTATOR_TYPE
        + "\n'@\n"
        + f"$target={_ps(target)};"
        "$state=[pscustomobject]@{MutationError=-1};"
        "$hook={param($p,$h)$state.MutationError="
        "[ApplyPilot.PhaseA.Tests.ReparseMutator]::TrySetJunction($p,$target)}.GetNewClosure();"
        "$accepted=$true;$m=Get-Module PhaseAEvidenceStore;"
        "try{& $m {param($p,$s,$h)Assert-PhaseAProtectedAcl -Path $p -OperatorSid $s "
        "-BeforeFinalObjectRevalidation $h -DefinitionImport} "
        f"{_ps(protected)} {_ps(sid)} $hook}}catch{{$accepted=$false}};"
        f"$item=Get-Item -LiteralPath {_ps(protected)} -Force;"
        "[pscustomobject]@{Accepted=$accepted;MutationError=$state.MutationError;"
        "MutationSucceeded=($state.MutationError -eq 0);"
        "Reparse=(($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0)}"
        "|ConvertTo-Json -Compress"
    )
    result = json.loads(_module(body).stdout)
    assert result == {
        "Accepted": False,
        "MutationError": 0,
        "MutationSucceeded": True,
        "Reparse": True,
    }


def test_directory_acl_drift_after_initial_read_is_rejected(tmp_path: Path):
    protected = tmp_path / "protected"
    protected.mkdir()
    _protect(protected)
    sid = _current_sid()
    body = (
        "Add-Type -TypeDefinition @'\n"
        + REPARSE_MUTATOR_TYPE
        + "\n'@\n"
        + "$m=Get-Module PhaseAEvidenceStore;"
        "$mutated=[byte[]](& $m {param($s)$acl=New-PhaseAProtectedSecurity $s;"
        "$inheritance=[Security.AccessControl.InheritanceFlags]::ContainerInherit -bor "
        "[Security.AccessControl.InheritanceFlags]::ObjectInherit;"
        "$rule=[Security.AccessControl.FileSystemAccessRule]::new("
        "[Security.Principal.SecurityIdentifier]::new('S-1-1-0'),"
        "[Security.AccessControl.FileSystemRights]::FullControl,$inheritance,"
        "[Security.AccessControl.PropagationFlags]::None,"
        "[Security.AccessControl.AccessControlType]::Allow);"
        "$null=$acl.AddAccessRule($rule);return ,$acl.GetSecurityDescriptorBinaryForm()} "
        + _ps(sid)
        + ");"
        "$state=[pscustomobject]@{Mutated=$false};"
        "$hook={param($p,$h)[ApplyPilot.PhaseA.Tests.ReparseMutator]::SetPathDacl($p,$mutated);"
        "$state.Mutated=$true}.GetNewClosure();"
        "$accepted=$true;$errorMessage=$null;"
        "try{& $m {param($p,$s,$h)Assert-PhaseAProtectedAcl -Path $p -OperatorSid $s "
        "-BeforeFinalObjectRevalidation $h -DefinitionImport} "
        f"{_ps(protected)} {_ps(sid)} $hook}}catch{{$accepted=$false;$errorMessage=$_.Exception.Message}};"
        f"$acl=Get-Acl -LiteralPath {_ps(protected)};"
        "$everyoneCount=@($acl.GetAccessRules($true,$true,"
        "[Security.Principal.SecurityIdentifier])|Where-Object{"
        "$_.IdentityReference.Value -ceq 'S-1-1-0'}).Count;"
        "[pscustomobject]@{Accepted=$accepted;Mutated=$state.Mutated;Error=$errorMessage;"
        "EveryoneAceCount=$everyoneCount}|ConvertTo-Json -Compress"
    )
    result = json.loads(_module(body).stdout)
    assert result["Accepted"] is False
    assert result["Mutated"] is True
    assert result["EveryoneAceCount"] == 1
    assert "DACL" in result["Error"]


def test_directory_delete_pending_before_final_acceptance_is_rejected(tmp_path: Path):
    protected = tmp_path / "protected"
    protected.mkdir()
    _protect(protected)
    sid = _current_sid()
    body = (
        "Add-Type -TypeDefinition @'\n"
        + REPARSE_MUTATOR_TYPE
        + "\n'@\n"
        + "$state=[pscustomobject]@{Marked=$false};"
        "$hook={param($p,$h)[ApplyPilot.PhaseA.Tests.ReparseMutator]::"
        "MarkDirectoryDeletePending($p);$state.Marked=$true}.GetNewClosure();"
        "$accepted=$true;$m=Get-Module PhaseAEvidenceStore;"
        "try{& $m {param($p,$s,$h)Assert-PhaseAProtectedAcl -Path $p -OperatorSid $s "
        "-BeforeFinalObjectRevalidation $h -DefinitionImport} "
        f"{_ps(protected)} {_ps(sid)} $hook}}catch{{$accepted=$false}};"
        f"[pscustomobject]@{{Accepted=$accepted;Marked=$state.Marked;"
        f"Exists=(Test-Path -LiteralPath {_ps(protected)})}}|ConvertTo-Json -Compress"
    )
    result = json.loads(_module(body).stdout)
    assert result == {
        "Accepted": False,
        "Marked": True,
        "Exists": False,
    }


def test_directory_acl_validation_preserves_delete_share_and_revalidates_handle(tmp_path: Path):
    protected = tmp_path / "protected"
    protected.mkdir()
    moved = tmp_path / "moved"
    _protect(protected)
    sid = _current_sid()
    body = (
        f"$destination={_ps(moved)};"
        "$state=[pscustomobject]@{Moved=$false};"
        "$hook={param($p,$h)try{Move-Item -LiteralPath $p -Destination $destination -ErrorAction Stop;"
        "$state.Moved=$true}catch{}}.GetNewClosure();"
        "$accepted=$true;$m=Get-Module PhaseAEvidenceStore;"
        "try{& $m {param($p,$s,$h)Assert-PhaseAProtectedAcl -Path $p -OperatorSid $s "
        "-BeforeFinalObjectRevalidation $h -DefinitionImport} "
        f"{_ps(protected)} {_ps(sid)} $hook}}catch{{$accepted=$false}};"
        f"[pscustomobject]@{{Accepted=$accepted;Moved=$state.Moved;"
        f"OriginalExists=(Test-Path -LiteralPath {_ps(protected)});"
        f"DestinationExists=(Test-Path -LiteralPath {_ps(moved)})}}|ConvertTo-Json -Compress"
    )
    result = json.loads(_module(body).stdout)
    assert result == {
        "Accepted": False,
        "Moved": True,
        "OriginalExists": False,
        "DestinationExists": True,
    }


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
    validation = (
        f"Assert-PhaseAEvidenceStore -StoreRoot {_ps(final)} -CanonicalOperatorSid {_ps(sid)} "
        f"-ExpectedCommit {_ps('1'*40)} -ExpectedReceiptBindingsByHash $expected "
        f"-OperatorSigningMetadataPath {_ps(anchors['signing_meta'])} -OperatorSigningSpkiPath {_ps(anchors['signing_spki'])} "
        f"-RecoveryEncryptionMetadataPath {_ps(anchors['recovery_meta'])} -RecoveryEncryptionSpkiPath {_ps(anchors['recovery_spki'])} "
        "-ExpectedMachineIdentityDigest 'b78a83fa8a529aac1bcbc52961ea3d225e30b09ff5287fcdabf3589b0ca0b23e' "
        f"-AncestorBoundary {_ps(base)} -DefinitionImport"
    )
    machine = _module(
        "Get-PhaseAMachineDigest -MachineGuid '01234567-89ab-cdef-0123-456789abcdef' "
        "-SmbiosUuid 'fedcba98-7654-3210-fedc-ba9876543210' -DefinitionImport"
    ).stdout.strip()
    validation = validation.replace("b78a83fa8a529aac1bcbc52961ea3d225e30b09ff5287fcdabf3589b0ca0b23e", machine)
    host_signature = host_receipt.with_suffix(".sig")
    valid_signature = host_signature.read_bytes()
    host_signature.write_bytes(b"\x00" * len(valid_signature))
    stress = _module(
        f"$source=Get-Content {_ps(source_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$hostBinding=Get-Content {_ps(host_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$expected=@{{{_ps(source_receipt.stem)}=$source;{_ps(host_receipt.stem)}=$hostBinding}};"
        f"for($i=0;$i -lt 20;$i++){{try{{{validation}|Out-Null}}catch{{}};"
        f"$exclusive=[IO.FileStream]::new({_ps(host_signature)},[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$exclusive.Dispose()}};"
        f"Move-Item {_ps(host_signature)} {_ps(str(host_signature)+'.moved')};Move-Item {_ps(str(host_signature)+'.moved')} {_ps(host_signature)};'released'"
    )
    assert stress.stdout.strip() == "released"
    host_signature.write_bytes(valid_signature)
    missing = _module(
        f"$source=Get-Content {_ps(source_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$expected=@{{{_ps(source_receipt.stem)}=$source}};try{{{validation}|Out-Null}}catch{{}};"
        f"Move-Item {_ps(host_receipt)} {_ps(str(host_receipt)+'.moved')};Move-Item {_ps(str(host_receipt)+'.moved')} {_ps(host_receipt)};"
        f"Move-Item {_ps(final/'store.json')} {_ps(str(final/'store.json')+'.moved')};Move-Item {_ps(str(final/'store.json')+'.moved')} {_ps(final/'store.json')};'released'"
    )
    assert missing.stdout.strip() == "released"
    residue = final / "bundles" / ".staging-33333333-3333-4333-8333-333333333333"
    residue.write_bytes(b"host-tree-drift")
    _protect(residue)
    host_failure = _module(
        f"$source=Get-Content {_ps(source_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$hostBinding=Get-Content {_ps(host_receipt)} -Raw|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"$expected=@{{{_ps(source_receipt.stem)}=$source;{_ps(host_receipt.stem)}=$hostBinding}};"
        f"try{{{validation}|Out-Null}}catch{{}};Move-Item {_ps(host_receipt)} {_ps(str(host_receipt)+'.moved')};"
        f"Move-Item {_ps(str(host_receipt)+'.moved')} {_ps(host_receipt)};'released'"
    )
    assert host_failure.stdout.strip() == "released"
    residue.unlink()
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
    assert request.parent.name == operation

    second_stage = parent / ".provisioning-22222222-2222-4222-8222-222222222222"
    second_stage.mkdir()
    (second_stage / "other.bin").write_bytes(b"other")
    second_before = json.loads(_module(
        f"Get-PhaseADirectoryManifest -Root {_ps(parent)}|ConvertTo-Json -Depth 12 -Compress"
    ).stdout)
    second_target = _module(f"Get-PhaseATargetDigest -Path {_ps(second_stage)}").stdout.strip()
    second_operation = "b" * 64
    second_args = auth_args.replace(operation, second_operation).replace(target, second_target).replace(
        _sha(_canonical(before)), _sha(_canonical(second_before))
    )
    second_authorization = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} {second_args} -CreateUnsigned -OutputDirectory {_ps(bootstrap)}"
    ).stdout.strip())
    second_signature = second_authorization.with_suffix(".sig")
    second_signature.write_bytes(_sign(private, second_authorization.read_bytes()))
    _protect(second_authorization)
    _protect(second_signature)
    second_common = common.replace(str(stage), str(second_stage)).replace(
        str(authorization), str(second_authorization)
    ).replace(str(authorization_sig), str(second_signature))
    second_result = json.loads(_run_ps(second_common + "|ConvertTo-Json -Compress", timeout=120).stdout)
    second_request = Path(second_result["CompletionRequestPath"])
    assert second_result["State"] == "COMPLETION_REQUIRED"
    assert second_request.parent.name == second_operation
    assert request.exists() and second_request.exists()

    conflict = request.parent / f"{'0'*64}.json"
    conflict.write_bytes(b"{}")
    _protect(conflict)
    conflicting = _run_ps("try{" + common + "|Out-Null;'missed'}catch{'rejected'}", timeout=120)
    assert conflicting.stdout.strip() == "rejected"


def test_exact_sidecar_vocabulary_and_provider_interoperability(tmp_path: Path):
    module = MODULE.read_text(encoding="utf-8")
    generator = NEW_RECEIPT.read_text(encoding="utf-8")
    assert "legacy-authority-destruction" not in module + generator
    assert "applypilot.phase-a.legacy-sidecar-destruction-authorization" in module + generator
    assert "applypilot.phase-a.legacy-sidecar-destruction-completion" in module + generator
    exact_providers = ("postgres", "llm-api", "review-api", "other")
    for old_provider in ("oauth-refresh-token", "api-key", "session-cookie", "password"):
        assert f"'{old_provider}'" not in module + generator
    _, public, key_hash = _new_keypair(tmp_path, "vocabulary")
    output = tmp_path / "unsigned"
    sid = _current_sid()
    command = (
        f"& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.legacy-sidecar-destruction-authorization "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ApprovedCommit {_ps('1'*40)} -OperationId {_ps('2'*64)} -TargetIdentityDigest {_ps('3'*64)} "
        f"-BeforeManifestSha256 {_ps('4'*64)} -ExpectedAfterManifestSha256 {_ps('5'*64)} "
        f"-EvidenceBundleSha256 {_ps('6'*64)} -CredentialInventoryRoot {_ps('7'*64)} "
        f"-CredentialRevocationSetRoot {_ps('8'*64)} -OperatorSid {_ps(sid)} "
        f"-CreatedAtUtc '2026-07-14T12:34:56Z' -CreateUnsigned -OutputDirectory {_ps(output)}"
    )
    receipt = Path(_run_ps(command).stdout.strip())
    expected = (
        f'{{"approvedCommit":"{"1"*40}","beforeManifestSha256":"{"4"*64}",'
        f'"createdAtUtc":"2026-07-14T12:34:56Z","credentialInventoryRoot":"{"7"*64}",'
        f'"credentialRevocationSetRoot":"{"8"*64}","evidenceBundleSha256":"{"6"*64}",'
        f'"expectedAfterManifestSha256":"{"5"*64}","operationId":"{"2"*64}",'
        f'"operatorSid":"{sid}","operatorSigningKeySpkiSha256":"{key_hash}",'
        '"receiptType":"applypilot.phase-a.legacy-sidecar-destruction-authorization",'
        f'"schemaVersion":1,"targetIdentityDigest":"{"3"*64}"}}'
    ).encode("ascii")
    assert receipt.read_bytes() == expected
    completion = Path(_run_ps(
        f"& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.legacy-sidecar-destruction-completion "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-ApprovedCommit {_ps('1'*40)} -OperationId {_ps('2'*64)} -AuthorizationReceiptSha256 {_ps('6'*64)} "
        f"-ActualAfterManifestSha256 {_ps('5'*64)} -ExpectedAfterManifestSha256 {_ps('5'*64)} "
        f"-CreatedAtUtc '2026-07-14T12:35:00Z' -CreateUnsigned -OutputDirectory {_ps(output/'completion')}"
    ).stdout.strip())
    expected_completion = (
        f'{{"actualAfterManifestSha256":"{"5"*64}","approvedCommit":"{"1"*40}",'
        f'"authorizationReceiptSha256":"{"6"*64}","createdAtUtc":"2026-07-14T12:35:00Z",'
        f'"operationId":"{"2"*64}","operatorSigningKeySpkiSha256":"{key_hash}",'
        '"receiptType":"applypilot.phase-a.legacy-sidecar-destruction-completion",'
        '"result":"COMPLETE","schemaVersion":1}'
    ).encode("ascii")
    assert completion.read_bytes() == expected_completion
    revocation_common = (
        f"-ReceiptType applypilot.phase-a.credential-revocation -OperatorSigningSpkiPath {_ps(public)} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} -ApprovedCommit {_ps('1'*40)} "
        f"-CredentialReferenceDigest {_ps('2'*64)} -RevokedAtUtc '2026-07-14T12:30:00Z' "
        f"-StaleProbeAtUtc '2026-07-14T12:31:00Z' -ProviderEvidenceSha256 {_ps('3'*64)} "
        f"-MachineIdentityDigest {_ps('4'*64)} -Nonce {_ps('5'*64)} "
        f"-CreatedAtUtc '2026-07-14T12:35:00Z' -CreateUnsigned"
    )
    for provider in exact_providers:
        provider_output = output / provider
        provider_receipt = Path(_run_ps(
            f"& {_ps(NEW_RECEIPT)} {revocation_common} -ProviderClass {_ps(provider)} "
            f"-OutputDirectory {_ps(provider_output)}"
        ).stdout.strip())
        assert json.loads(provider_receipt.read_bytes())["providerClass"] == provider
    for provider in ("oauth-refresh-token", "api-key", "session-cookie", "password"):
        rejected = _run_ps(
            "try{" + f"& {_ps(NEW_RECEIPT)} {revocation_common} -ProviderClass {_ps(provider)} "
            f"-OutputDirectory {_ps(output / ('rejected-' + provider))}|Out-Null;'missed'" +
            "}catch{'rejected'}"
        )
        assert rejected.stdout.strip() == "rejected"


def test_manifest_digest_formulas_match_independent_fixed_vectors():
    volume = 0x0102030405060708
    file_id = bytes(range(16))
    canonical_path = r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\Case\File.bin"
    relative = r"Nested\File.bin"
    expected_target = hashlib.sha256(
        b"applypilot.phase-a.target.v1\0"
        + struct.pack("<Q", volume)
        + file_id
        + struct.pack("<I", len(canonical_path.encode("utf-8")))
        + canonical_path.encode("utf-8")
    ).hexdigest()
    expected_relative = hashlib.sha256(
        b"applypilot.phase-a.relative-path.v1\0" + relative.encode("utf-8")
    ).hexdigest()
    result = json.loads(_module(
        "$m=Get-Module PhaseAEvidenceStore;& $m {param($v,$id,$path,$relative)"
        "[pscustomobject]@{Target=Get-PhaseATargetIdentityDigestFromParts $v $id $path;"
        "Relative=Get-PhaseARelativePathDigest $relative}} "
        f"{volume} {_ps(file_id.hex())} {_ps(canonical_path)} {_ps(relative)}|ConvertTo-Json -Compress"
    ).stdout)
    assert result == {"Target": expected_target, "Relative": expected_relative}


def test_manifest_uses_held_volume_guid_paths_and_preserves_relative_casing(tmp_path: Path):
    root = tmp_path / "ManifestRoot"
    nested = root / "NestedCase"
    nested.mkdir(parents=True)
    target = nested / "FileCase.bin"
    target.write_bytes(b"content")
    manifest = json.loads(_module(
        f"Get-PhaseADirectoryManifest -Root {_ps(root)}|ConvertTo-Json -Depth 8 -Compress"
    ).stdout)
    held = json.loads(_module(
        "$m=Get-Module PhaseAEvidenceStore;& $m {param($p)"
        "$h=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($p,$false);try{"
        "$i=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($h);"
        "[pscustomobject]@{Volume=$i.VolumeSerialNumber;FileId=$i.FileId;Path=[ApplyPilot.PhaseA.EvidenceNative]::GetVolumeGuidPath($h)}}finally{$h.Dispose()}} "
        f"{_ps(target)}|ConvertTo-Json -Compress"
    ).stdout)
    relative = r"NestedCase\FileCase.bin"
    expected_relative = hashlib.sha256(
        b"applypilot.phase-a.relative-path.v1\0" + relative.encode("utf-8")
    ).hexdigest()
    path_bytes = held["Path"].encode("utf-8")
    expected_object = hashlib.sha256(
        b"applypilot.phase-a.target.v1\0"
        + struct.pack("<Q", held["Volume"])
        + bytes.fromhex(held["FileId"])
        + struct.pack("<I", len(path_bytes))
        + path_bytes
    ).hexdigest()
    file_entry = next(entry for entry in manifest["entries"] if entry["kind"] == "file")
    assert file_entry["relativePathDigest"] == expected_relative
    assert file_entry["objectIdentityDigest"] == expected_object


@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_adjudication_install_rejects_changed_authenticated_candidate_set(tmp_path: Path, mutation: str):
    private, public, key_hash = _new_keypair(tmp_path, "adjudication")
    sid = _current_sid()
    store = tmp_path / "store"
    store.mkdir()
    _protect(store)
    for leaf in ("bundles", "adjudications", "operations"):
        (store / leaf).mkdir()
        _protect(store / leaf)
    source, preimage_a, preimage_b = "a" * 64, "b" * 64, "c" * 64
    candidate_a, candidate_b = "d" * 64, "e" * 64

    def add_bundle(preimage: str) -> Path:
        path = store / "bundles" / f"{source}-{preimage}.apeb"
        path.write_bytes(preimage.encode("ascii"))
        _protect(path)
        return path

    add_bundle(preimage_a)
    second = add_bundle(preimage_b) if mutation == "remove" else None
    authenticator = (
        "$auth={param($c)$candidate=if($c.PreimageSha256 -ceq '" + preimage_a + "'){'"
        + candidate_a + "'}else{'" + candidate_b + "'};[ordered]@{"
        "sourceIdentityDigest=$c.SourceIdentityDigest;preimageSha256=$c.PreimageSha256;"
        "candidateBundleSha256=$candidate}};"
    )
    unsigned = tmp_path / "unsigned"
    receipt = Path(_run_ps(
        authenticator + f"& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.evidence-adjudication "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-SourceIdentityDigest {_ps(source)} -SelectedBundleSha256 {_ps(candidate_a)} "
        f"-StoreRoot {_ps(store)} -CanonicalOperatorSid {_ps(sid)} -DefinitionBundleAuthenticator $auth "
        f"-DefinitionImport -Nonce {_ps('f'*64)} -CreatedAtUtc '2026-07-14T12:34:56Z' "
        f"-CreateUnsigned -OutputDirectory {_ps(unsigned)}"
    ).stdout.strip())
    signature = receipt.with_suffix(".sig")
    signature.write_bytes(_sign(private, receipt.read_bytes()))
    _protect(receipt)
    _protect(signature)
    if mutation == "add":
        add_bundle(preimage_b)
    else:
        second.unlink()
    expected = receipt.read_text(encoding="utf-8").replace("'", "''")
    result = _module(
        authenticator + f"$expected='{expected}'|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"try{{Install-PhaseASignedReceipt -ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)} "
        f"-StoreRoot {_ps(store)} -OperatorSigningSpkiPath {_ps(public)} "
        f"-ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        "-ExpectedReceiptType applypilot.phase-a.evidence-adjudication -ExpectedBindings $expected "
        "-DefinitionBundleAuthenticator $auth -DefinitionImport|Out-Null;'missed'}catch{'rejected'}"
    )
    assert result.stdout.strip() == "rejected"
    assert list((store / "adjudications").iterdir()) == []


def test_adjudication_install_revalidates_after_publication_race(tmp_path: Path):
    private, public, key_hash = _new_keypair(tmp_path, "adjudication-race")
    sid = _current_sid()
    store = tmp_path / "store"
    store.mkdir()
    _protect(store)
    for leaf in ("bundles", "adjudications", "operations"):
        (store / leaf).mkdir()
        _protect(store / leaf)
    source, preimage, candidate = "a" * 64, "b" * 64, "c" * 64
    bundle = store / "bundles" / f"{source}-{preimage}.apeb"
    bundle.write_bytes(b"one")
    _protect(bundle)
    authenticator = (
        "$auth={param($c)[ordered]@{sourceIdentityDigest=$c.SourceIdentityDigest;"
        "preimageSha256=$c.PreimageSha256;candidateBundleSha256='" + candidate + "'}};"
    )
    receipt = Path(_run_ps(
        authenticator + f"& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.evidence-adjudication "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-SourceIdentityDigest {_ps(source)} -SelectedBundleSha256 {_ps(candidate)} -StoreRoot {_ps(store)} "
        f"-CanonicalOperatorSid {_ps(sid)} -DefinitionBundleAuthenticator $auth -DefinitionImport "
        f"-Nonce {_ps('d'*64)} -CreatedAtUtc '2026-07-14T12:34:56Z' -CreateUnsigned -OutputDirectory {_ps(tmp_path/'unsigned')}"
    ).stdout.strip())
    signature = receipt.with_suffix(".sig")
    signature.write_bytes(_sign(private, receipt.read_bytes()))
    _protect(receipt)
    _protect(signature)
    expected = receipt.read_text(encoding="utf-8").replace("'", "''")
    added = store / "bundles" / f"{source}-{'e'*64}.apeb"
    race = (
        f"$race={{param($pair)[IO.File]::WriteAllBytes({_ps(added)},[Text.Encoding]::ASCII.GetBytes('two'));"
        f"$m=Get-Module PhaseAEvidenceStore;& $m {{param($p,$s)Set-PhaseAProtectedAcl $p $s -File}} {_ps(added)} {_ps(sid)}}};"
    )
    result = _module(
        authenticator + race + f"$expected='{expected}'|ConvertFrom-Json -AsHashtable -DateKind String;"
        f"try{{Install-PhaseASignedReceipt -ReceiptPath {_ps(receipt)} -SignaturePath {_ps(signature)} "
        f"-StoreRoot {_ps(store)} -OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        "-ExpectedReceiptType applypilot.phase-a.evidence-adjudication -ExpectedBindings $expected "
        "-DefinitionBundleAuthenticator $auth -BeforeFinalPairRevalidation $race -DefinitionImport|Out-Null;'missed'}catch{'rejected'}"
    )
    assert result.stdout.strip() == "rejected"


def test_empty_bundle_store_is_enumerable_but_cannot_create_adjudication(tmp_path: Path):
    _, public, key_hash = _new_keypair(tmp_path, "empty-adjudication")
    sid = _current_sid()
    store = tmp_path / "store"
    store.mkdir()
    _protect(store)
    for leaf in ("bundles", "adjudications", "operations"):
        (store / leaf).mkdir()
        _protect(store / leaf)
    count = _module(
        f"@(Get-PhaseAAuthenticatedBundleCandidates -StoreRoot {_ps(store)} "
        f"-CanonicalOperatorSid {_ps(sid)} -SourceIdentityDigest {_ps('a'*64)}).Count"
    )
    assert count.stdout.strip() == "0"
    rejected = _run_ps(
        f"try{{& {_ps(NEW_RECEIPT)} -ReceiptType applypilot.phase-a.evidence-adjudication "
        f"-OperatorSigningSpkiPath {_ps(public)} -ExpectedOperatorSigningKeySpkiSha256 {_ps(key_hash)} "
        f"-SourceIdentityDigest {_ps('a'*64)} -SelectedBundleSha256 {_ps('b'*64)} -StoreRoot {_ps(store)} "
        f"-CanonicalOperatorSid {_ps(sid)} -Nonce {_ps('c'*64)} -CreatedAtUtc '2026-07-14T12:34:56Z' "
        f"-CreateUnsigned -OutputDirectory {_ps(tmp_path/'unsigned')}|Out-Null;'missed'}}catch{{'rejected'}}"
    )
    assert rejected.stdout.strip() == "rejected"
