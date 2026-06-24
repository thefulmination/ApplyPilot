# ApplyPilot keep-alive guard. Runs the PYTHON supervisor, which does the count / budget
# / apply-restart entirely in python (invoked via `-m`, the form that works in the Task
# Scheduler context -- a python script-file argument silently produces no output there).
# The Windows task (ApplyPilotKeepAlive, every 3 min, IgnoreNew) restarts THIS guard if
# the supervisor itself dies (OOM / whole-tree kill / session end). The supervisor writes
# a done-marker when the applied target is reached; this guard then deletes the task.
$ErrorActionPreference = "SilentlyContinue"

$ProjectRoot = "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
$py = Join-Path $ProjectRoot ".conda-env\python.exe"
$env:APPLYPILOT_DIR = Join-Path $ProjectRoot ".applypilot"
$env:APPLYPILOT_DB_PATH = "C:\Users\JStal\AppData\Local\ApplyPilot\applypilot.db"
$env:CLAUDE_PATH = "C:\Users\JStal\AppData\Roaming\npm\claude.cmd"
$env:PYTHONUTF8 = "1"

$logDir = Join-Path $ProjectRoot ".applypilot\logs"
$keepLog = Join-Path $logDir "keepalive.log"
$superOut = Join-Path $logDir "supervisor_stdout.out"
$doneMarker = "C:\Users\JStal\AppData\Local\ApplyPilot\keepalive.done"
$TargetApplied = 132

function Log($m) { Add-Content -Path $keepLog -Value ("{0}  {1}" -f ((Get-Date).ToUniversalTime().ToString("o")), $m) }

# Supervisor reached the target -> remove the keep-alive task and stop.
if (Test-Path $doneMarker) {
    Log "DONE marker present -- removing keep-alive task"
    schtasks /delete /tn "ApplyPilotKeepAlive" /f | Out-Null
    exit 0
}

# Supervisor already running? (IgnoreNew should prevent a second instance; double-check
# so two supervisors -- and thus two apply runs -- never race.)
$running = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match 'supervise-apply' } |
            Measure-Object).Count
if ($running -gt 0) { Log "supervisor already running ($running) -- skip"; exit 0 }

Log "LAUNCH supervisor (target_applied=$TargetApplied)"
# Synchronous: when the supervisor dies, this guard exits and the task restarts it in
# <= 3 min. The supervisor restarts the apply on each crash and stops at the target.
& $py -m applypilot.cli supervise-apply --target-applied $TargetApplied --max-cost-usd 90 `
    --linkedin-daily-cap 20 --max-job-age-days 45 --stall-minutes 20 `
    --max-attempts 100 --max-hours 48 *>> $superOut
Log "supervisor exited code=$LASTEXITCODE"
