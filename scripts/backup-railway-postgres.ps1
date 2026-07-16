[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [string]$PgDumpPath = "C:\\Program Files\\PostgreSQL\\18\\bin\\pg_dump.exe"
)

$ErrorActionPreference = "Stop"

if (-not $env:DATABASE_PUBLIC_URL) {
    throw "DATABASE_PUBLIC_URL is not available; run this through Railway variable injection."
}
if (-not (Test-Path -LiteralPath $PgDumpPath -PathType Leaf)) {
    throw "pg_dump was not found at the configured path."
}

$parent = Split-Path -Parent $OutputPath
if (-not $parent) {
    throw "OutputPath must include a parent directory."
}
New-Item -ItemType Directory -Force -Path $parent | Out-Null

$resolvedParent = (Resolve-Path -LiteralPath $parent).Path
$resolvedOutput = Join-Path $resolvedParent (Split-Path -Leaf $OutputPath)
$partial = "$resolvedOutput.partial"

try {
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    $dumpArgs = @(
        "--dbname=$env:DATABASE_PUBLIC_URL",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file=$partial"
    )
    & $PgDumpPath @dumpArgs
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit $LASTEXITCODE."
    }

    $item = Get-Item -LiteralPath $partial
    if ($item.Length -le 0) {
        throw "pg_dump produced an empty file."
    }

    $pgRestore = Join-Path (Split-Path -Parent $PgDumpPath) "pg_restore.exe"
    & $pgRestore --list $partial | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore could not read the generated archive."
    }

    Move-Item -LiteralPath $partial -Destination $resolvedOutput
    $final = Get-Item -LiteralPath $resolvedOutput
    $stream = [System.IO.File]::OpenRead($resolvedOutput)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash($stream)
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    $hash = [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    [pscustomobject]@{
        path = $final.FullName
        bytes = $final.Length
        sha256 = $hash
    } | ConvertTo-Json -Compress
} finally {
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
}
