[CmdletBinding(DefaultParameterSetName='Unsigned')]
param(
  [Parameter(Mandatory)][ValidateSet('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion','host-provisioning')][string]$ReceiptType,
  [Parameter(Mandatory)][string]$Commit,
  [Parameter(Mandatory)][string]$SigningSpkiPath,
  [Parameter(Mandatory)][string]$ExpectedSigningSpkiSha256,
  [Parameter(Mandatory)][string]$OperationId,
  [Parameter(Mandatory)][string]$TargetDigest,
  [Parameter(Mandatory)][string]$OperatorSidDigest,
  [Parameter(Mandatory)][string]$MachineDigest,
  [Parameter(Mandatory)][string]$StoreConfigSha256,
  [Parameter(Mandatory)][string]$ManifestBeforeSha256,
  [Parameter(Mandatory)][string]$ManifestAfterSha256,
  [string]$HostProvisioningReceiptSha256,
  [string]$SourceApprovalReceiptSha256,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][switch]$CreateUnsigned,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][string]$OutputDirectory,
  [Parameter(Mandatory, ParameterSetName='Verify')][switch]$VerifyReturnedSignature,
  [Parameter(Mandatory, ParameterSetName='Verify')][string]$ReceiptPath,
  [Parameter(Mandatory, ParameterSetName='Verify')][string]$SignaturePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PhaseAEvidenceStore.psm1') -Force
$module = Get-Module PhaseAEvidenceStore

$value = & $module {
  param($Type,$ApprovedCommit,$SpkiPath,$SpkiHash,$OpId,$Target,$Operator,$Machine,$Store,$Before,$After,$Host,$Source)
  if ($ApprovedCommit -cnotmatch '^[0-9a-f]{40}$') { throw 'Approved commit must be a lowercase 40-hex commit.' }
  $parsedOperation = [guid]::Empty
  if (-not [guid]::TryParseExact($OpId, 'D', [ref]$parsedOperation)) { throw 'Operation ID is invalid.' }
  foreach ($binding in @($SpkiHash,$Target,$Operator,$Machine,$Store,$Before,$After)) {
    Assert-PhaseAHexDigest $binding 'Receipt binding'
  }
  $needsHost = $Type -in @('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion')
  $needsSource = $Type -in @('adjudication','operation-authorization','operation-completion')
  if ($needsHost) { Assert-PhaseAHexDigest $Host 'Host-provisioning receipt binding' }
  elseif ($Host) { throw 'Host-provisioning binding is not part of this receipt schema.' }
  if ($needsSource) { Assert-PhaseAHexDigest $Source 'Source-approval receipt binding' }
  elseif ($Source) { throw 'Source-approval binding is not part of this receipt schema.' }
  $rsa = Import-PhaseASpki $SpkiPath $SpkiHash; $rsa.Dispose()
  $receipt = [ordered]@{
    schema='applypilot.phase-a.signed-receipt.v1'; receiptType=$Type; commit=$ApprovedCommit;
    signingKeySpkiSha256=$SpkiHash; operationId=$OpId; targetDigest=$Target;
    operatorSidDigest=$Operator; machineDigest=$Machine; storeConfigSha256=$Store
  }
  if ($needsHost) { $receipt.hostProvisioningReceiptSha256=$Host }
  if ($needsSource) { $receipt.sourceApprovalReceiptSha256=$Source }
  $receipt.manifestBeforeSha256=$Before
  $receipt.manifestAfterSha256=$After
  Assert-PhaseAClosedFields $receipt $script:ReceiptFieldsByType[$Type]
  return $receipt
} $ReceiptType $Commit $SigningSpkiPath $ExpectedSigningSpkiSha256 $OperationId $TargetDigest `
  $OperatorSidDigest $MachineDigest $StoreConfigSha256 $ManifestBeforeSha256 $ManifestAfterSha256 `
  $HostProvisioningReceiptSha256 $SourceApprovalReceiptSha256

$bytes = & $module { param($Value) ConvertTo-PhaseACanonicalJsonBytes $Value } $value
$digest = & $module { param($Bytes) Get-PhaseASha256 $Bytes } $bytes

if ($PSCmdlet.ParameterSetName -eq 'Verify') {
  $received = & $module { param($Path) Read-PhaseAValidatedBytes $Path } $ReceiptPath
  if ([IO.Path]::GetFileName($ReceiptPath) -cne "$digest.json" -or
      -not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($received.Bytes, $bytes)) {
    throw 'Returned receipt does not exactly match the constructed canonical bytes.'
  }
  $arguments = @{
    ReceiptPath=$ReceiptPath; SignaturePath=$SignaturePath; SigningSpkiPath=$SigningSpkiPath;
    ExpectedSigningSpkiSha256=$ExpectedSigningSpkiSha256; ExpectedReceiptType=$ReceiptType;
    ExpectedCommit=$Commit; ExpectedOperationId=$OperationId; ExpectedTargetDigest=$TargetDigest;
    ExpectedOperatorSidDigest=$OperatorSidDigest; ExpectedMachineDigest=$MachineDigest;
    ExpectedStoreConfigSha256=$StoreConfigSha256; ExpectedManifestBeforeSha256=$ManifestBeforeSha256;
    ExpectedManifestAfterSha256=$ManifestAfterSha256
  }
  if ($HostProvisioningReceiptSha256) { $arguments.ExpectedHostProvisioningReceiptSha256=$HostProvisioningReceiptSha256 }
  if ($SourceApprovalReceiptSha256) { $arguments.ExpectedSourceApprovalReceiptSha256=$SourceApprovalReceiptSha256 }
  $null = Test-PhaseASignedReceipt @arguments
  [IO.Path]::GetFullPath($ReceiptPath)
  exit 0
}

$output = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $output -PathType Container)) { $null = New-Item -ItemType Directory -Path $output }
$destination = Join-Path $output "$digest.json"
$stream = [IO.FileStream]::new($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try { $stream.Write($bytes); $stream.Flush($true) } finally { $stream.Dispose() }
$destination
