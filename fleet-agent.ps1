# fleet-agent.ps1 -Label m2 [-PollSec 20] [-AutoUpdate] [-UpdateEverySec 900]
#
#   Run this ONCE on a worker box (m2, m4, or even home). It is the actuator that makes the home
#   box's `fleet.ps1` able to control THIS machine: every -PollSec it reads this box's row in the
#   Postgres table `fleet_desired_state` and starts/stops LOCAL apply workers to match. So setting
#   m2=4 from the home box makes this agent bring m2 to 4 workers; setting m2=0 stops them.
#
#   Enroll once per box (ideally as a Task Scheduler "At log on" task so it auto-starts):
#     m2:  $env:FLEET_PG_DSN="host=192.168.1.187 port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
#          cd C:\ApplyPilot ;  .\fleet-agent.ps1 -Label m2 -AutoUpdate
#     m4:  $env:FLEET_PG_DSN="host=100.90.104.99 port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
#          cd C:\ApplyPilot ;  .\fleet-agent.ps1 -Label m4 -AutoUpdate
#
#   -AutoUpdate (worker boxes ONLY, never home -- home is the dev origin and pushes, never pulls):
#   every -UpdateEverySec git-fetch the current branch's upstream; if behind and fast-forwardable
#   with a clean tree, wait for a BETWEEN-JOBS window (fleet-agent-update-gate.py: no fresh
#   non-idle heartbeat + no live lease for this Label; fail-closed), then stop local workers,
#   ff-only pull, pip reinstall only if pyproject.toml changed, and let the next reconcile respawn
#   workers on the new code. If the agent's own files changed it exits 1 so the FleetAgent
#   scheduled task (RestartCount budget) relaunches it fresh -- manual foreground runs must
#   relaunch by hand. Spec: docs/superpowers/specs/2026-07-03-fleet-pull-updater-design.md
#
#   Fail-safe: on any DB blip the agent leaves local workers untouched (never kills on a transient error).
param([Parameter(Mandatory = $true)][string]$Label, [int]$PollSec = 20,
      [switch]$AutoUpdate, [int]$UpdateEverySec = 900)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# ---- host-identity guard: never run another machine's workers on this box ----
# A box declares its OWN fleet label in APPLYPILOT_FLEET_LABEL (home/m2/m4). Running this
# agent with a foreign -Label (e.g. `-Label m2` on the HOME box) would reconcile+spawn that
# machine's workers HERE -- the live 2026-07-04 incident where home's desired=0 yet 4 m2
# (TARPON) workers ran on the home box. If this box is labeled and disagrees, refuse to start.
# Unset label = unknown identity = permissive (back-compat for not-yet-labeled boxes).
$boxLabel = "$env:APPLYPILOT_FLEET_LABEL".Trim()
if ($boxLabel -and ($boxLabel -ne $Label)) {
  throw "[fleet-agent:$Label] host-identity guard: this box is '$boxLabel' but the agent was started with -Label '$Label'. Refusing to spawn another machine's workers here (e.g. m2/TARPON workers must never run on the home box). Re-run with -Label $boxLabel, or start this agent on the '$Label' box."
}

$py = $null
foreach ($d in @(".\.conda-env\python.exe", ".\.venv\Scripts\python.exe")) { if (Test-Path $d) { $py = (Resolve-Path $d).Path; break } }
if (-not $py) { throw "python not found (.conda-env or .venv) -- run the box setup first." }
if (-not $env:FLEET_PG_DSN) { $env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5" }
$worker = Join-Path $repo "run-fleet-worker.ps1"
if (-not (Test-Path $worker)) { throw "run-fleet-worker.ps1 not found next to fleet-agent.ps1 ($worker)" }

# ---- startup PRE-FLIGHT: is THIS box ready to run workers + reach the home box? ----
Write-Host "[fleet-agent:$Label] pre-flight (DSN=$env:FLEET_PG_DSN)..." -ForegroundColor Cyan
$pf = @()
& $py -c "import applypilot" 2>$null; if (-not $?) { $pf += "applypilot not importable -> $py -m pip install -e ." }
if (-not (@(".\.conda-env\Scripts\applypilot-fleet-apply.exe", ".\.venv\Scripts\applypilot-fleet-apply.exe") | Where-Object { Test-Path $_ })) { $pf += "applypilot-fleet-apply.exe MISSING -> pip install -e ." }
if (-not (Get-ChildItem ".\.playwright-browsers\chromium-*" -Directory -ErrorAction SilentlyContinue)) { $pf += "Chromium MISSING in .playwright-browsers (set PLAYWRIGHT_BROWSERS_PATH + reinstall)" }
$probe = (& $py "fleet-agent-query.py" $Label 2>$null | Select-Object -Last 1)
if ("$probe" -notmatch '^\d+\|') {
  $pf += "CANNOT reach home Postgres over FLEET_PG_DSN (got '$probe') -- check the DSN host/LAN/firewall/pg_hba"
} else {
  $wantAgent = ($probe -split '\|')[1]
  $cli = if ($wantAgent -eq 'codex') { Join-Path $env:APPDATA "npm\codex.cmd" } else { Join-Path $env:APPDATA "npm\claude.cmd" }
  if (-not (Test-Path $cli)) { $pf += "$wantAgent CLI missing ($cli) -- workers will flash-and-die until it's installed + logged in" }
  Write-Host "[fleet-agent:$Label] home box wants this machine = '$probe' (workers|agent|model|gen)" -ForegroundColor Gray
}
if ($pf.Count) {
  Write-Host "[fleet-agent:$Label] PRE-FLIGHT PROBLEMS on this box:" -ForegroundColor Red
  $pf | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
  Write-Host "[fleet-agent:$Label] fix those and re-run. (Reconciling anyway in 8s; Ctrl-C to abort.)" -ForegroundColor Yellow
  Start-Sleep -Seconds 8
} else {
  Write-Host "[fleet-agent:$Label] pre-flight PASS -- ready to obey the home box." -ForegroundColor Green
}

function Get-LocalWorkers {
  $rx = '--worker-id\s+"?' + [regex]::Escape($Label) + '-(\d+)'
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='applypilot-fleet-apply.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $rx })
}
function Slot-Of($proc) { if ($proc.CommandLine -match ('--worker-id\s+"?' + [regex]::Escape($Label) + '-(\d+)')) { [int]$matches[1] } else { -1 } }

# ---- auto-update (spec: 2026-07-03-fleet-pull-updater-design.md) ----
$updateLog = Join-Path $repo ".fleet-logs\fleet-agent-update.log"
function Log-Update([string]$msg, [string]$color = "Gray") {
  $line = "[{0}] [fleet-agent:{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Label, $msg
  Write-Host $line -ForegroundColor $color
  try {
    $dir = Split-Path -Parent $updateLog
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $f = Get-Item $updateLog -ErrorAction SilentlyContinue
    if ($f -and $f.Length -gt 50MB) { Move-Item $updateLog "$updateLog.1" -Force }  # single-slot rotation
    Add-Content -Path $updateLog -Value $line
  } catch { Write-Host "[fleet-agent:$Label] WARNING: update-log write failed: $_" -ForegroundColor Yellow }
}
function Get-ShortSha([string]$sha) { if ($sha -and $sha.Length -ge 9) { $sha.Substring(0, 9) } else { "$sha" } }

# Files whose change requires the agent itself to restart (exit 1 -> scheduled-task relaunch).
$selfFiles = @("fleet-agent.ps1", "fleet-agent-query.py", "fleet-agent-update-gate.py",
               "src/applypilot/fleet/update_gate.py")
$script:lastUpdateCheck = [datetime]::MinValue
$script:updatePending = $false
$script:updBranch = $null
$script:updRemote = $null

function Invoke-AutoUpdate {
  # Returns $true when it stopped workers (update applied) so the caller can respawn immediately.
  $due = ((Get-Date) - $script:lastUpdateCheck).TotalSeconds -ge $UpdateEverySec
  if (-not ($due -or $script:updatePending)) { return $false }

  if ($due) {
    $script:lastUpdateCheck = Get-Date
    $branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
    if (-not $branch -or $branch -eq "HEAD") { Log-Update "skip: not on a branch ('$branch')" "Yellow"; $script:updatePending = $false; return $false }
    $remote = (& git config "branch.$branch.remote" 2>$null); if (-not $remote) { $remote = "origin" }
    & git fetch $remote --quiet 2>$null
    if ($LASTEXITCODE -ne 0) { Log-Update "skip: git fetch $remote failed (offline?)" "Yellow"; $script:updatePending = $false; return $false }
    $script:updBranch = $branch; $script:updRemote = $remote
  }
  $branch = $script:updBranch; $remote = $script:updRemote
  if (-not $branch -or -not $remote) { $script:updatePending = $false; return $false }
  $local = (& git rev-parse HEAD 2>$null)
  $target = (& git rev-parse "$remote/$branch" 2>$null)
  if (-not $target -or $local -eq $target) { $script:updatePending = $false; return $false }

  # guards: worker boxes must never own local commits or edits
  if (@(& git status --porcelain 2>$null).Count -gt 0) {
    Log-Update "UPDATE BLOCKED: working tree dirty -- this box must be a clean clone" "Red"
    $script:updatePending = $false; return $false
  }
  & git merge-base --is-ancestor HEAD "$remote/$branch" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Log-Update "UPDATE BLOCKED: history diverged from $remote/$branch -- fix by hand" "Red"
    $script:updatePending = $false; return $false
  }

  if (-not $script:updatePending) { Log-Update "update available: $(Get-ShortSha $local) -> $(Get-ShortSha $target) (waiting for between-jobs window)" "Cyan" }
  $script:updatePending = $true

  # between-jobs gate (fail-closed: anything but IDLE means wait)
  $gate = (& $py "fleet-agent-update-gate.py" $Label 2>$null | Select-Object -Last 1)
  if ("$gate" -ne "IDLE") { return $false }

  Log-Update "between-jobs window open -- updating" "Cyan"
  Get-LocalWorkers | ForEach-Object {
    Log-Update "-stop $Label-$(Slot-Of $_) (pre-update)" "Yellow"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2

  & git merge --ff-only --quiet "$remote/$branch" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Log-Update "UPDATE FAILED: ff-only merge refused -- leaving tree at $(Get-ShortSha $local); workers respawn on old code" "Red"
    $script:updatePending = $false; return $true
  }
  $script:updatePending = $false
  $changed = @(& git diff --name-only $local HEAD 2>$null)
  Log-Update "updated $(Get-ShortSha $local) -> $(Get-ShortSha (& git rev-parse HEAD)) ($($changed.Count) files)" "Green"

  if ($changed -contains "pyproject.toml") {
    Log-Update "pyproject.toml changed -> pip install -e ." "Cyan"
    & $py -m pip install -e . --quiet
    if ($LASTEXITCODE -ne 0) { Log-Update "PIP INSTALL FAILED -- new code + old deps; fix by hand ASAP" "Red" }
  }
  $selfChanged = @($changed | Where-Object { $selfFiles -contains ($_ -replace '\\', '/') })
  if ($selfChanged.Count -gt 0) {
    Log-Update "agent's own files changed ($($selfChanged -join ', ')) -- exiting for supervisor relaunch (manual runs: restart fleet-agent.ps1 yourself)" "Yellow"
    exit 1
  }
  return $true
}

$lastGen = $null
Write-Host "[fleet-agent:$Label] online -- reconciling LOCAL workers to fleet_desired_state every ${PollSec}s (Ctrl-C to stop)" -ForegroundColor Cyan
while ($true) {
  # auto-update runs BEFORE reconcile so a post-update respawn happens in this same tick
  if ($AutoUpdate) { Invoke-AutoUpdate | Out-Null }

  $line = (& $py "fleet-agent-query.py" $Label 2>$null | Select-Object -Last 1)
  $f = "$line" -split '\|'
  if ($f.Count -lt 4 -or $f[0] -eq 'KEEP') { Start-Sleep -Seconds $PollSec; continue }  # DB blip -> leave as-is
  $want = [int]$f[0]; $agent = $f[1]; $model = $f[2]; $gen = [int]$f[3]

  $procs = Get-LocalWorkers
  # A worker is TWO OS processes (the pip .exe wrapper + its python.exe child), both carrying the
  # same --worker-id. Count DISTINCT SLOTS, never raw processes: counting processes reads 1 worker
  # as 2 -> "over target" -> kill -> next poll sees 0 -> respawn, forever (live 7/03: this loop
  # leaked 51 launcher windows in 40 min on home and killed home-0 mid-apply every ~42s).
  $slotGroups = @($procs | Group-Object { Slot-Of $_ })
  $have = $slotGroups.Count

  # generation bump -> the home box asked for a clean restart: kill all local, then re-spawn to $want
  if ($null -ne $lastGen -and $gen -ne $lastGen) {
    Write-Host "[fleet-agent:$Label] generation $lastGen->$gen : restarting all local workers" -ForegroundColor Yellow
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2; $procs = @(); $have = 0
  }
  $lastGen = $gen

  if ($have -lt $want) {
    $running = @($slotGroups | ForEach-Object { [int]$_.Name })
    $started = 0; $slot = 0
    while ($started -lt ($want - $have) -and $slot -le 20) {
      if ($running -notcontains $slot) {
        # -File must be pre-quoted: Start-Process joins array args with spaces WITHOUT quoting,
        # so a repo path containing spaces (home's OneDrive checkout) otherwise truncates to
        # "-File C:\...\New" and the child exits before the launcher ever runs (m2/m4's
        # C:\ApplyPilot masked this for days).
        # No -NoExit: a launcher window must close when its worker exits, or every worker death
        # leaves an eternal window (transcripts live in .applypilot\logs\worker-<slot>.log).
        $argList = @("-ExecutionPolicy", "Bypass", "-File", "`"$worker`"", "-Slot", $slot, "-Agent", $agent, "-Label", $Label)
        if ($model) { $argList += @("-Model", $model) }
        Start-Process powershell.exe -ArgumentList $argList -WorkingDirectory $repo
        Write-Host "[fleet-agent:$Label] +start $Label-$slot ($agent$(if($model){"/$model"}))" -ForegroundColor Green
        $running += $slot; $started++; Start-Sleep -Milliseconds 800
      }
      $slot++
    }
  }
  elseif ($have -gt $want) {
    # Scale down by SLOT (highest first), killing every process of that slot (exe wrapper + python child).
    $slotGroups | Sort-Object { [int]$_.Name } -Descending | Select-Object -First ($have - $want) | ForEach-Object {
      Write-Host "[fleet-agent:$Label] -stop $Label-$($_.Name) (scale-down / offload)" -ForegroundColor Yellow
      $_.Group | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
  }
  Start-Sleep -Seconds $PollSec
}
