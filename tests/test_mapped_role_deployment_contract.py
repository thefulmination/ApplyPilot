"""Static deployment contracts for mapped worker identities and pg_hba safety."""

from pathlib import Path
import json
import os
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]


def _script(name: str) -> str:
    return (REPO / "scripts" / name).read_text(encoding="utf-8")


def _root_script(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def test_database_hardening_script_requires_identity_manifest_and_receipts() -> None:
    script = _script("setup-fleet-pg-tailscale.ps1")
    for required in (
        "[Parameter(Mandatory = $true)][string]$NodeId",
        "[Parameter(Mandatory = $true)][ValidateSet('apply', 'linkedin', 'compute', 'discovery')]",
        "[Parameter(Mandatory = $true)][string]$Role",
        "[Parameter(Mandatory = $true)][string]$RegrantManifest",
        "expected_service_roles",
        "database_owner_role",
        "retired_admin_roles",
        "AclRegrant",
        "ensure_fleet_worker_role",
        "RollbackSql",
        "ReceiptPath",
        "pg_hba_file_rules",
        "Copy-Item -LiteralPath $HbaPath",
        "[System.IO.File]::Replace",
        "pg_reload_conf",
        "reject",
        "Assert-SafeScalar",
        "include directives are unsupported",
        "Assert-ApplyPilotHbaEffectiveOrder",
        "Assert-ApplyPilotOutputPaths",
        'data.pop("rollback_sql")',
        '"escalation_required": True',
        '"in_doubt": True',
        '"deployment_committed"',
        "Write-ApplyPilotDurableAtomicJson",
        "[IO.FileOptions]::WriteThrough",
        "$stream.Flush($true)",
        "Get-Acl -LiteralPath $fullPath",
        "Set-Acl -LiteralPath $temporary",
    ):
        assert required in script

    assert "Add-Content" not in script
    assert "Set-Content" not in script
    assert "user=postgres" not in script
    assert "user=fleet_worker" not in script
    assert "regrant_sql=tuple(raw.get(" not in script


def test_rollback_executor_verifies_hash_and_enforces_atomic_break_glass_order(tmp_path: Path) -> None:
    script_path = REPO / "scripts" / "rollback-fleet-pg-role.py"
    script = script_path.read_text(encoding="utf-8")
    main_body = script.split("def main() -> int:", 1)[1]
    assert "hmac.compare_digest" in script
    assert "with conn.transaction():" in script
    assert "psql --single-transaction --set=ON_ERROR_STOP=on" in script
    assert main_body.index("psycopg.connect") < main_body.index("_restore_hba_and_reload(conn")
    assert main_body.index("_restore_hba_and_reload(conn") < main_body.index("conn.execute(rollback_sql)")

    receipt = tmp_path / "receipt.json"
    rollback = tmp_path / "rollback.sql"
    rollback.write_text("SELECT 1;\n", encoding="utf-8")
    receipt.write_text(
        '{"rollback_sql_sha256":"' + ("0" * 64) + '","inventory":{"infrastructure_superuser_roles":["postgres"]}}',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["APPLYPILOT_ADMIN_PG_DSN"] = "must-not-be-used-before-hash-verification"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--receipt",
            str(receipt),
            "--rollback-sql",
            str(rollback),
        ],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SHA-256 does not match" in result.stderr
    assert "connection" not in result.stderr.lower()


def test_worker_installer_requires_unique_mapped_dsn_and_never_admin_credentials() -> None:
    script = _script("setup-fleet-worker.ps1")
    for required in (
        "[Parameter(Mandatory = $true)][string]$NodeId",
        "[Parameter(Mandatory = $true)][ValidateSet('apply', 'linkedin', 'compute', 'discovery')]",
        "[Parameter(Mandatory = $true)][string]$MappedRole",
        "[Parameter(Mandatory = $true)][string]$FleetPgDsn",
        "conninfo_to_dict",
        "validate_runtime_principal",
        "FLEET_PG_DSN",
        "APPLYPILOT_WORKER_ID",
        "APPLYPILOT_WORKER_CONTRACT",
    ):
        assert required in script

    assert 'Read-Host "  Postgres password' not in script
    assert "user=postgres" not in script
    assert "user=fleet_worker" not in script
    assert "APPLYPILOT_FLEET_DSN" not in script


def test_root_installers_are_thin_mapped_role_forwarders() -> None:
    worker = _root_script("setup-fleet-worker.ps1")
    hardener = _root_script("setup-fleet-pg-tailscale.ps1")

    assert "scripts\\setup-fleet-worker.ps1" in worker
    assert "& $target @forward" in worker
    assert "scripts\\setup-fleet-pg-tailscale.ps1" in hardener
    assert "& $target @forward" in hardener

    for script in (worker, hardener):
        assert 'if ($MappedRole -in @("postgres", "fleet_worker"))' in script or (
            'if ($Role -in @("postgres", "fleet_worker"))' in script
        )
        assert "ensure_fleet_worker_role" not in script
        assert "APPLYPILOT_ADMIN_PG_DSN" not in script
        assert "APPLYPILOT_SUPER_DSN" not in script
        assert "Add-Content" not in script
        assert "pgpass" not in script.lower()


def test_root_installers_reject_admin_and_shared_roles_before_forwarding() -> None:
    commands = (
        (
            "setup-fleet-worker.ps1",
            ["-NodeId", "node-a", "-Contract", "apply", "-FleetPgDsn", "invalid"],
            "-MappedRole",
        ),
        (
            "setup-fleet-pg-tailscale.ps1",
            [
                "-NodeId",
                "node-a",
                "-Contract",
                "apply",
                "-RegrantManifest",
                "missing.json",
            ],
            "-Role",
        ),
    )
    for script, base_args, role_arg in commands:
        for forbidden in ("postgres", "fleet_worker"):
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(REPO / script),
                    *base_args,
                    role_arg,
                    forbidden,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0
            assert "unique per-node" in result.stderr
            assert "forwarding mapped-role setup" not in result.stdout


def _run_hba_function(command: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    prefix = (
        ". './scripts/setup-fleet-pg-tailscale.ps1' -NodeId test-node -Contract apply "
        "-Role mapped_test -RegrantManifest unused.json; "
    )
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", prefix + command],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_hba_contract_rejects_controls_and_includes_and_places_block_first(tmp_path: Path) -> None:
    include_hba = tmp_path / "included.conf"
    include_hba.write_text("include_if_exists 'other.conf'\nhost all all 0.0.0.0/0 trust\n", encoding="utf-8")
    env = os.environ.copy()
    env["AP_TEST_HBA"] = str(include_hba)
    include_result = _run_hba_function(
        "try { Get-ApplyPilotHbaLines -HbaPath $env:AP_TEST_HBA; exit 9 } "
        "catch { if ($_.Exception.Message -notlike '*include directives are unsupported*') { throw }; exit 0 }",
        env=env,
    )
    assert include_result.returncode == 0, include_result.stderr

    control_result = _run_hba_function(
        'try { Assert-SafeScalar -Name Role -Value "mapped`nrole"; exit 9 } '
        "catch { if ($_.Exception.Message -notlike '*control-character*') { throw }; exit 0 }"
    )
    assert control_result.returncode == 0, control_result.stderr

    order_result = _run_hba_function(
        "$source=@('local all all trust','host all all 0.0.0.0/0 trust',"
        "'hostssl all all ::0/0 trust'); "
        "$candidate=New-ApplyPilotManagedHba -Lines $source -Database fleet -Role mapped_test "
        "-TailnetCidr 100.64.0.0/10; "
        "Assert-ApplyPilotHbaEffectiveOrder -Lines $candidate -Database fleet -Role mapped_test "
        "-TailnetCidr 100.64.0.0/10; "
        "if (($candidate | Select-String '^host ').LineNumber[0] -ne 3) { exit 8 }; exit 0"
    )
    assert order_result.returncode == 0, order_result.stderr


def test_hba_path_collision_leaves_original_byte_exact(tmp_path: Path) -> None:
    hba = tmp_path / "pg_hba.conf"
    hba.write_bytes(b"local all all trust\r\nhost all all 0.0.0.0/0 trust\r\n")
    env = os.environ.copy()
    env["AP_TEST_HBA"] = str(hba)
    env["AP_TEST_DIR"] = str(tmp_path)
    result = _run_hba_function(
        "$before=[Convert]::ToHexString([IO.File]::ReadAllBytes($env:AP_TEST_HBA)); "
        "$candidate=@('host fleet mapped_test 100.64.0.0/10 scram-sha-256',"
        "'host fleet mapped_test 0.0.0.0/0 reject','host fleet mapped_test ::0/0 reject'); "
        "try { Invoke-ApplyPilotHbaReplacement -HbaPath $env:AP_TEST_HBA -CandidateLines $candidate "
        "-Database fleet -Role mapped_test -TailnetCidr 100.64.0.0/10 "
        "-ReceiptPath $env:AP_TEST_HBA -RollbackSql (Join-Path $env:AP_TEST_DIR 'rollback.sql') "
        "-PreflightBackup (Join-Path $env:AP_TEST_DIR 'pre.bak') "
        "-ReplaceBackup (Join-Path $env:AP_TEST_DIR 'replace.bak') "
        "-CandidatePath (Join-Path $env:AP_TEST_DIR 'candidate') "
        "-RestorePath (Join-Path $env:AP_TEST_DIR 'restore') "
        "-ValidateAndReload { throw 'must not run' } -ReloadOriginal { throw 'must not run' } "
        "-FinalizeOutput { throw 'must not run' }; exit 9 } "
        "catch { if ($_.Exception.Message -notlike '*path collision*') { throw } }; "
        "$after=[Convert]::ToHexString([IO.File]::ReadAllBytes($env:AP_TEST_HBA)); "
        "if ($before -ne $after) { exit 8 }; exit 0",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert hba.read_bytes() == b"local all all trust\r\nhost all all 0.0.0.0/0 trust\r\n"


def test_hba_post_write_failure_restores_hba_and_outputs_byte_exact(tmp_path: Path) -> None:
    hba = tmp_path / "pg_hba.conf"
    receipt = tmp_path / "receipt.json"
    rollback = tmp_path / "rollback.sql"
    originals = {
        hba: b"local all all trust\r\nhost all all 0.0.0.0/0 trust\r\n",
        receipt: b'{"role":"mapped_test"}\n',
        rollback: b"REVOKE CONNECT ON DATABASE fleet FROM mapped_test;\n",
    }
    for path, data in originals.items():
        path.write_bytes(data)
    env = os.environ.copy()
    env.update(
        AP_TEST_HBA=str(hba),
        AP_TEST_RECEIPT=str(receipt),
        AP_TEST_ROLLBACK=str(rollback),
        AP_TEST_DIR=str(tmp_path),
    )
    result = _run_hba_function(
        "$candidate=@('# BEGIN APPLYPILOT MAPPED ROLE mapped_test',"
        "'host fleet mapped_test 100.64.0.0/10 scram-sha-256',"
        "'host fleet mapped_test 0.0.0.0/0 reject','host fleet mapped_test ::0/0 reject',"
        "'# END APPLYPILOT MAPPED ROLE mapped_test','host all all 0.0.0.0/0 trust'); "
        "try { Invoke-ApplyPilotHbaReplacement -HbaPath $env:AP_TEST_HBA -CandidateLines $candidate "
        "-Database fleet -Role mapped_test -TailnetCidr 100.64.0.0/10 "
        "-ReceiptPath $env:AP_TEST_RECEIPT -RollbackSql $env:AP_TEST_ROLLBACK "
        "-PreflightBackup (Join-Path $env:AP_TEST_DIR 'post.pre.bak') "
        "-ReplaceBackup (Join-Path $env:AP_TEST_DIR 'post.replace.bak') "
        "-CandidatePath (Join-Path $env:AP_TEST_DIR 'post.candidate') "
        "-RestorePath (Join-Path $env:AP_TEST_DIR 'post.restore') "
        "-ValidateAndReload {} -ReloadOriginal {} -FinalizeOutput { "
        "[IO.File]::WriteAllText($env:AP_TEST_RECEIPT,'corrupt'); throw 'post-write failure' }; exit 9 } "
        "catch { if ($_.Exception.Message -notlike '*post-write failure*') { throw }; exit 0 }",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    for path, data in originals.items():
        assert path.read_bytes() == data


def test_abrupt_committed_receipt_finalization_leaves_prior_in_doubt_receipt_intact(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    original = b'{"status":"database_reconciled","escalation_required":true,"in_doubt":true}\n'
    receipt.write_bytes(original)
    env = os.environ.copy()
    env["AP_TEST_RECEIPT"] = str(receipt)
    result = _run_hba_function(
        "$committed=[pscustomobject]@{status='deployment_committed';"
        "escalation_required=$false;in_doubt=$false}; "
        "Write-ApplyPilotDurableAtomicJson -Path $env:AP_TEST_RECEIPT -Value $committed "
        "-BeforeReplace { Stop-Process -Id $PID -Force }; exit 9",
        env=env,
    )
    assert result.returncode != 0
    assert receipt.read_bytes() == original
    parsed = json.loads(receipt.read_text(encoding="utf-8"))
    assert parsed == {
        "status": "database_reconciled",
        "escalation_required": True,
        "in_doubt": True,
    }
    durable_temps = list(tmp_path.glob(".receipt.json.*.tmp"))
    assert len(durable_temps) == 1
    assert json.loads(durable_temps[0].read_text(encoding="utf-8")) == {
        "status": "deployment_committed",
        "escalation_required": False,
        "in_doubt": False,
    }
    durable_temps[0].unlink()
    completed = _run_hba_function(
        "$before=Get-Acl -LiteralPath $env:AP_TEST_RECEIPT; "
        "$beforeAccess=@($before.Access | ForEach-Object { "
        "'{0}|{1}|{2}|{3}|{4}' -f $_.IdentityReference,$_.FileSystemRights,"
        "$_.AccessControlType,$_.InheritanceFlags,$_.PropagationFlags } | Sort-Object); "
        "$committed=[pscustomobject]@{status='deployment_committed';"
        "escalation_required=$false;in_doubt=$false}; "
        "Write-ApplyPilotDurableAtomicJson -Path $env:AP_TEST_RECEIPT -Value $committed; "
        "$after=Get-Acl -LiteralPath $env:AP_TEST_RECEIPT; "
        "$afterAccess=@($after.Access | ForEach-Object { "
        "'{0}|{1}|{2}|{3}|{4}' -f $_.IdentityReference,$_.FileSystemRights,"
        "$_.AccessControlType,$_.InheritanceFlags,$_.PropagationFlags } | Sort-Object); "
        "if ($before.Owner -ne $after.Owner -or $before.Group -ne $after.Group -or "
        "(Compare-Object $beforeAccess $afterAccess)) { exit 8 }; exit 0",
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "status": "deployment_committed",
        "escalation_required": False,
        "in_doubt": False,
    }
