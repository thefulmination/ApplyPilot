<#
Install a worker runtime with a unique database principal already mapped by the
controller. This script never accepts or stores migration/admin credentials.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$NodeId,
    [Parameter(Mandatory = $true)][ValidateSet('apply', 'linkedin', 'compute', 'discovery')]
    [string]$Contract,
    [Parameter(Mandatory = $true)][string]$MappedRole,
    [Parameter(Mandatory = $true)][string]$FleetPgDsn,
    [string]$InstallDir = "C:\ApplyPilot",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($MappedRole -in @("postgres", "fleet_worker")) {
    throw "MappedRole must be a unique per-node login role"
}
if (-not $NodeId.Trim()) { throw "NodeId must not be empty" }

$env:APPLYPILOT_SETUP_DSN = $FleetPgDsn
$env:APPLYPILOT_SETUP_NODE = $NodeId
$env:APPLYPILOT_SETUP_CONTRACT = $Contract
$env:APPLYPILOT_SETUP_ROLE = $MappedRole
try {
    $validation = @'
import os
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from applypilot.fleet.pg_roles import validate_runtime_principal

dsn = os.environ["APPLYPILOT_SETUP_DSN"]
params = conninfo_to_dict(dsn)
expected_role = os.environ["APPLYPILOT_SETUP_ROLE"]
if params.get("user") != expected_role:
    raise SystemExit("FLEET_PG_DSN user does not match MappedRole")
if params.get("user") in {"postgres", "fleet_worker"}:
    raise SystemExit("admin and shared fleet worker DSNs are forbidden")
if not params.get("password"):
    raise SystemExit("FLEET_PG_DSN must contain an explicit password")
with psycopg.connect(dsn, row_factory=dict_row) as conn:
    identity = validate_runtime_principal(
        conn,
        worker_id=os.environ["APPLYPILOT_SETUP_NODE"],
        contract=os.environ["APPLYPILOT_SETUP_CONTRACT"],
    )
print(identity.session_user)
'@
    $validatedRole = (& $Python -c $validation).Trim()
    if ($LASTEXITCODE -ne 0 -or $validatedRole -ne $MappedRole) {
        throw "mapped runtime principal validation failed"
    }

    [Environment]::SetEnvironmentVariable("FLEET_PG_DSN", $FleetPgDsn, "User")
    [Environment]::SetEnvironmentVariable("APPLYPILOT_WORKER_ID", $NodeId, "User")
    [Environment]::SetEnvironmentVariable("APPLYPILOT_WORKER_CONTRACT", $Contract, "User")
    $env:FLEET_PG_DSN = $FleetPgDsn
    $env:APPLYPILOT_WORKER_ID = $NodeId
    $env:APPLYPILOT_WORKER_CONTRACT = $Contract
    Write-Host "[worker-setup] validated mapped role '$MappedRole' for $NodeId/$Contract"
    Write-Host "[worker-setup] runtime variables installed for the current Windows user"
    Write-Host "[worker-setup] checkout location: $InstallDir"
}
finally {
    Remove-Item Env:APPLYPILOT_SETUP_DSN -ErrorAction SilentlyContinue
    Remove-Item Env:APPLYPILOT_SETUP_NODE -ErrorAction SilentlyContinue
    Remove-Item Env:APPLYPILOT_SETUP_CONTRACT -ErrorAction SilentlyContinue
    Remove-Item Env:APPLYPILOT_SETUP_ROLE -ErrorAction SilentlyContinue
}
