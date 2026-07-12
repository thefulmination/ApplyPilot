# Register the read-only crash-liveness task without touching FleetAgent or worker desired state.
# This task is safe to install from a normal interactive user PowerShell session.
param(
  [int]$IntervalMinutes = 30,
  [int]$Limit = 25,
  [int]$OlderDays = 7,
  [int]$RefreshDays = 7,
  [switch]$Unregister
)
$ErrorActionPreference = "Stop"
if ($IntervalMinutes -lt 5) { throw "IntervalMinutes must be at least 5." }
if ($Limit -lt 1 -or $Limit -gt 1000) { throw "Limit must be between 1 and 1000." }
if ($OlderDays -lt 0 -or $RefreshDays -lt 0) { throw "OlderDays and RefreshDays cannot be negative." }

$taskName = "ApplyPilotFleet-CrashLiveness"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $repo "run-fleet-crash-liveness.ps1"
$logPath = Join-Path $repo ".fleet-logs\crash-liveness.log"
if (-not (Test-Path $runner)) { throw "run-fleet-crash-liveness.ps1 not found at $runner" }

if ($Unregister) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "unregistered $taskName"
  exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Once -Limit $Limit -OlderDays $OlderDays -RefreshDays $RefreshDays -LogPath `"$logPath`"" `
  -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 9)
$principal = New-ScheduledTaskPrincipal `
  -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
  -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description "ApplyPilot crash liveness refresh: evidence-only; never retries or resolves applications." `
  -Force | Out-Null
Write-Host "registered $taskName every $IntervalMinutes minutes (limit=$Limit)"
