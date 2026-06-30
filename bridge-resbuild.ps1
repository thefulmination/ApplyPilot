# bridge-resbuild.ps1  --  ONE command to put YOUR review work (the res_build fit map /
# kept jobs) into ApplyPilot's apply gate.
#
#   Preview, no writes:        .\bridge-resbuild.ps1 -DryRun
#   First canary (top 15):     .\bridge-resbuild.ps1 -Limit 15
#   Promote all offsite picks: .\bridge-resbuild.ps1
#   Reverse the last promote:  .\bridge-resbuild.ps1 -Revert
#
# Pipeline:
#   1) res_build  applypilotExportApplyList.ts --decider=human   (the jobs YOU reviewed + kept;
#      pure file I/O, never touches the brain)  ->  apply-list JSONL
#   2) applypilot resbuild-promote  (writes audit_score + decision_source so the apply gate
#      selects them -- INCLUDING the ones ApplyPilot's own ranker scores below threshold).
#      Run through run-applypilot.ps1 so APPLYPILOT_DB_PATH points at the LIVE brain and the
#      db is backed up afterwards.
#
# LinkedIn is excluded by default (its apply lane is separate / supervised). Promotion only
# STAGES jobs apply-eligible in the brain -- nothing applies until you run the fleet. Every
# real promote writes a snapshot next to the apply-list; -Revert restores the prior state.
param(
    [int]$Limit = 0,
    [string]$Decider = "human",                 # human = jobs YOU kept; model = the ranker's picks; either
    [switch]$DryRun,
    [switch]$Revert,
    [string[]]$ExcludeHost = @("linkedin.com"),
    [switch]$IncludeApplied
)
$ErrorActionPreference = "Stop"

$ApplyPilot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ResBuild = "C:\Users\JStal\OneDrive\Documents\New project 9"
$RunAP = Join-Path $ApplyPilot "run-applypilot.ps1"
$OutDir = Join-Path $ApplyPilot ".applypilot"
$ListPath = Join-Path $OutDir "resbuild-apply-list.jsonl"
$SnapPath = Join-Path $OutDir "resbuild-apply-list.snapshot.json"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if ($Revert) {
    if (-not (Test-Path -LiteralPath $SnapPath)) {
        throw "No snapshot at $SnapPath -- nothing to revert (run a real promote first)."
    }
    & $RunAP resbuild-revert $SnapPath
    exit $LASTEXITCODE
}

# 1) Export the jobs you kept (res_build; file I/O only, no brain access).
Write-Host "[bridge] exporting kept jobs from res_build (decider=$Decider)..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $ResBuild)) { throw "res_build tree not found at $ResBuild (pass the right path)." }
$tsx = Join-Path $ResBuild "node_modules\.bin\tsx"
if (-not (Test-Path -LiteralPath $tsx)) { throw "tsx not found at $tsx -- run 'npm install' in res_build first." }
Push-Location $ResBuild
try {
    $env:NODE_OPTIONS = "--max-old-space-size=4096"
    & $tsx "src/cli/applypilotExportApplyList.ts" "--decider=$Decider" "--out=$ListPath"
    if ($LASTEXITCODE -ne 0) { throw "exporter failed (exit $LASTEXITCODE)" }
}
finally { Pop-Location }

# 2) Promote into the LIVE brain via run-applypilot.ps1 (sets APPLYPILOT_DB_PATH + backs up).
$promoteArgs = @("resbuild-promote", $ListPath, "--snapshot", $SnapPath, "--scale", "ten")
foreach ($h in $ExcludeHost) { $promoteArgs += @("--exclude-host", $h) }
if ($Limit -gt 0) { $promoteArgs += @("--limit", "$Limit") }
if ($IncludeApplied) { $promoteArgs += "--include-applied" }
if ($DryRun) { $promoteArgs += "--dry-run" }

Write-Host "[bridge] promoting into the live brain (LinkedIn excluded; reversible)..." -ForegroundColor Cyan
& $RunAP @promoteArgs
exit $LASTEXITCODE
