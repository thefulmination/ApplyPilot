# setup-fleet-ssh-access.ps1
#
# Home:
#   .\setup-fleet-ssh-access.ps1 -GenerateKey
#   .\setup-fleet-ssh-access.ps1 -Check
#
# Windows worker, run in an elevated PowerShell:
#   .\setup-fleet-ssh-access.ps1 -InstallPublicKey -PublicKey '<ssh-ed25519 ... codex-fleet-access>' -TargetUser rstal
[CmdletBinding()]
param(
  [switch]$GenerateKey,
  [switch]$InstallPublicKey,
  [switch]$Check,
  [string]$PublicKey = "",
  [string]$TargetUser = "",
  [string]$KeyPath = "",
  [string]$TailnetCidr = "100.64.0.0/10",
  [int]$SshTimeoutSeconds = 8,
  [string[]]$Only = @()
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $KeyPath) {
  $KeyPath = Join-Path $env:USERPROFILE ".ssh\codex_fleet_ed25519"
}

$FleetTargets = @(
  @{ Name = "Tarpon";   User = "rstal";              Target = "rstal@tarpon";                    Kind = "windows" },
  @{ Name = "GGGTower"; User = "backoffice";         Target = "backoffice@gggtower";             Kind = "windows" },
  @{ Name = "Paloma";   User = "palomaperez";        Target = "palomaperez@palomas-macbook-air"; Kind = "mac" }
)

function Write-Section([string]$Title) {
  Write-Host ""
  Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Test-IsAdmin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-PublicKey([string]$Key) {
  if (-not $Key -or $Key -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) ') {
    throw "Public key must start with ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp*. Got: $Key"
  }
}

function Get-PublicKey {
  $pubPath = "$KeyPath.pub"
  if (-not (Test-Path -LiteralPath $pubPath)) {
    throw "Public key file not found: $pubPath"
  }
  $key = (Get-Content -LiteralPath $pubPath -Raw).Trim()
  Assert-PublicKey $key
  return $key
}

function Ensure-FleetKey {
  if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
    throw "ssh-keygen is not available. Install Windows OpenSSH Client first."
  }
  $sshDir = Split-Path -Parent $KeyPath
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null

  if (-not (Test-Path -LiteralPath $KeyPath)) {
    Write-Host "Generating fleet SSH key: $KeyPath"
    & ssh-keygen -t ed25519 -f $KeyPath -N "" -C "codex-fleet-access"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed with exit $LASTEXITCODE" }
  } else {
    Write-Host "Fleet SSH key already exists: $KeyPath"
  }

  if (-not (Test-Path -LiteralPath "$KeyPath.pub")) {
    & ssh-keygen -y -f $KeyPath | Set-Content -LiteralPath "$KeyPath.pub" -Encoding ascii
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen -y failed with exit $LASTEXITCODE" }
  }

  $pub = Get-PublicKey
  Write-Section "ApplyPilot fleet SSH public key"
  Write-Host $pub

  Write-Section "One-time target commands"
  Write-Host "Tarpon/GGGTower, run elevated from C:\ApplyPilot:"
  Write-Host ".\setup-fleet-ssh-access.ps1 -InstallPublicKey -TargetUser rstal -PublicKey '$pub'"
  Write-Host ".\setup-fleet-ssh-access.ps1 -InstallPublicKey -TargetUser backoffice -PublicKey '$pub'"
  Write-Host ""
  Write-Host "Paloma, run from the ApplyPilot checkout:"
  Write-Host "APPLYPILOT_FLEET_SSH_PUBLIC_KEY='$pub' bash ./setup-fleet-ssh-access-mac.sh"
}

function Add-KeyLine([string]$Path, [string]$Key) {
  $dir = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType File -Force -Path $Path | Out-Null
  }
  $existing = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)
  if ($existing -notcontains $Key) {
    Add-Content -LiteralPath $Path -Value $Key -Encoding ascii
    Write-Host "Added key to $Path"
  } else {
    Write-Host "Key already present in $Path"
  }
}

function Set-AuthorizedKeysAcl([string]$Path, [string]$UserName) {
  try {
    & icacls $Path /inheritance:r /grant "${UserName}:F" "SYSTEM:F" "Administrators:F" | Out-Null
  } catch {
    Write-Host "WARN: could not set user authorized_keys ACL on ${Path}: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

function Ensure-OpenSshServer {
  if (-not (Test-IsAdmin)) {
    throw "Run elevated as Administrator to install/start sshd and set the firewall rule."
  }

  $cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($cap -and $cap.State -ne "Installed") {
    Write-Host "Installing OpenSSH.Server"
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
  }

  Set-Service -Name sshd -StartupType Automatic
  Start-Service sshd

  $ruleName = "ApplyPilot SSH over Tailscale"
  $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
  if (-not $rule) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort 22 -RemoteAddress $TailnetCidr -Profile Any | Out-Null
    Write-Host "Firewall rule added: $ruleName RemoteAddress $TailnetCidr"
  } else {
    Set-NetFirewallRule -DisplayName $ruleName -Enabled True -Profile Any | Out-Null
    Get-NetFirewallRule -DisplayName $ruleName | Get-NetFirewallAddressFilter |
      Set-NetFirewallAddressFilter -RemoteAddress $TailnetCidr
    Write-Host "Firewall rule updated: $ruleName RemoteAddress $TailnetCidr"
  }
}

function Install-PublicKey {
  Assert-PublicKey $PublicKey
  if (-not $TargetUser) { $TargetUser = $env:USERNAME }

  Ensure-OpenSshServer

  $profile = if ($TargetUser -ieq $env:USERNAME) {
    $env:USERPROFILE
  } else {
    Join-Path (Join-Path $env:SystemDrive "Users") $TargetUser
  }
  if (-not (Test-Path -LiteralPath $profile)) {
    throw "Target user profile not found: $profile. Pass -TargetUser with the Windows account name."
  }

  $userKeys = Join-Path $profile ".ssh\authorized_keys"
  Add-KeyLine $userKeys $PublicKey
  Set-AuthorizedKeysAcl $userKeys $TargetUser

  $adminKeys = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"
  Add-KeyLine $adminKeys $PublicKey
  try {
    & icacls $adminKeys /inheritance:r /grant "Administrators:F" "SYSTEM:F" | Out-Null
  } catch {
    Write-Host "WARN: could not set administrators_authorized_keys ACL: $($_.Exception.Message)" -ForegroundColor Yellow
  }

  Restart-Service sshd
  Write-Host "SSH access installed for $TargetUser. From home, test with:"
  Write-Host "ssh -i `"$KeyPath`" -o BatchMode=yes $TargetUser@$env:COMPUTERNAME hostname"
}

function Invoke-SshCheck {
  if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is not available on this machine."
  }
  if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "Fleet SSH key not found: $KeyPath. Run -GenerateKey first."
  }

  $targets = $FleetTargets
  if ($Only.Count -gt 0) {
    $want = @($Only | ForEach-Object { $_.ToLowerInvariant() })
    $targets = @($FleetTargets | Where-Object {
      $want -contains $_.Name.ToLowerInvariant() -or
      $want -contains $_.User.ToLowerInvariant() -or
      $want -contains $_.Target.ToLowerInvariant()
    })
  }

  $failures = 0
  foreach ($target in $targets) {
    Write-Host ""
    Write-Host "[$($target.Name)] $($target.Target)" -ForegroundColor Yellow
    $args = @(
      "-i", $KeyPath,
      "-o", "IdentitiesOnly=yes",
      "-o", "BatchMode=yes",
      "-o", "ConnectTimeout=$SshTimeoutSeconds",
      "-o", "ConnectionAttempts=1",
      "-o", "StrictHostKeyChecking=accept-new",
      $target.Target,
      "hostname"
    )
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $out = & ssh @args 2>&1
      $code = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $oldErrorAction
    }
    if ($out) { $out | ForEach-Object { Write-Host $_ } }
    if ($code -ne 0) {
      $failures += 1
      Write-Host "FAILED: ssh exited $code" -ForegroundColor Red
    } else {
      Write-Host "OK" -ForegroundColor Green
    }
  }
  if ($failures -gt 0) { exit 1 }
}

if (-not $GenerateKey -and -not $InstallPublicKey -and -not $Check) {
  $Check = $true
}

if ($GenerateKey) { Ensure-FleetKey }
if ($InstallPublicKey) { Install-PublicKey }
if ($Check) { Invoke-SshCheck }
