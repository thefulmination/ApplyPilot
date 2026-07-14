[CmdletBinding(DefaultParameterSetName='Unsigned')]
param(
  [Parameter(Mandatory)][string]$InputPath,
  [Parameter(Mandatory)][string]$SigningSpkiPath,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][switch]$CreateUnsigned,
  [Parameter(Mandatory, ParameterSetName='Verify')][switch]$VerifyReturnedSignature,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][string]$OutputDirectory,
  [Parameter(Mandatory, ParameterSetName='Verify')][string]$SignaturePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PhaseAEvidenceStore.psm1') -Force

if ($PSCmdlet.ParameterSetName -eq 'Verify') {
  $null = Test-PhaseASignedReceipt -ReceiptPath $InputPath -SignaturePath $SignaturePath -SigningSpkiPath $SigningSpkiPath
  [IO.Path]::GetFullPath($InputPath)
  exit 0
}

$module = Get-Module PhaseAEvidenceStore
$parsed = & $module {
  param($Path, $SpkiPath)
  $document = Read-PhaseACanonicalJson $Path
  Assert-PhaseAClosedFields $document.Value $script:ReceiptFields
  if ($document.Value.schema -cne 'applypilot.phase-a.signed-receipt.v1' -or
      $script:ReceiptTypes -cnotcontains $document.Value.receiptType) {
    throw 'Unsupported receipt schema or type.'
  }
  foreach ($name in @('signingKeySpkiSha256','targetDigest','operatorSidDigest','machineDigest',
      'storeConfigSha256','hostProvisioningReceiptSha256','sourceApprovalReceiptSha256',
      'manifestBeforeSha256','manifestAfterSha256')) {
    Assert-PhaseAHexDigest ([string]$document.Value[$name]) $name
  }
  $operation = [guid]::Empty
  if (-not [guid]::TryParseExact([string]$document.Value.operationId, 'D', [ref]$operation)) {
    throw 'Invalid operation ID.'
  }
  $spkiHash = Get-PhaseASha256 ([IO.File]::ReadAllBytes($SpkiPath))
  if ($document.Value.signingKeySpkiSha256 -cne $spkiHash) {
    throw 'Receipt signing-key binding is wrong.'
  }
  return $document
} $InputPath $SigningSpkiPath

$output = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $output -PathType Container)) {
  $null = New-Item -ItemType Directory -Path $output
}
$destination = Join-Path $output "$($parsed.Sha256).json"
try {
  $stream = [IO.FileStream]::new($destination, [IO.FileMode]::CreateNew,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try {
    $stream.Write($parsed.Bytes)
    $stream.Flush($true)
  } finally { $stream.Dispose() }
} catch [IO.IOException] {
  if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or
      -not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
        [IO.File]::ReadAllBytes($destination), $parsed.Bytes)) { throw }
}
$destination
