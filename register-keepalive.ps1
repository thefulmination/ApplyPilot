# Registers the ApplyPilotKeepAlive scheduled task: runs keepalive-apply.ps1 every 3
# minutes, single instance, no execution-time limit, restart-on-failure. Run once:
#   powershell -ExecutionPolicy Bypass -File register-keepalive.ps1
# Remove with:  schtasks /delete /tn ApplyPilotKeepAlive /f   (it also self-removes at the target)
$ErrorActionPreference = "Stop"
$root = "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
try {
    $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\keepalive-apply.ps1`""
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration (New-TimeSpan -Days 1)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName "ApplyPilotKeepAlive" -Action $action -Trigger $trigger `
        -Settings $settings -Description "ApplyPilot apply keep-alive (auto-restart)" -Force | Out-Null
    Start-ScheduledTask -TaskName "ApplyPilotKeepAlive"
    Write-Output ("OK STATE=" + (Get-ScheduledTask -TaskName "ApplyPilotKeepAlive").State)
} catch {
    Write-Output ("REGISTER FAILED: " + $_.Exception.Message)
}
