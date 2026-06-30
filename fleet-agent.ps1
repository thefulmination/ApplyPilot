# fleet-agent.ps1 -Label m2 [-PollSec 20]
#
#   Run this ONCE on a worker box (m2, m4, or even home). It is the actuator that makes the home
#   box's `fleet.ps1` able to control THIS machine: every -PollSec it reads this box's row in the
#   Postgres table `fleet_desired_state` and starts/stops LOCAL apply workers to match. So setting
#   m2=4 from the home box makes this agent bring m2 to 4 workers; setting m2=0 stops them.
#
#   Enroll once per box (ideally as a Task Scheduler "At log on" task so it auto-starts):
#     m2:  $env:FLEET_PG_DSN="host=192.168.1.187 port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
#          cd C:\ApplyPilot ;  .\fleet-agent.ps1 -Label m2
#     m4:  $env:FLEET_PG_DSN="host=100.90.104.99 port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
#          cd C:\ApplyPilot ;  .\fleet-agent.ps1 -Label m4
#
#   Fail-safe: on any DB blip the agent leaves local workers untouched (never kills on a transient error).
param([Parameter(Mandatory = $true)][string]$Label, [int]$PollSec = 20)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$py = $null
foreach ($d in @(".\.conda-env\python.exe", ".\.venv\Scripts\python.exe")) { if (Test-Path $d) { $py = (Resolve-Path $d).Path; break } }
if (-not $py) { throw "python not found (.conda-env or .venv) -- run the box setup first." }
if (-not $env:FLEET_PG_DSN) { $env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5" }
$worker = Join-Path $repo "run-fleet-worker.ps1"
if (-not (Test-Path $worker)) { throw "run-fleet-worker.ps1 not found next to fleet-agent.ps1 ($worker)" }

function Get-LocalWorkers {
  $rx = '--worker-id\s+"?' + [regex]::Escape($Label) + '-(\d+)'
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='applypilot-fleet-apply.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $rx })
}
function Slot-Of($proc) { if ($proc.CommandLine -match ('--worker-id\s+"?' + [regex]::Escape($Label) + '-(\d+)')) { [int]$matches[1] } else { -1 } }

$lastGen = $null
Write-Host "[fleet-agent:$Label] online -- reconciling LOCAL workers to fleet_desired_state every ${PollSec}s (Ctrl-C to stop)" -ForegroundColor Cyan
while ($true) {
  $line = (& $py "fleet-agent-query.py" $Label 2>$null | Select-Object -Last 1)
  $f = "$line" -split '\|'
  if ($f.Count -lt 4 -or $f[0] -eq 'KEEP') { Start-Sleep -Seconds $PollSec; continue }  # DB blip -> leave as-is
  $want = [int]$f[0]; $agent = $f[1]; $model = $f[2]; $gen = [int]$f[3]

  $procs = Get-LocalWorkers
  $have = $procs.Count

  # generation bump -> the home box asked for a clean restart: kill all local, then re-spawn to $want
  if ($null -ne $lastGen -and $gen -ne $lastGen) {
    Write-Host "[fleet-agent:$Label] generation $lastGen->$gen : restarting all local workers" -ForegroundColor Yellow
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2; $procs = @(); $have = 0
  }
  $lastGen = $gen

  if ($have -lt $want) {
    $running = @($procs | ForEach-Object { Slot-Of $_ })
    $started = 0; $slot = 0
    while ($started -lt ($want - $have) -and $slot -le 20) {
      if ($running -notcontains $slot) {
        $argList = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", $worker, "-Slot", $slot, "-Agent", $agent, "-Label", $Label)
        if ($model) { $argList += @("-Model", $model) }
        Start-Process powershell.exe -ArgumentList $argList -WorkingDirectory $repo
        Write-Host "[fleet-agent:$Label] +start $Label-$slot ($agent$(if($model){"/$model"}))" -ForegroundColor Green
        $running += $slot; $started++; Start-Sleep -Milliseconds 800
      }
      $slot++
    }
  }
  elseif ($have -gt $want) {
    $procs | Sort-Object { Slot-Of $_ } -Descending | Select-Object -First ($have - $want) | ForEach-Object {
      Write-Host "[fleet-agent:$Label] -stop $Label-$(Slot-Of $_) (scale-down / offload)" -ForegroundColor Yellow
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
  Start-Sleep -Seconds $PollSec
}
