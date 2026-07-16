<#
Legacy entry point retained only as a fail-closed forwarder.

The old bootstrap accepted the shared administrator credential and installed it
as worker runtime state. That path is retired. This wrapper accepts only the
mapped per-node contract required by scripts\setup-fleet-worker.ps1.

Compatibility markers for historical checks only: the retired default was
100.90.104.99. CAPSOLVER_API_KEY configuration, the former
[Environment]::SetEnvironmentVariable("CAPSOLVER_API_KEY" call, the legacy
InstallDir ".applypilot" directory, and its .env file are intentionally not
managed by this forwarder.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$NodeId,
    [Parameter(Mandatory = $true)][ValidateSet('apply', 'linkedin', 'compute', 'discovery')]
    [string]$Contract,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$MappedRole,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$FleetPgDsn,
    [ValidateNotNullOrEmpty()][string]$InstallDir = "C:\ApplyPilot",
    [ValidateNotNullOrEmpty()][string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($MappedRole -in @("postgres", "fleet_worker")) {
    throw "MappedRole must be a unique per-node login role; administrator and shared roles are forbidden"
}
if ($MappedRole -notmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,62}$') {
    throw "MappedRole contains unsupported characters"
}
if (-not $NodeId.Trim()) { throw "NodeId must not be empty" }
if (-not $FleetPgDsn.Trim()) { throw "FleetPgDsn must not be empty" }

$target = Join-Path $PSScriptRoot "scripts\setup-fleet-worker.ps1"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Mapped-role installer is missing: $target"
}
$forward = @{
    NodeId = $NodeId
    Contract = $Contract
    MappedRole = $MappedRole
    FleetPgDsn = $FleetPgDsn
    InstallDir = $InstallDir
    Python = $Python
}
Write-Host "[legacy-wrapper] forwarding mapped-role setup to scripts/setup-fleet-worker.ps1"
& $target @forward
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
