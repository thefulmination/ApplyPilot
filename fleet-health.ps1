# fleet-health.ps1 - one-command read-only fleet health report.
[CmdletBinding()]
param(
  [string]$Dsn = "",
  [string]$KeyPath = "",
  [int]$SshTimeoutSeconds = 12,
  [switch]$SkipRemote
)

$ErrorActionPreference = "Continue"
$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $repo

if (-not $Dsn) {
  if ($env:FLEET_PG_DSN) {
    $Dsn = $env:FLEET_PG_DSN
  } else {
    $Dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
  }
}
if (-not $KeyPath) {
  $KeyPath = Join-Path $env:USERPROFILE ".ssh\codex_fleet_ed25519"
}

$FleetTargets = @(
  @{ Name = "Tarpon";   Target = "rstal@tarpon";                  Kind = "windows" },
  @{ Name = "GGGTower"; Target = "backoffice@gggtower";           Kind = "windows" },
  @{ Name = "Paloma";   Target = "palomaperez@palomas-macbook-air"; Kind = "mac" }
)

function Write-Section([string]$Title) {
  Write-Host ""
  Write-Host "================================================================================" -ForegroundColor DarkCyan
  Write-Host $Title -ForegroundColor Cyan
  Write-Host "================================================================================" -ForegroundColor DarkCyan
}

function Invoke-QuietProbe([string]$Label, [scriptblock]$Body) {
  Write-Host ""
  Write-Host "[$Label]" -ForegroundColor Yellow
  try {
    & $Body
  } catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  }
}

function Get-RepoPython {
  foreach ($candidate in @(
    (Join-Path $repo ".conda-env\python.exe"),
    (Join-Path $repo ".venv\Scripts\python.exe")
  )) {
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}

function Get-ApplyPilotCli {
  foreach ($candidate in @(
    (Join-Path $repo ".conda-env\Scripts\applypilot.exe"),
    (Join-Path $repo ".venv\Scripts\applypilot.exe")
  )) {
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}

function Show-CapSolverReadiness {
  $cli = Get-ApplyPilotCli
  if (-not $cli) {
    Write-Host "ERROR: applypilot.exe not found at .conda-env\Scripts or .venv\Scripts" -ForegroundColor Red
    return
  }

  Write-Host "CapSolver readiness"
  & $cli fleet-capsolver-check --json
  if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: CapSolver readiness failed with exit code $LASTEXITCODE" -ForegroundColor Red
  }
}

function Show-LocalScheduledTasks {
  $tasks = Get-ScheduledTask -TaskName "ApplyPilotFleet-*" -ErrorAction SilentlyContinue
  if (-not $tasks) {
    Write-Host "(no ApplyPilotFleet-* scheduled tasks found)"
    return
  }
  $rows = foreach ($task in $tasks) {
    $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -ErrorAction SilentlyContinue
    [pscustomobject]@{
      TaskName       = $task.TaskName
      State          = $task.State
      LastRunTime    = if ($info) { $info.LastRunTime } else { $null }
      LastTaskResult = if ($info) { $info.LastTaskResult } else { $null }
    }
  }
  $rows | Sort-Object TaskName | Format-Table -AutoSize
}

function Show-ApplyPilotProcesses {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*ApplyPilot*" } |
    Select-Object ProcessId, Name, CommandLine |
    Format-Table -Wrap
}

function Show-DatabaseHealth {
  $py = Get-RepoPython
  if (-not $py) {
    Write-Host "ERROR: no repo Python found at .conda-env\python.exe or .venv\Scripts\python.exe" -ForegroundColor Red
    return
  }

  $code = @'
import os
import sys

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception as exc:
    print(f"ERROR: cannot import psycopg: {type(exc).__name__}: {exc}")
    sys.exit(0)

dsn = os.environ.get("FLEET_PG_DSN")

def show_rows(title, cur, sql):
    print(f"\n-- {title} --")
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    except Exception as exc:
        cur.connection.rollback()
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return
    cur.connection.rollback()
    if not rows:
        print("(none)")
        return
    cols = list(rows[0].keys())
    print("\t".join(cols))
    for row in rows:
        print("\t".join("" if row[col] is None else str(row[col]) for col in cols))

try:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            show_rows(
                "fleet_desired_state",
                cur,
                "SELECT machine_owner, desired_workers, agent, model, generation, updated_at "
                "FROM fleet_desired_state ORDER BY machine_owner",
            )
            show_rows(
                "worker_heartbeat",
                cur,
                "SELECT worker_id, role, state, current_job, "
                "round(EXTRACT(EPOCH FROM (now() - last_beat)))::int AS age_s, last_beat "
                "FROM worker_heartbeat ORDER BY worker_id",
            )
            show_rows(
                "open remote_commands",
                cur,
                "SELECT worker_id, command, count(*) AS open, min(issued_at) AS oldest "
                "FROM remote_commands WHERE acked_at IS NULL "
                "GROUP BY worker_id, command ORDER BY worker_id, command",
            )
            show_rows(
                "search_tasks",
                cur,
                "SELECT status, count(*) AS n, "
                "count(*) FILTER (WHERE enabled AND status='queued' AND next_due_at <= now()) AS due_now, "
                "max(last_run_at) AS last_run_at "
                "FROM search_tasks GROUP BY status ORDER BY status",
            )
            show_rows(
                "discovered_postings",
                cur,
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE synced_to_home_at IS NULL) AS unsynced, "
                "count(*) FILTER (WHERE discovered_at >= now() - interval '24 hours') AS recent_24h, "
                "max(discovered_at) AS newest "
                "FROM discovered_postings",
            )
            show_rows(
                "apply_queue",
                cur,
                "SELECT status, count(*) AS n FROM apply_queue GROUP BY status ORDER BY status",
            )
            show_rows(
                "compute_queue",
                cur,
                "SELECT status, count(*) AS n FROM compute_queue GROUP BY status ORDER BY status",
            )
except Exception as exc:
    print(f"ERROR: database probe failed: {type(exc).__name__}: {exc}")
'@

  $oldDsn = $env:FLEET_PG_DSN
  $env:FLEET_PG_DSN = $Dsn
  $code | & $py -
  if ($null -ne $oldDsn) {
    $env:FLEET_PG_DSN = $oldDsn
  } else {
    Remove-Item Env:FLEET_PG_DSN -ErrorAction SilentlyContinue
  }
}

function Invoke-SshProbe([string]$Name, [string]$Target, [string]$Command) {
  Write-Host ""
  Write-Host "[${Name}: $Target]" -ForegroundColor Yellow
  if ($SkipRemote) {
    Write-Host "SKIP: -SkipRemote was passed."
    return
  }
  if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "SKIP: ssh is not available on this machine." -ForegroundColor Yellow
    return
  }
  if (-not (Test-Path $KeyPath)) {
    Write-Host "SKIP: SSH key not found at $KeyPath" -ForegroundColor Yellow
    return
  }

  $sshArgs = @(
    "-i", $KeyPath,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=$SshTimeoutSeconds",
    "-o", "StrictHostKeyChecking=accept-new",
    $Target,
    $Command
  )
  $output = & ssh @sshArgs 2>&1
  $code = $LASTEXITCODE
  if ($output) { $output | ForEach-Object { Write-Host $_ } }
  if ($code -ne 0) {
    Write-Host "SSH probe exited $code" -ForegroundColor Red
  }
}

Write-Section "ApplyPilot Fleet Health"
Write-Host "Repo: $repo"
Write-Host "DSN : $Dsn"
Write-Host "Key : $KeyPath"
Write-Host "Time: $(Get-Date -Format o)"

Write-Section "Local Home"
Invoke-QuietProbe "tailscale status" {
  if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    tailscale status
  } else {
    Write-Host "(tailscale CLI not found)"
  }
}
Invoke-QuietProbe "ApplyPilotFleet scheduled tasks" { Show-LocalScheduledTasks }
Invoke-QuietProbe "ApplyPilot processes" { Show-ApplyPilotProcesses }
Invoke-QuietProbe "CapSolver readiness" { Show-CapSolverReadiness }
Invoke-QuietProbe "Postgres fleet tables" { Show-DatabaseHealth }

Write-Section "Remote Fleet"
$windowsTaskProbe = @"
hostname
Set-Location C:\ApplyPilot
Write-Output 'CapSolver readiness'
`$cli = `$null
foreach (`$candidate in @('.\.conda-env\Scripts\applypilot.exe', '.\.venv\Scripts\applypilot.exe')) { if (Test-Path `$candidate) { `$cli = (Resolve-Path `$candidate).Path; break } }
if (`$cli) { & `$cli fleet-capsolver-check --json } else { Write-Output 'ERROR: applypilot.exe not found' }
Get-ScheduledTask -TaskName 'ApplyPilotFleet-*' -ErrorAction SilentlyContinue | ForEach-Object {
  `$i = Get-ScheduledTaskInfo -TaskName `$_.TaskName -ErrorAction SilentlyContinue
  [pscustomobject]@{ TaskName=`$_.TaskName; State=`$_.State; LastTaskResult=if (`$i) { `$i.LastTaskResult } else { `$null } }
} | Sort-Object TaskName | Format-Table -AutoSize
if (Test-Path .\.venv\Scripts\python.exe) { .\.venv\Scripts\python.exe -m pip check }
Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like '*C:\ApplyPilot*' } | Select-Object ProcessId,Name,CommandLine | Format-Table -Wrap
"@

$gggProbe = @"
hostname
Set-Location C:\ApplyPilot
git branch --show-current
git log --oneline -1
Write-Output 'CapSolver readiness'
`$cli = `$null
foreach (`$candidate in @('.\.conda-env\Scripts\applypilot.exe', '.\.venv\Scripts\applypilot.exe')) { if (Test-Path `$candidate) { `$cli = (Resolve-Path `$candidate).Path; break } }
if (`$cli) { & `$cli fleet-capsolver-check --json } else { Write-Output 'ERROR: applypilot.exe not found' }
Get-ScheduledTask -TaskName 'ApplyPilotFleet-*' -ErrorAction SilentlyContinue | ForEach-Object {
  `$i = Get-ScheduledTaskInfo -TaskName `$_.TaskName -ErrorAction SilentlyContinue
  [pscustomobject]@{ TaskName=`$_.TaskName; State=`$_.State; LastTaskResult=if (`$i) { `$i.LastTaskResult } else { `$null } }
} | Sort-Object TaskName | Format-Table -AutoSize
if (Test-Path .\.venv\Scripts\python.exe) { .\.venv\Scripts\python.exe -m pip check }
Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like '*C:\ApplyPilot*' } | Select-Object ProcessId,Name,CommandLine | Format-Table -Wrap
"@

$macProbe = "hostname; pwd; ps aux | grep -E 'applypilot|run-worker-mac' | grep -v grep || true"

Invoke-SshProbe "Tarpon" "rstal@tarpon" $windowsTaskProbe
Invoke-SshProbe "GGGTower" "backoffice@gggtower" $gggProbe
Invoke-SshProbe "Paloma" "palomaperez@palomas-macbook-air" $macProbe

Write-Section "End"
Write-Host "Read-only health report complete."
