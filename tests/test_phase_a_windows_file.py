from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "PhaseAWindowsFile.psm1"
PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"


def _ps_literal(value: os.PathLike[str] | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_ps(body: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
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
    assert identity["FinalPath"].casefold() == str(wrapper).casefold()
    result = _run_ps(
        "(Get-Module PhaseAWindowsFile).ExportedCommands.Keys | "
        "Sort-Object | ConvertTo-Json -Compress\n"
    )
    assert json.loads(result.stdout) == sorted(
        [
            "Assert-PhaseAFileIdentity",
            "Get-PhaseAFileIdentity",
            "Open-PhaseAValidatedDirectoryLease",
            "Open-PhaseAValidatedFile",
            "Rename-PhaseAFileNoReplace",
            "Set-PhaseAFileDeletionDisposition",
        ]
    )


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
    source.write_text("source", encoding="utf-8")
    result = _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(source)} "
        f"-Access ReadWriteDelete -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(source.name)}\n"
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
    occupied.write_text("occupied", encoding="utf-8")
    assert _run_ps(
        f"$handle = Open-PhaseAValidatedFile -Path {_ps_literal(destination)} "
        f"-Access ReadWriteDelete -AuthorizedRoot {_ps_literal(tmp_path)} "
        f"-AuthorizedBasename {_ps_literal(destination.name)}\n"
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
