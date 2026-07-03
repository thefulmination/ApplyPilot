# backup-brain.ps1 [-KeepBackups 7] [-RegisterTask]
#
#   Nightly INTEGRITY-GATED backup of the SQLite brain (applypilot.db) -- the single
#   irreplaceable asset (labels, outcomes, application history; nothing regenerates it).
#   launcher.py already writes rolling backups DURING apply runs, but nothing backs the
#   brain up when the pipeline is quiet -- this closes that gap (sibling of
#   backup-fleet-pg.ps1, same conventions).
#
#   Backups go to LOCAL disk (%LOCALAPPDATA%\ApplyPilot\brain-backups), NOT OneDrive --
#   OneDrive sync locks on large CHANGING files caused corruption. A finished backup file
#   never changes, so the newest one is additionally MIRRORED to OneDrive
#   (ApplyPilot-Backups) for off-machine survival of this failing laptop.
#
#   Integrity gate: PRAGMA quick_check FIRST; a corrupt live DB refuses to back up
#   (never rotate good history away in favor of a bad copy).
#
#   One-time setup (daily 03:00 user-level task; PG backup runs 03:30 -- staggered):
#     .\backup-brain.ps1 -RegisterTask
#   Manual run / verify:
#     .\backup-brain.ps1
param(
    [int]$KeepBackups = 7,
    [switch]$RegisterTask,
    [string]$BrainPath = $(if ($env:APPLYPILOT_DB_PATH) { $env:APPLYPILOT_DB_PATH }
                           else { Join-Path $env:LOCALAPPDATA "ApplyPilot\applypilot.db" }),
    [string]$BackupDir = (Join-Path $env:LOCALAPPDATA "ApplyPilot\brain-backups"),
    [string]$MirrorDir = (Join-Path $HOME "OneDrive\Documents\ApplyPilot-Backups"),
    [int]$KeepMirrors = 2
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($RegisterTask) {
    $script = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -KeepBackups $KeepBackups"
    $trigger = New-ScheduledTaskTrigger -Daily -At 3:00am
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "ApplyPilot brain backup" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Nightly integrity-gated SQLite online backup of the brain (applypilot.db)" -Force | Out-Null
    Write-Host "[brain-backup] scheduled task registered: daily 03:00 -> $BackupDir (+ newest mirrored to $MirrorDir)" -ForegroundColor Green
    return
}

if (-not (Test-Path $BrainPath)) { throw "brain not found at $BrainPath -- refusing (wrong box or APPLYPILOT_DB_PATH unset?)." }
$py = @((Join-Path $repo ".conda-env\python.exe"), (Join-Path $repo ".venv\Scripts\python.exe")) |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { throw "python not found (.conda-env or .venv) next to this script." }

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

# Disk headroom gate: brain is ~1GB; never let the backup be what fills the disk.
$freeGB = [math]::Round((Get-PSDrive ($BackupDir.Substring(0,1))).Free / 1GB, 1)
$needGB = [math]::Round(((Get-Item $BrainPath).Length / 1GB) + 2, 1)
if ($freeGB -lt $needGB) { throw "Only ${freeGB}GB free (need ~${needGB}GB) -- skipping backup." }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $BackupDir "applypilot-brain-$stamp.db"

# quick_check gate + online backup in one python call (safe alongside a live WAL writer).
$code = @'
import sqlite3, sys
src_path, dest_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
verdict = src.execute("PRAGMA quick_check").fetchone()[0]
if verdict != "ok":
    print(f"INTEGRITY FAILED: {verdict}", file=sys.stderr)
    sys.exit(2)
dst = sqlite3.connect(dest_path)
src.backup(dst)
dst.close(); src.close()
print("ok")
'@
$out = & $py -c $code $BrainPath $dest 2>&1
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $dest) { Remove-Item $dest -Force }   # never keep a partial/failed copy
    throw "brain backup failed (exit $LASTEXITCODE): $out -- live DB integrity may be BAD; investigate before it propagates."
}
$size = (Get-Item $dest).Length
if ($size -lt 50MB) {
    Remove-Item $dest -Force
    throw "backup implausibly small ($([math]::Round($size/1MB,1))MB) -- not keeping it."
}

# Rotate local: keep the newest $KeepBackups.
Get-ChildItem $BackupDir -Filter "applypilot-brain-*.db" |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepBackups |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Mirror the newest to OneDrive (finished file = static = sync-safe) for off-machine survival.
try {
    New-Item -ItemType Directory -Force -Path $MirrorDir | Out-Null
    Copy-Item $dest (Join-Path $MirrorDir (Split-Path -Leaf $dest)) -Force
    Get-ChildItem $MirrorDir -Filter "applypilot-brain-*.db" |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepMirrors |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $mirrored = "mirrored to $MirrorDir (keep $KeepMirrors)"
} catch { $mirrored = "MIRROR FAILED: $_" }

Write-Host ("[brain-backup] OK -> {0} ({1:N1} MB); keeping newest {2} local; {3}" `
    -f $dest, ($size/1MB), $KeepBackups, $mirrored) -ForegroundColor Green
