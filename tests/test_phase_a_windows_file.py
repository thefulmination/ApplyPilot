from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "PhaseAWindowsFile.psm1"
EVIDENCE_MODULE_PATH = REPO_ROOT / "scripts" / "PhaseAEvidenceStore.psm1"
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Phase A native file-handle tests require Windows",
)
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _ps_literal(value: os.PathLike[str] | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(body: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None, "PowerShell is required for Windows handle tests"
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"Import-Module {_ps_literal(MODULE_PATH)} -Force\n"
        + body
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"PowerShell failed ({result.returncode})\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _start_ps(body: str) -> subprocess.Popen[str]:
    assert PWSH is not None
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        [PWSH, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_raw_ps(body: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"PowerShell failed ({result.returncode})\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _protected_descriptor_script(variable: str = "$descriptor") -> str:
    return (
        f"$evidenceModule = Import-Module {_ps_literal(EVIDENCE_MODULE_PATH)} "
        "-Force -PassThru\n"
        "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
        f"[byte[]]{variable} = & $evidenceModule {{ param($operatorSid) "
        "Get-PhaseAProtectedSecurityDescriptorBytes $operatorSid -File } $sid\n"
        f"Import-Module {_ps_literal(MODULE_PATH)} -Force\n"
    )


def _new_file_script(
    path: Path,
    root: Path,
    *,
    basename: str | None = None,
    pattern: str | None = None,
) -> str:
    authorization = (
        f"-AuthorizedBasename {_ps_literal(basename)}"
        if basename is not None
        else f"-AuthorizedBasenamePattern {_ps_literal(pattern or '')}"
    )
    return (
        _protected_descriptor_script()
        + f"$handle = New-PhaseAValidatedFile -Path {_ps_literal(path)} "
        f"-AuthorizedRoot {_ps_literal(root)} {authorization} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
    )


def _probe_identity_material(path: Path) -> dict[str, object]:
    from ctypes import wintypes

    class FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", FILE_ID_128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x80000000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x00200000,
        None,
    )
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        info = FILE_ID_INFO()
        assert get_info(
            handle, 18, ctypes.byref(info), ctypes.sizeof(info)
        )
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_final_path(handle, buffer, len(buffer), 0x1)
        assert 0 < length < len(buffer)
        return {
            "Volume": int(info.VolumeSerialNumber),
            "FileId": bytes(info.FileId.Identifier).hex().upper(),
            "Path": buffer.value.replace("/", "\\"),
        }
    finally:
        assert close_handle(handle)


def _open_result(
    path: Path | str,
    root: Path,
    basename: str,
    access: str = "Read",
) -> dict[str, object]:
    result = _run_ps(
        "try {\n"
        f"  $handle = Open-PhaseAValidatedFile -Path {_ps_literal(path)} "
        f"-Access {_ps_literal(access)} -AuthorizedRoot {_ps_literal(root)} "
        f"-AuthorizedBasename {_ps_literal(basename)}\n"
        "  try {\n"
        "    $identity = Get-PhaseAFileIdentity -Handle $handle\n"
        "    [pscustomobject]@{ Ok = $true; Identity = $identity } | "
        "ConvertTo-Json -Compress\n"
        "  } finally { $handle.Dispose() }\n"
        "} catch {\n"
        "  [pscustomobject]@{ Ok = $false; Error = $_.Exception.Message } | "
        "ConvertTo-Json -Compress\n"
        "}\n"
    )
    return json.loads(result.stdout.strip())


def test_regular_file_identity_and_exported_surface(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("Write-Output 'ok'\n", encoding="utf-8")

    opened = _open_result(wrapper, tmp_path, wrapper.name)

    assert opened["Ok"] is True
    identity = opened["Identity"]
    assert identity["NumberOfLinks"] == 1
    assert 0 <= identity["VolumeSerialNumber"] <= 0xFFFFFFFFFFFFFFFF
    assert re.fullmatch(r"[0-9A-F]{32}", identity["FileId"])
    assert identity["FinalPath"].casefold() == str(wrapper).casefold()
    result = _run_ps(
        "(Get-Module PhaseAWindowsFile).ExportedCommands.Keys | "
        "Sort-Object | ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == sorted(
        [
            "Assert-PhaseAFileIdentity",
            "Get-PhaseAFileIdentity",
            "Get-PhaseAFileIdentityMaterial",
            "New-PhaseAValidatedFile",
            "Open-PhaseAValidatedDirectoryLease",
            "Open-PhaseAValidatedFile",
            "Open-PhaseAValidatedFileWriteStream",
            "Rename-PhaseAFileNoReplace",
            "Set-PhaseAFileDeletionDisposition",
        ]
    )


def test_force_reload_rejects_incompatible_loaded_type_and_accepts_current_type():
    incompatible = _run_raw_ps(
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -TypeDefinition 'namespace ApplyPilot.PhaseA { "
        "public static class WindowsFile {} }'\n"
        "try {\n"
        f"  Import-Module {_ps_literal(MODULE_PATH)} -Force\n"
        "  'missed'\n"
        "} catch { $_.Exception.Message }\n"
    )
    message = incompatible.stdout.strip().lower()
    assert "restart" in message
    assert "incompatible" in message

    compatible = _run_raw_ps(
        "$ErrorActionPreference = 'Stop'\n"
        f"Import-Module {_ps_literal(MODULE_PATH)} -Force\n"
        f"Import-Module {_ps_literal(MODULE_PATH)} -Force\n"
        "[ApplyPilot.PhaseA.WindowsFile]::ContractVersion\n"
    )
    assert compatible.stdout.strip()


def test_existing_stage_open_authorizes_only_explicit_publish_basename(tmp_path: Path):
    digest = "a" * 64
    stage = tmp_path / f".{digest}.receipt-stage"
    final = tmp_path / f"{digest}.json"
    other = tmp_path / f"{digest}.sig"
    stage.write_bytes(b"receipt")
    result = _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(stage)} "
        "-Access ReadWriteDelete "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(stage.name)} "
        f"-AuthorizedRenameBasename {_ps_literal(final.name)}\n"
        "try {\n"
        f"  try {{ Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(other)} }} "
        "catch { $otherRejected = $true }\n"
        f"  Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(final)}\n"
        "  $identity = Get-PhaseAFileIdentity -Handle $handle\n"
        "  [pscustomobject]@{ OtherRejected = $otherRejected; Final = $identity.FinalPath } | "
        "ConvertTo-Json -Compress\n"
        "} finally { $handle.Dispose() }\n"
    )
    assert json.loads(result.stdout) == {
        "OtherRejected": True,
        "Final": str(final),
    }
    assert final.read_bytes() == b"receipt"


def test_real_evidence_descriptor_is_final_acl_compatible_at_creation(tmp_path: Path):
    staged = tmp_path / "real-helper.stage"
    result = _run_ps(
        _new_file_script(staged, tmp_path, basename=staged.name)
        + "try {\n"
        "  & $evidenceModule { param($held, $operatorSid) "
        "Assert-PhaseAProtectedFileHandleAcl $held $operatorSid } $handle $sid\n"
        "  $bytes = [ApplyPilot.PhaseA.EvidenceNative]::GetFileSecurityDescriptor("
        "$handle.FileHandle)\n"
        "  $raw = [Security.AccessControl.RawSecurityDescriptor]::new($bytes, 0)\n"
        "  $raw.ControlFlags.ToString()\n"
        "} finally { $handle.Dispose() }\n"
    )
    assert set(result.stdout.strip().split(", ")) == {
        "DiscretionaryAclPresent",
        "DiscretionaryAclAutoInherited",
        "DiscretionaryAclProtected",
        "SelfRelative",
    }


def test_new_file_uses_protected_descriptor_at_creation_and_create_new(tmp_path: Path):
    staged = tmp_path / "receipt.stage"
    result = _run_ps(
        _new_file_script(staged, tmp_path, basename=staged.name)
        + "try {\n"
        f"  $acl = Get-Acl -LiteralPath {_ps_literal(staged)}\n"
        "  $duplicateRejected = $false\n"
        "  try {\n"
        f"    $duplicate = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "  } catch { $duplicateRejected = $true }\n"
        "  [pscustomobject]@{ Protected = $acl.AreAccessRulesProtected; "
        "Owner = $acl.Owner; DuplicateRejected = $duplicateRejected } | "
        "ConvertTo-Json -Compress\n"
        "} finally { $handle.Dispose() }\n"
    )
    payload = json.loads(result.stdout)
    assert payload["Protected"] is True
    assert payload["DuplicateRejected"] is True
    assert staged.read_bytes() == b""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "CreateNew" in source
    assert "Set-Acl" not in source


def test_second_process_observes_only_protected_acl_while_creation_handle_is_held(
    tmp_path: Path,
):
    staged = tmp_path / "watched.stage"
    marker = tmp_path / "created.marker"
    observed = tmp_path / "observed.json"
    watcher = _start_ps(
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$marker = {_ps_literal(marker)}\n"
        f"$path = {_ps_literal(staged)}\n"
        f"$observed = {_ps_literal(observed)}\n"
        "$deadline = [DateTime]::UtcNow.AddSeconds(10)\n"
        "while ([DateTime]::UtcNow -lt $deadline) {\n"
        "  if (Test-Path -LiteralPath $marker) {\n"
        "    try {\n"
        "      $acl = Get-Acl -LiteralPath $path -ErrorAction Stop\n"
        "      [pscustomobject]@{ Protected = $acl.AreAccessRulesProtected; "
        "Owner = $acl.Owner } | ConvertTo-Json -Compress | "
        "Set-Content -LiteralPath $observed -Encoding utf8\n"
        "      exit 0\n"
        "    } catch {}\n"
        "  }\n"
        "  Start-Sleep -Milliseconds 5\n"
        "}\n"
        "exit 3\n"
    )
    creator = _run_ps(
        _new_file_script(staged, tmp_path, basename=staged.name)
        + "try {\n"
        f"  Set-Content -LiteralPath {_ps_literal(marker)} -Value ready -NoNewline\n"
        "$deadline = [DateTime]::UtcNow.AddSeconds(5)\n"
        f"  while (-not (Test-Path -LiteralPath {_ps_literal(observed)}) -and "
        "[DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 5 }\n"
        f"  (Test-Path -LiteralPath {_ps_literal(observed)}).ToString().ToLowerInvariant()\n"
        "} finally { $handle.Dispose() }\n"
    )
    stdout, stderr = watcher.communicate(timeout=10)
    assert creator.stdout.strip() == "true"
    assert watcher.returncode == 0, (stdout, stderr)
    assert json.loads(observed.read_text(encoding="utf-8-sig"))["Protected"] is True


def test_new_file_rejects_invalid_names_and_wrong_descriptor(tmp_path: Path):
    invalid_paths = [
        tmp_path / "wild*.stage",
        f"{tmp_path / 'ads.stage'}:stream",
        f"{tmp_path / 'alias.stage'}.",
    ]
    for path in invalid_paths:
        result = _run_ps(
            _protected_descriptor_script()
            + "try {\n"
            f"  $h = New-PhaseAValidatedFile -Path {_ps_literal(path)} "
            f"-AuthorizedRoot {_ps_literal(tmp_path)} "
            "-AuthorizedBasenamePattern '\\A[a-z]+\\.stage\\z' "
            "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
            "  try { 'missed' } finally { $h.Dispose() }\n"
            "} catch { 'rejected' }\n"
        )
        assert result.stdout.strip() == "rejected"

    weak = tmp_path / "weak.stage"
    result = _run_ps(
        _protected_descriptor_script("$realDescriptor")
        + "$raw = [Security.AccessControl.RawSecurityDescriptor]::new("
        "$realDescriptor, 0)\n"
        "$control = $raw.ControlFlags -band "
        "(-bnot [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)\n"
        "$weak = [Security.AccessControl.RawSecurityDescriptor]::new("
        "$control, $raw.Owner, $raw.Group, $null, $raw.DiscretionaryAcl)\n"
        "$bytes = [byte[]]::new($weak.BinaryLength)\n"
        "$weak.GetBinaryForm($bytes, 0)\n"
        "try {\n"
        f"  $h = New-PhaseAValidatedFile -Path {_ps_literal(weak)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(weak.name)} "
        "-SecurityDescriptor $bytes -Access ReadWriteDelete\n"
        "  try { 'missed' } finally { $h.Dispose() }\n"
        "} catch { 'rejected' }\n"
    )
    assert result.stdout.strip() == "rejected"
    assert not weak.exists()


def test_identity_material_matches_independent_win32_probe_and_is_copied(tmp_path: Path):
    staged = tmp_path / "identity.stage"
    result = _run_ps(
        _new_file_script(staged, tmp_path, basename=staged.name)
        + "try {\n"
        "  $first = Get-PhaseAFileIdentityMaterial -Handle $handle\n"
        "  $original = [Convert]::ToHexString($first.FileId)\n"
        "  $first.FileId[0] = $first.FileId[0] -bxor 255\n"
        "  $second = Get-PhaseAFileIdentityMaterial -Handle $handle\n"
        "  [pscustomobject]@{ Volume = $second.VolumeSerialNumber; "
        "FileId = [Convert]::ToHexString($second.FileId); Original = $original; "
        "Path = $second.VolumeGuidPath; Length = $second.FileId.Length } | "
        "ConvertTo-Json -Compress\n"
        "} finally { $handle.Dispose() }\n"
    )
    payload = json.loads(result.stdout)
    independent = _probe_identity_material(staged)
    assert payload["Length"] == 16
    assert payload["FileId"] == payload["Original"]
    assert payload["Volume"] == independent["Volume"]
    assert payload["FileId"] == independent["FileId"]
    assert payload["Path"] == independent["Path"]
    assert re.fullmatch(
        r"\\\\\?\\Volume\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}\\.+",
        payload["Path"],
    )


def test_new_handle_pattern_authorizes_same_handle_no_replace_rename(tmp_path: Path):
    source = tmp_path / "receipt.0123456789abcdef.stage"
    destination = tmp_path / "receipt.json"
    occupied = tmp_path / "occupied.json"
    result = _run_ps(
        _new_file_script(
            source,
            tmp_path,
            pattern=r"\A(?:receipt\.(?:[0-9a-f]{16}\.stage|json)|occupied\.json)\z",
        )
        + "try {\n"
        "  $before = Get-PhaseAFileIdentityMaterial -Handle $handle\n"
        "  $stream = Open-PhaseAValidatedFileWriteStream -Handle $handle\n"
        "  try { $bytes = [Text.Encoding]::UTF8.GetBytes('payload'); "
        "$stream.Write($bytes, 0, $bytes.Length); $stream.Flush() } finally { $stream.Dispose() }\n"
        "  $afterStream = Get-PhaseAFileIdentityMaterial -Handle $handle\n"
        f"  Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(destination)}\n"
        "  $after = Get-PhaseAFileIdentityMaterial -Handle $handle\n"
        "  [pscustomobject]@{ Same = ([Convert]::ToHexString($before.FileId) -eq "
        "[Convert]::ToHexString($afterStream.FileId) -and "
        "[Convert]::ToHexString($before.FileId) -eq "
        "[Convert]::ToHexString($after.FileId)); Path = $after.VolumeGuidPath } | "
        "ConvertTo-Json -Compress\n"
        "} finally { $handle.Dispose() }\n"
    )
    payload = json.loads(result.stdout)
    assert payload["Same"] is True
    assert destination.read_text(encoding="utf-8") == "payload"

    occupied.write_text("occupied", encoding="utf-8")
    failed = _run_ps(
        _new_file_script(
            source,
            tmp_path,
            pattern=r"\A(?:receipt\.(?:[0-9a-f]{16}\.stage|json)|occupied\.json)\z",
        )
        + "try {\n"
        f"  try {{ Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(occupied)}; 'missed' }} "
        "catch { 'rejected' }\n"
        "} finally { $handle.Dispose() }\n"
    )
    assert failed.stdout.strip() == "rejected"
    assert occupied.read_text(encoding="utf-8") == "occupied"


def test_new_file_handle_retains_ancestor_leases_and_releases_synchronously(
    tmp_path: Path,
):
    parent = tmp_path / "active"
    parent.mkdir()
    staged = parent / "lease.stage"
    renamed = tmp_path / "released"
    result = _run_ps(
        _new_file_script(staged, parent, basename=staged.name)
        + "$held = $false\n"
        f"try {{ Rename-Item -LiteralPath {_ps_literal(parent)} -NewName {_ps_literal(renamed.name)} }} "
        "catch { $held = $true }\n"
        "$handle.Dispose()\n"
        f"Rename-Item -LiteralPath {_ps_literal(parent)} -NewName {_ps_literal(renamed.name)}\n"
        "[pscustomobject]@{ Held = $held; Released = (Test-Path -LiteralPath "
        f"{_ps_literal(renamed)}) }} | ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == {"Held": True, "Released": True}


def test_creation_exception_stress_does_not_leak_handles_or_delete_existing_leaf(
    tmp_path: Path,
):
    staged = tmp_path / "stress.stage"
    staged.write_text("original", encoding="utf-8")
    result = _run_ps(
        _protected_descriptor_script()
        + "$rejected = 0\n"
        "for ($index = 0; $index -lt 50; $index++) {\n"
        "  try {\n"
        f"    $h = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "  } catch { $rejected++ }\n"
        "}\n"
        f"$probe = [IO.File]::Open({_ps_literal(staged)}, [IO.FileMode]::Open, "
        "[IO.FileAccess]::ReadWrite, [IO.FileShare]::None)\n"
        "$probe.Dispose()\n"
        "$rejected\n"
    )
    assert result.stdout.strip() == "50"
    assert staged.read_text(encoding="utf-8") == "original"


def test_post_create_validation_failure_cleans_residue_and_allows_retry(tmp_path: Path):
    staged = tmp_path / "validation-failure.stage"
    result = _run_ps(
        _protected_descriptor_script()
        + "$flags = [Reflection.BindingFlags]::NonPublic -bor "
        "[Reflection.BindingFlags]::Static\n"
        "$hooks = [ApplyPilot.PhaseA.WindowsFile].Assembly.GetType("
        "'ApplyPilot.PhaseA.WindowsFileTestHooks', $true)\n"
        "$failValidation = $hooks.GetField('FailPostCreateValidation', $flags)\n"
        "$failValidation.SetValue($null, $true)\n"
        "try {\n"
        f"  $failed = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "} catch { $validationRejected = $true } finally { "
        "$failValidation.SetValue($null, $false) }\n"
        f"$removed = -not (Test-Path -LiteralPath {_ps_literal(staged)})\n"
        f"$retry = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "try { [pscustomobject]@{ Rejected = $validationRejected; Removed = $removed; "
        "Retry = $true } | ConvertTo-Json -Compress } finally { $retry.Dispose() }\n"
    )
    assert json.loads(result.stdout) == {
        "Rejected": True,
        "Removed": True,
        "Retry": True,
    }


def test_delete_by_handle_cleanup_failure_reports_residue_and_blocks_retry(tmp_path: Path):
    staged = tmp_path / "cleanup-failure.stage"
    result = _run_ps(
        _protected_descriptor_script()
        + "$flags = [Reflection.BindingFlags]::NonPublic -bor "
        "[Reflection.BindingFlags]::Static\n"
        "$hooks = [ApplyPilot.PhaseA.WindowsFile].Assembly.GetType("
        "'ApplyPilot.PhaseA.WindowsFileTestHooks', $true)\n"
        "$failValidation = $hooks.GetField('FailPostCreateValidation', $flags)\n"
        "$failCleanup = $hooks.GetField('FailCleanupDelete', $flags)\n"
        "$failValidation.SetValue($null, $true); $failCleanup.SetValue($null, $true)\n"
        "try {\n"
        f"  $failed = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "} catch { $message = $_.Exception.ToString() } finally { "
        "$failValidation.SetValue($null, $false); $failCleanup.SetValue($null, $false) }\n"
        f"$residue = Test-Path -LiteralPath {_ps_literal(staged)}\n"
        "try {\n"
        f"  $retry = New-PhaseAValidatedFile -Path {_ps_literal(staged)} "
        f"-AuthorizedRoot {_ps_literal(tmp_path)} -AuthorizedBasename {_ps_literal(staged.name)} "
        "-SecurityDescriptor $descriptor -Access ReadWriteDelete\n"
        "} catch { $retryRejected = $true }\n"
        "[pscustomobject]@{ CleanupReported = ($message -match 'cleanup failed'); "
        "Residue = $residue; RetryRejected = $retryRejected } | ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == {
        "CleanupReported": True,
        "Residue": True,
        "RetryRejected": True,
    }
    assert staged.exists()


def test_rename_rejects_cross_volume_destination_before_native_mutation(tmp_path: Path):
    source = tmp_path / "cross.stage"
    result = _run_ps(
        _new_file_script(
            source,
            tmp_path,
            pattern=r"\A(?:cross\.stage|destination\.stage)\z",
        )
        + "try {\n"
        "  try { Rename-PhaseAFileNoReplace -Handle $handle "
        "-Destination 'D:\\destination.stage'; 'missed' } catch { 'rejected' }\n"
        "} finally { $handle.Dispose() }\n"
    )
    assert result.stdout.strip() == "rejected"
    assert source.exists()


def test_directory_lease_accepts_regular_directory(tmp_path: Path):
    result = _run_ps(
        f"$lease = Open-PhaseAValidatedDirectoryLease -Path {_ps_literal(tmp_path)}\n"
        "try { (Get-PhaseAFileIdentity -Handle $lease).FinalPath } "
        "finally { $lease.Dispose() }\n"
    )
    assert result.stdout.strip().casefold() == str(tmp_path).casefold()


def test_file_handle_holds_and_releases_directory_leases(tmp_path: Path):
    parent = tmp_path / "active"
    parent.mkdir()
    renamed = tmp_path / "renamed"
    wrapper = parent / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    result = _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access Read -AuthorizedRoot {_ps_literal(parent)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "$held = $false\n"
        f"try {{ Rename-Item -LiteralPath {_ps_literal(parent)} -NewName {_ps_literal(renamed.name)} }} "
        "catch { $held = $true }\n"
        "$handle.Dispose()\n"
        "$deadline = [DateTime]::UtcNow.AddSeconds(2)\n"
        "do {\n"
        "  try {\n"
        f"    Rename-Item -LiteralPath {_ps_literal(parent)} -NewName {_ps_literal(renamed.name)}\n"
        "    $released = $true\n"
        "  } catch { Start-Sleep -Milliseconds 25 }\n"
        "} while (-not $released -and [DateTime]::UtcNow -lt $deadline)\n"
        "[pscustomobject]@{ Held = $held; Released = $released } | "
        "ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == {"Held": True, "Released": True}


def test_disposal_is_synchronous_and_does_not_poll(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    result = _run_ps(
        "$types = [Collections.Generic.HashSet[string]]::new()\n"
        "for ($index = 0; $index -lt 100; $index++) {\n"
        f"  $handle = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access Read -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "  $null = $types.Add($handle.GetType().FullName)\n"
        "  $handle.Dispose()\n"
        "  $handle.Dispose()\n"
        f"  $probe = [IO.File]::Open({_ps_literal(wrapper)}, [IO.FileMode]::Open, "
        "[IO.FileAccess]::ReadWrite, [IO.FileShare]::None)\n"
        "  $probe.Dispose()\n"
        "}\n"
        "$types | ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == "ApplyPilot.PhaseA.ValidatedHandle"
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "ThreadPool.QueueUserWorkItem" not in source
    assert "Thread.Sleep" not in source


def test_rejects_leaf_symlink(tmp_path: Path):
    target = tmp_path / "target.ps1"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "wrapper.ps1"
    os.symlink(target, link)

    assert _open_result(link, tmp_path, link.name)["Ok"] is False


def test_rejects_ancestor_junction(tmp_path: Path):
    target = tmp_path / "real"
    target.mkdir()
    wrapper = target / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert _open_result(junction / wrapper.name, junction, wrapper.name)["Ok"] is False


def test_rejects_leaf_hardlink_count_greater_than_one(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    os.link(wrapper, tmp_path / "second-name.ps1")

    assert _open_result(wrapper, tmp_path, wrapper.name)["Ok"] is False


def test_validation_failure_does_not_leak_exclusive_target_handle(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    second_name = tmp_path / "second-name.ps1"
    wrapper.write_text("target", encoding="utf-8")
    os.link(wrapper, second_name)
    result = _run_ps(
        "$rejected = $false\n"
        "try {\n"
        f"  $failed = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access Read -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "} catch { $rejected = $true }\n"
        f"Remove-Item -LiteralPath {_ps_literal(second_name)} -Force\n"
        f"$retry = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access Read -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "try { [pscustomobject]@{ Rejected = $rejected; Retry = $true } | "
        "ConvertTo-Json -Compress } finally { $retry.Dispose() }\n"
    )
    assert json.loads(result.stdout) == {"Rejected": True, "Retry": True}


def test_rejects_alternate_data_stream(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    ads = f"{wrapper}:evidence"
    Path(ads).write_text("stream", encoding="utf-8")

    assert _open_result(ads, tmp_path, wrapper.name)["Ok"] is False


def test_rejects_trailing_dot_alias(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")

    assert _open_result(f"{wrapper}.", tmp_path, wrapper.name)["Ok"] is False


def test_rejects_file_outside_authorized_root(tmp_path: Path):
    root = tmp_path / "authorized"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    wrapper = outside / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")

    assert _open_result(wrapper, root, wrapper.name)["Ok"] is False


def test_rejects_non_local_path(tmp_path: Path):
    unc = r"\\localhost\C$\wrapper.ps1"

    assert _open_result(unc, Path(r"\\localhost\C$"), "wrapper.ps1")["Ok"] is False


def test_rejects_basename_mismatch_and_directory_in_file_position(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("target", encoding="utf-8")
    assert _open_result(wrapper, tmp_path, "other.ps1")["Ok"] is False

    directory = tmp_path / "directory.ps1"
    directory.mkdir()
    assert _open_result(directory, tmp_path, directory.name)["Ok"] is False


def test_identity_assertion_detects_replacement_before_mutation(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("first", encoding="utf-8")
    result = _run_ps(
        f"$snapshot = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access Read -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "$expected = Get-PhaseAFileIdentity -Handle $snapshot\n"
        "$snapshot.Dispose()\n"
        f"Remove-Item -LiteralPath {_ps_literal(wrapper)} -Force\n"
        f"Set-Content -LiteralPath {_ps_literal(wrapper)} -Value second -NoNewline\n"
        f"$mutation = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access ReadWrite -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "try {\n"
        "  try { Assert-PhaseAFileIdentity -Handle $mutation -Expected $expected; 'missed' }\n"
        "  catch { 'rejected' }\n"
        "} finally { $mutation.Dispose() }\n"
    )
    assert result.stdout.strip() == "rejected"


def test_rename_is_same_handle_and_does_not_replace(tmp_path: Path):
    source = tmp_path / "source.ps1"
    destination = tmp_path / "renamed.ps1"
    result = _run_ps(
        _new_file_script(
            source,
            tmp_path,
            pattern=r"\A(?:source|renamed|occupied)\.ps1\z",
        )
        + "$stream = Open-PhaseAValidatedFileWriteStream -Handle $handle\n"
        "$bytes = [Text.Encoding]::UTF8.GetBytes('source')\n"
        "$stream.Write($bytes, 0, $bytes.Length); $stream.Flush(); $stream.Dispose()\n"
        "$before = Get-PhaseAFileIdentity -Handle $handle\n"
        "try {\n"
        f"  Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(destination)}\n"
        "  $after = Get-PhaseAFileIdentity -Handle $handle\n"
        "  Assert-PhaseAFileIdentity -Handle $handle -Expected $after\n"
        "  [pscustomobject]@{ Same = ($before.VolumeSerialNumber -eq $after.VolumeSerialNumber "
        "-and $before.FileId -eq $after.FileId); FinalPath = $after.FinalPath } | "
        "ConvertTo-Json -Compress\n"
        "} finally { $handle.Dispose() }\n"
    )
    payload = json.loads(result.stdout)
    assert payload == {"Same": True, "FinalPath": str(destination)}
    assert destination.read_text(encoding="utf-8") == "source"

    occupied = tmp_path / "occupied.ps1"
    second = tmp_path / "second.ps1"
    occupied.write_text("occupied", encoding="utf-8")
    assert _run_ps(
        _new_file_script(
            second,
            tmp_path,
            pattern=r"\A(?:second|occupied)\.ps1\z",
        )
        +
        "try {\n"
        f"  try {{ Rename-PhaseAFileNoReplace -Handle $handle -Destination {_ps_literal(occupied)}; 'missed' }} "
        "catch { 'rejected' }\n"
        "} finally { $handle.Dispose() }\n"
    ).stdout.strip() == "rejected"
    assert occupied.read_text(encoding="utf-8") == "occupied"


def test_same_handle_delete_succeeds_with_read_write_delete(tmp_path: Path):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text("sensitive", encoding="utf-8")
    result = _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access ReadWriteDelete -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "try { Set-PhaseAFileDeletionDisposition -Handle $handle } "
        "finally { $handle.Dispose() }\n"
    )
    assert result.returncode == 0
    assert not wrapper.exists()


@pytest.mark.parametrize("access", ["Read", "ReadWrite"])
def test_delete_rejected_without_delete_access(tmp_path: Path, access: str):
    wrapper = tmp_path / f"{access}.ps1"
    wrapper.write_text("retain", encoding="utf-8")
    result = _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(wrapper)} "
        f"-Access {_ps_literal(access)} -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(wrapper.name)}\n"
        "try {\n"
        "  try { Set-PhaseAFileDeletionDisposition -Handle $handle; 'missed' }\n"
        "  catch { 'rejected' }\n"
        "} finally { $handle.Dispose() }\n"
    )
    assert result.stdout.strip() == "rejected"
    assert wrapper.read_text(encoding="utf-8") == "retain"
