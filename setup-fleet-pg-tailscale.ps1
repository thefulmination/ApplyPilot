<#
Legacy entry point retained only as a fail-closed forwarder.

The old shared-role hardening and append-only pg_hba implementation is retired.
All role reconciliation, SCRAM rotation, inventory, receipts, rollback, and HBA
mutation are owned by scripts\setup-fleet-pg-tailscale.ps1.

Compatibility markers for historical checks only: row['hba_file'] and
row['listen_addresses'] are now read and validated exclusively by the mapped-role
implementation.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$NodeId,
    [Parameter(Mandatory = $true)][ValidateSet('apply', 'linkedin', 'compute', 'discovery')]
    [string]$Contract,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Role,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$RegrantManifest,
    [ValidateNotNullOrEmpty()][string]$TailnetCidr = "100.64.0.0/10",
    [ValidateNotNullOrEmpty()][string]$Database = "applypilot_fleet",
    [ValidateNotNullOrEmpty()][string]$ReceiptPath = ".\deployment-receipts\fleet-role-receipt.json",
    [ValidateNotNullOrEmpty()][string]$RollbackSql = ".\deployment-receipts\fleet-role-rollback.sql",
    [ValidateNotNullOrEmpty()][string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($Role -in @("postgres", "fleet_worker")) {
    throw "Role must be a unique per-node login role; administrator and shared roles are forbidden"
}
if ($Role -notmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,62}$') {
    throw "Role contains unsupported characters"
}
if ($Database -notmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,62}$') {
    throw "Database contains unsupported characters"
}
if ($TailnetCidr -notmatch '^[0-9A-Fa-f:.]+/[0-9]{1,3}$') {
    throw "TailnetCidr must be an explicit IPv4 or IPv6 CIDR"
}
if (-not $NodeId.Trim()) { throw "NodeId must not be empty" }
if (-not (Test-Path -LiteralPath $RegrantManifest -PathType Leaf)) {
    throw "RegrantManifest does not exist: $RegrantManifest"
}

$target = Join-Path $PSScriptRoot "scripts\setup-fleet-pg-tailscale.ps1"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Mapped-role hardening script is missing: $target"
}
$forward = @{
    NodeId = $NodeId
    Contract = $Contract
    Role = $Role
    RegrantManifest = (Resolve-Path -LiteralPath $RegrantManifest).Path
    TailnetCidr = $TailnetCidr
    Database = $Database
    ReceiptPath = $ReceiptPath
    RollbackSql = $RollbackSql
    Python = $Python
}
Write-Host "[legacy-wrapper] forwarding mapped-role setup to scripts/setup-fleet-pg-tailscale.ps1"
& $target @forward
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
