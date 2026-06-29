# run-fleet-console.ps1 -- launch the ApplyPilot fleet control panel.
#
# LAN-ONLY. This panel operates a system that submits REAL job applications, so it binds
# a PRIVATE LAN IPv4 (10.x / 172.16-31.x / 192.168.x) reachable only from your own network,
# falling back to 127.0.0.1. NEVER port-forward this, never expose it to the internet, and
# never pass it a public IP or 0.0.0.0 -- the server itself refuses those, but don't try.

$ErrorActionPreference = "Stop"

$RepoRoot  = "C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot"
$PyExe     = "C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.conda-env/python.exe"
$FleetDsn  = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
$Port      = 8787   # 8765 is used by an unrelated local app (radio_digest); 8787 is free

# DSN comes from the environment (pgpass makes it passwordless). Both names are read by
# the reused fleet helpers (pgqueue.connect / codex_bridge). Never echoed to the console.
$env:APPLYPILOT_FLEET_DSN = $FleetDsn
$env:FLEET_PG_DSN         = $FleetDsn

Set-Location $RepoRoot

# Detect THIS box's private LAN IPv4 so the URL is reachable from other LAN machines.
# Prefer 192.168.x, then 10.x, then 172.16-31.x; fall back to 127.0.0.1.
function Get-PrivateIPv4 {
    try {
        $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -and $_.AddressState -eq "Preferred" } |
            Select-Object -ExpandProperty IPAddress
    } catch {
        $addrs = @()
    }
    foreach ($pat in @('^192\.168\.', '^10\.', '^172\.(1[6-9]|2[0-9]|3[0-1])\.')) {
        $hit = $addrs | Where-Object { $_ -match $pat } | Select-Object -First 1
        if ($hit) { return $hit }
    }
    return "127.0.0.1"
}

$BindIp = Get-PrivateIPv4

Write-Host ""
Write-Host "ApplyPilot Fleet Console (LAN-only)" -ForegroundColor Cyan
Write-Host "Open this URL on any machine on your LAN:" -ForegroundColor Cyan
Write-Host ("    http://{0}:{1}" -f $BindIp, $Port) -ForegroundColor Green
Write-Host "Do NOT port-forward this. It controls a system that submits REAL applications." -ForegroundColor Yellow
Write-Host ""

& $PyExe -m applypilot.fleet.console_app --host $BindIp --port $Port
