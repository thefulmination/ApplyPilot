# Invoke-FleetReconcile.ps1 - read-only drift check or explicit repair over Tailscale SSH.
#
# Default mode is CHECK-ONLY: inspect code state and health without changing remote boxes.
# Pass -Apply to run the idempotent repair path: git fetch, optional branch checkout,
# git merge --ff-only, pip install -e ., task registration, and local health probe.
[CmdletBinding()]
param(
  [switch]$Apply,
  [switch]$RunHealth,
  [string]$Branch = "",
  [string]$KeyPath = "",
  [int]$SshTimeoutSeconds = 12,
  [int]$RemoteCommandTimeoutSeconds = 75,
  [string[]]$Only = @()
)

$ErrorActionPreference = "Continue"
$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $repo

if (-not $KeyPath) {
  $KeyPath = Join-Path $env:USERPROFILE ".ssh\codex_fleet_ed25519"
}

$FleetTargets = @(
  @{ Name = "Tarpon"; Machine = "m2"; Kind = "windows"; Target = "rstal@tarpon"; RepoPath = "C:\ApplyPilot" },
  @{ Name = "GGGTower"; Machine = "m4"; Kind = "windows"; Target = "backoffice@gggtower"; RepoPath = "C:\ApplyPilot" },
  @{ Name = "Paloma"; Machine = "mac"; Kind = "mac"; Target = "palomaperez@palomas-macbook-air"; RepoPath = '$HOME/ApplyPilot' }
)

function Write-Section([string]$Title) {
  Write-Host ""
  Write-Host "================================================================================" -ForegroundColor DarkCyan
  Write-Host $Title -ForegroundColor Cyan
  Write-Host "================================================================================" -ForegroundColor DarkCyan
}

function Assert-SshReady {
  if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is not available on this machine."
  }
  if (-not (Test-Path $KeyPath)) {
    throw "SSH key not found at $KeyPath"
  }
}

function Encode-RemotePowerShell([string]$Command) {
  [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
}

function Quote-ProcessArgument([string]$Value) {
  if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
  if ($Value -notmatch '[\s"]') { return $Value }
  $quoted = '"'
  $slashes = 0
  foreach ($ch in $Value.ToCharArray()) {
    if ($ch -eq '\') {
      $slashes += 1
    } elseif ($ch -eq '"') {
      $quoted += ('\' * (($slashes * 2) + 1)) + '"'
      $slashes = 0
    } else {
      if ($slashes -gt 0) {
        $quoted += ('\' * $slashes)
        $slashes = 0
      }
      $quoted += $ch
    }
  }
  if ($slashes -gt 0) { $quoted += ('\' * ($slashes * 2)) }
  return $quoted + '"'
}

function New-ProcessStartInfo([string[]]$SshArgs) {
  $psi = [Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = "ssh"
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  if ($psi.PSObject.Properties.Match("ArgumentList").Count -gt 0) {
    foreach ($arg in $SshArgs) { [void]$psi.ArgumentList.Add($arg) }
  } else {
    $psi.Arguments = (($SshArgs | ForEach-Object { Quote-ProcessArgument $_ }) -join " ")
  }
  return $psi
}

function Invoke-RemoteCommand([hashtable]$Target, [string]$Command) {
  $sshArgs = @(
    "-i", $KeyPath,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=$SshTimeoutSeconds",
    "-o", "ConnectionAttempts=1",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    "-o", "StrictHostKeyChecking=accept-new",
    $Target.Target,
    $Command
  )
  Write-Host ""
  Write-Host "[$($Target.Name)] $($Target.Target)" -ForegroundColor Yellow
  $proc = [Diagnostics.Process]::new()
  $proc.StartInfo = New-ProcessStartInfo $sshArgs
  [void]$proc.Start()
  $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
  $stderrTask = $proc.StandardError.ReadToEndAsync()
  if (-not $proc.WaitForExit($RemoteCommandTimeoutSeconds * 1000)) {
    Write-Host "[$($Target.Name)] remote command timed out after ${RemoteCommandTimeoutSeconds}s; killing ssh." -ForegroundColor Red
    try { $proc.Kill($true) } catch { $proc.Kill() }
    $proc.WaitForExit()
  }
  $stdout = $stdoutTask.Result
  $stderr = $stderrTask.Result
  if ($stdout) { Write-Host $stdout.TrimEnd() }
  if ($stderr) { Write-Host $stderr.TrimEnd() -ForegroundColor DarkYellow }
  if ($proc.ExitCode -ne 0) {
    Write-Host "[$($Target.Name)] remote command exited $($proc.ExitCode)" -ForegroundColor Red
  }
}

function New-WindowsBody([hashtable]$Target) {
  $mode = if ($Apply) { "APPLY" } else { "CHECK-ONLY" }
  $applyLiteral = if ($Apply) { '$true' } else { '$false' }
  $runHealthLiteral = if ($RunHealth) { '$true' } else { '$false' }
  $branchLiteral = $Branch.Replace("'", "''")
  $repoLiteral = $Target.RepoPath.Replace("'", "''")
  $machineLiteral = $Target.Machine.Replace("'", "''")
@"
`$ErrorActionPreference = "Continue"
`$ProgressPreference = "SilentlyContinue"
Write-Output "mode: $mode"
Set-Location '$repoLiteral'
Write-Output "repo: `$PWD"
Write-Output "git status --short --branch"
git status --short --branch
Write-Output "git rev-parse --short HEAD"
git rev-parse --short HEAD
if ($applyLiteral) {
  `$branch = '$branchLiteral'
  if (`$branch) {
    `$remoteRef = `$null
    foreach (`$remoteName in @("myfork", "origin", "homebundle")) {
      git remote get-url `$remoteName *> `$null
      if (`$LASTEXITCODE -ne 0) { continue }
      Write-Output "APPLY: git fetch `$remoteName `$branch"
      `$refspec = "+refs/heads/`$(`$branch):refs/remotes/`$(`$remoteName)/`$(`$branch)"
      git fetch `$remoteName `$refspec --prune
      if (`$LASTEXITCODE -ne 0) { continue }
      `$candidate = "`$remoteName/`$branch"
      git show-ref --verify --quiet "refs/remotes/`$candidate"
      if (`$LASTEXITCODE -eq 0) { `$remoteRef = `$candidate; break }
    }
    if (`$remoteRef) {
      git show-ref --verify --quiet "refs/heads/`$branch"
      if (`$LASTEXITCODE -eq 0) {
        Write-Output "APPLY: git checkout `$branch"
        git checkout `$branch
        Write-Output "APPLY: git merge --ff-only `$remoteRef"
        git merge --ff-only `$remoteRef
      } else {
        Write-Output "APPLY: git checkout -b `$branch `$remoteRef"
        git checkout -b `$branch `$remoteRef
      }
    } else {
      Write-Output "APPLY: git checkout `$branch"
      git checkout `$branch
    }
  } else {
    Write-Output "APPLY: git fetch"
    git fetch
    Write-Output "APPLY: git merge --ff-only"
    git merge --ff-only
  }
  Write-Output "APPLY: stop ApplyPilotFleet tasks/processes before reinstall"
  Get-ScheduledTask -TaskName 'ApplyPilotFleet-*' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Get-CimInstance Win32_Process | Where-Object {
    `$cmd = `$_.CommandLine
    `$name = `$_.Name
    `$_.ProcessId -ne `$PID -and (
      `$name -like 'applypilot-*' -or (
        `$name -in @('python.exe', 'powershell.exe', 'pwsh.exe', 'cmd.exe') -and
        (`$cmd -like '*applypilot-fleet-*' -or
         `$cmd -like '*fleet-agent.ps1*' -or
         `$cmd -like '*run-fleet-worker.ps1*' -or
         `$cmd -like '*run-fleet-workers.ps1*' -or
         `$cmd -like '*run-fleet-compute.ps1*' -or
         `$cmd -like '*.fleet-logs*')
      )
    )
  } | ForEach-Object {
    Write-Output "APPLY: stop process `$(`$_.ProcessId) `$(`$_.Name)"
    Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2
  `$py = `$null
  foreach (`$candidate in @('.\.conda-env\python.exe', '.\.venv\Scripts\python.exe')) {
    if (Test-Path `$candidate) { `$py = (Resolve-Path `$candidate).Path; break }
  }
  if (`$py) {
    Write-Output "APPLY: pip install -e ."
    & `$py -m pip install -e .
  } else {
    Write-Output "ERROR: no repo Python found for pip install -e ."
  }
  if (Test-Path .\register-fleet-tasks.ps1) {
    Write-Output "APPLY: register-fleet-tasks.ps1"
    .\register-fleet-tasks.ps1 -Machine '$machineLiteral' -AllowZero
  }
} else {
  Write-Output "CHECK-ONLY: pass -Apply to change remote state"
}
if ($runHealthLiteral -and (Test-Path .\fleet-health.ps1)) {
  Write-Output "fleet-health.ps1 -SkipRemote"
  .\fleet-health.ps1 -SkipRemote
} elseif (-not $runHealthLiteral) {
  Write-Output "CHECK-ONLY: skipping fleet-health.ps1; pass -RunHealth to include it"
}
"@
}

function New-MacBody([hashtable]$Target) {
  $mode = if ($Apply) { "APPLY" } else { "CHECK-ONLY" }
  $applyLiteral = if ($Apply) { "1" } else { "0" }
  $runHealthLiteral = if ($RunHealth) { "1" } else { "0" }
  $branchLiteral = $Branch.Replace("'", "'\''")
@"
set -u
echo 'mode: $mode'
cd "$($Target.RepoPath)" 2>/dev/null || cd "`$HOME/ApplyPilot"
echo 'repo:' "`$PWD"
echo 'git status --short --branch'
git status --short --branch
echo 'git rev-parse --short HEAD'
git rev-parse --short HEAD
if [ "$applyLiteral" = "1" ]; then
  branch='$branchLiteral'
  if [ -n "$branch" ]; then
    remote_ref=''
    for remote_name in myfork origin homebundle; do
      if ! git remote get-url "$remote_name" >/dev/null 2>&1; then continue; fi
      echo "APPLY: git fetch $remote_name $branch"
      git fetch "$remote_name" "+refs/heads/$branch:refs/remotes/$remote_name/$branch" --prune || continue
      candidate="$remote_name/$branch"
      if git show-ref --verify --quiet "refs/remotes/$candidate"; then remote_ref="$candidate"; break; fi
    done
    if [ -n "$remote_ref" ]; then
      if git show-ref --verify --quiet "refs/heads/$branch"; then
        echo "APPLY: git checkout $branch"
        git checkout "$branch"
        echo "APPLY: git merge --ff-only $remote_ref"
        git merge --ff-only "$remote_ref"
      else
        echo "APPLY: git checkout -b $branch $remote_ref"
        git checkout -b "$branch" "$remote_ref"
      fi
    else
      echo "APPLY: git checkout $branch"
      git checkout "$branch"
    fi
  else
    echo 'APPLY: git fetch'
    git fetch
    echo 'APPLY: git merge --ff-only'
    git merge --ff-only
  fi
  echo 'APPLY: pip install -e .'
  if [ -x ./.venv/bin/python ]; then ./.venv/bin/python -m pip install -e .; else python3 -m pip install -e .; fi
  if [ -f ./setup-mac-worker.sh ]; then echo 'APPLY: setup-mac-worker.sh present; run manually if launchd needs re-registration'; fi
else
  echo 'CHECK-ONLY: pass -Apply to change remote state'
fi
if [ "$runHealthLiteral" = "0" ]; then
  echo 'CHECK-ONLY: skipping fleet-health; pass -RunHealth to include it'
fi
"@
}

Write-Section "ApplyPilot Fleet Reconcile"
Write-Host "Mode: $(if ($Apply) { 'APPLY' } else { 'CHECK-ONLY' })"
Write-Host "Key : $KeyPath"
if ($Branch) { Write-Host "Branch: $Branch" }
Assert-SshReady

$targets = $FleetTargets
if ($Only.Count -gt 0) {
  $want = @($Only | ForEach-Object { $_.ToLowerInvariant() })
  $targets = @($FleetTargets | Where-Object {
    $want -contains $_.Name.ToLowerInvariant() -or
    $want -contains $_.Machine.ToLowerInvariant() -or
    $want -contains $_.Target.ToLowerInvariant()
  })
}

foreach ($target in $targets) {
  if ($target.Kind -eq "windows") {
    $encoded = Encode-RemotePowerShell (New-WindowsBody $target)
    Invoke-RemoteCommand $target "powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
  } else {
    $body = New-MacBody $target
    $encodedBody = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    Invoke-RemoteCommand $target "bash -lc `"echo $encodedBody | base64 --decode | bash`""
  }
}

Write-Section "End"
Write-Host "Fleet reconcile finished."
