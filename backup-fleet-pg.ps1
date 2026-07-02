# backup-fleet-pg.ps1 [-KeepBackups 14] [-RegisterTask]
#
#   Nightly backup for the fleet Postgres (applypilot_fleet) -- the DB that EXCLUSIVELY holds
#   applied_set (the never-double-apply ledger), un-synced apply results, and all challenge state.
#   The SQLite brain has integrity-gated backups; until now the fleet PG had NONE.
#
#   Backups go to LOCAL disk (%LOCALAPPDATA%\ApplyPilot\pg-backups), NOT OneDrive -- same reasoning
#   as the brain: OneDrive sync locks on large changing files caused corruption.
#
#   One-time setup (registers a daily 03:30 user-level scheduled task, no admin needed):
#     .\backup-fleet-pg.ps1 -RegisterTask
#   Manual run / verify:
#     .\backup-fleet-pg.ps1
param(
    [int]$KeepBackups = 14,
    [switch]$RegisterTask,
    [string]$BackupDir = (Join-Path $env:LOCALAPPDATA "ApplyPilot\pg-backups"),
    [string]$PgHost = "localhost", [int]$PgPort = 5432,
    [string]$PgDb = "applypilot_fleet", [string]$PgUser = "postgres"
)
$ErrorActionPreference = "Stop"
if (-not $env:PGPASSFILE) { $env:PGPASSFILE = Join-Path $env:APPDATA "postgresql\pgpass.conf" }

if ($RegisterTask) {
    $script = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -KeepBackups $KeepBackups"
    $trigger = New-ScheduledTaskTrigger -Daily -At 3:30am
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName "ApplyPilot fleet PG backup" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Nightly pg_dump of applypilot_fleet (applied_set dedup ledger)" -Force | Out-Null
    Write-Host "[pg-backup] scheduled task registered: daily 03:30 -> $BackupDir (StartWhenAvailable covers missed nights)" -ForegroundColor Green
    return
}

# Locate pg_dump: PATH first, then standard install dirs (newest version wins).
$pgDump = (Get-Command pg_dump.exe -ErrorAction SilentlyContinue).Source
if (-not $pgDump) {
    $pgDump = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $pgDump) { throw "pg_dump.exe not found (PATH or C:\Program Files\PostgreSQL\<ver>\bin)." }

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

# Disk headroom gate: never let the backup be the thing that fills the disk.
$freeGB = [math]::Round((Get-PSDrive ($BackupDir.Substring(0,1))).Free / 1GB, 1)
if ($freeGB -lt 2) { throw "Only ${freeGB}GB free on $($BackupDir.Substring(0,1)): -- skipping backup." }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $BackupDir "applypilot_fleet-$stamp.dump"
& $pgDump -h $PgHost -p $PgPort -U $PgUser -d $PgDb -Fc --no-password -f $dest
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $dest) { Remove-Item $dest -Force }   # never keep a partial dump
    throw "pg_dump failed (exit $LASTEXITCODE) -- check PGPASSFILE ($env:PGPASSFILE) and that Postgres is up."
}
$size = (Get-Item $dest).Length
if ($size -lt 10KB) {
    Remove-Item $dest -Force
    throw "pg_dump produced an implausibly small file ($size bytes) -- not keeping it."
}

# Rotate: keep the newest $KeepBackups.
Get-ChildItem $BackupDir -Filter "applypilot_fleet-*.dump" |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepBackups |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host ("[pg-backup] OK -> {0} ({1:N1} MB); keeping newest {2}. Restore: pg_restore -h {3} -U {4} -d {5} --clean <file>" `
    -f $dest, ($size/1MB), $KeepBackups, $PgHost, $PgUser, $PgDb) -ForegroundColor Green
