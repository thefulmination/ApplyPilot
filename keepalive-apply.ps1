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
$TargetApplied = 0   # 0 = BUDGET mode: --max-cost-usd is the real stop (target mode ignores it)

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
# Merge all streams (2>&1) and append as UTF-8. PowerShell's `*>>` redirect writes
# UTF-16LE under Windows PowerShell 5.1 (the Task Scheduler host), which made these logs
# awkward to read/grep; Out-File -Encoding utf8 keeps them plain UTF-8. $LASTEXITCODE
# still reflects python's exit code after the pipe (Out-File doesn't change it).
& $py -m applypilot.cli supervise-apply --target-applied $TargetApplied --max-cost-usd 90 `
    --workers 2 --linkedin-daily-cap 20 --max-job-age-days 45 --stall-minutes 20 `
    --max-attempts 100 --max-hours 48 2>&1 |
    Out-File -FilePath $superOut -Append -Encoding utf8
Log "supervisor exited code=$LASTEXITCODE"
