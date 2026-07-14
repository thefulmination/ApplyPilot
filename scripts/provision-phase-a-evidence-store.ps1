[CmdletBinding(DefaultParameterSetName='Provision')]
param(
  [Parameter(ParameterSetName='DefinitionImport', Mandatory)][switch]$DefinitionImport,
  [Parameter(ParameterSetName='Provision')][string]$StoreRoot = (Join-Path $env:ProgramData 'ApplyPilot\Evidence\v1'),
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CanonicalOperatorSid,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$SigningSpkiPath,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$RecoverySigningSpkiPath,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$SigningSpkiSha256,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$RecoverySigningSpkiSha256,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodyReceiptPath,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodySignaturePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PhaseAEvidenceStore.psm1') -Force

function Test-PhaseAElevatedToken {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-PhaseAStoreFile([string]$Path, [byte[]]$Bytes, [string]$OperatorSid) {
  $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try { $stream.Write($Bytes); $stream.Flush($true) } finally { $stream.Dispose() }
  $module = Get-Module PhaseAEvidenceStore
  & $module { param($P,$S) Set-PhaseAProtectedAcl -Path $P -OperatorSid $S -File } $Path $OperatorSid
}

function Test-PhaseATestStore([string]$Root, [string]$OperatorSid, [string]$ExpectedTargetDigest) {
  $module = Get-Module PhaseAEvidenceStore
  & $module {
    param($R,$S,$ExpectedTarget)
    $null = Assert-PhaseALocalNtfsPath $R
    Assert-PhaseAProtectedAcl $R $S
    $names = @(Get-ChildItem -LiteralPath $R -Directory -Force | Sort-Object Name | ForEach-Object Name)
    if ($names -join ',' -cne 'adjudications,bundles,operations') { throw 'Evidence subdirectory set is invalid.' }
    foreach ($name in $names) { Assert-PhaseAProtectedAcl (Join-Path $R $name) $S }
    $config = Read-PhaseACanonicalJson (Join-Path $R 'store.json')
    Assert-PhaseAClosedFields $config.Value @('schema','targetDigest','operatorSidDigest','machineDigest','securityDescriptorSha256','signingSpkiSha256','recoverySigningSpkiSha256')
    $actualTarget = if ($ExpectedTarget) { $ExpectedTarget } else { Get-PhaseATargetDigest $R }
    if ($config.Value.schema -cne 'applypilot.phase-a.evidence-store.v1' -or
        $config.Value.targetDigest -cne $actualTarget -or
        $config.Value.operatorSidDigest -cne (Get-PhaseAOperatorSidDigest $S) -or
        $config.Value.securityDescriptorSha256 -cne (Get-PhaseASecurityDescriptorHash $R)) {
      throw 'Test evidence store configuration is invalid.'
    }
    return $config.Sha256
  } $Root $OperatorSid $ExpectedTargetDigest
}

function Invoke-PhaseAEvidenceStoreProvision {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [string]$SigningSpkiPath,
    [string]$RecoverySigningSpkiPath,
    [string]$SigningSpkiSha256,
    [string]$RecoverySigningSpkiSha256,
    [string]$CustodyReceiptPath,
    [string]$CustodySignaturePath,
    [switch]$TestIdentity
  )
  if (-not $DefinitionImport -and $TestIdentity) { throw 'Test identity overrides require -DefinitionImport.' }
  if (-not (Test-PhaseAElevatedToken)) { throw 'Evidence-store provisioning requires an elevated token.' }
  $module = Get-Module PhaseAEvidenceStore
  $operator = & $module { param($S) Assert-PhaseACurrentOperator $S } $CanonicalOperatorSid
  $final = & $module { param($P) Assert-PhaseALocalNtfsPath $P -AllowMissingLeaf } $StoreRoot
  if ([IO.Path]::GetFileName($final) -cne 'v1' -and -not $DefinitionImport) {
    throw 'Production evidence store must end in the v1 path.'
  }
  if (Test-Path -LiteralPath $final) {
    if ($TestIdentity) {
      $hash = Test-PhaseATestStore $final $operator
      return [pscustomobject]@{ StoreRoot=$final; StoreConfigSha256=$hash; Existing=$true }
    }
    return Assert-PhaseAEvidenceStore @PSBoundParameters
  }
  if (-not $TestIdentity) {
    foreach ($required in @($SigningSpkiPath,$RecoverySigningSpkiPath,$SigningSpkiSha256,
        $RecoverySigningSpkiSha256,$CustodyReceiptPath,$CustodySignaturePath)) {
      if ([string]::IsNullOrWhiteSpace($required)) { throw 'Committed public anchors and custody receipt are required.' }
    }
    $signing = & $module { param($P,$H) Import-PhaseASpki $P $H } $SigningSpkiPath $SigningSpkiSha256
    $signing.Dispose()
    $recovery = & $module { param($P,$H) Import-PhaseASpki $P $H } $RecoverySigningSpkiPath $RecoverySigningSpkiSha256
    $recovery.Dispose()
  }
  $parent = Split-Path -Parent $final
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent
  }
  & $module { param($P,$S) Assert-PhaseAAncestorDeleteChild $P $S } $final $operator
  $stage = Join-Path $parent ".provisioning-$([guid]::NewGuid().ToString('D'))"
  $null = New-Item -ItemType Directory -Path $stage
  & $module { param($P,$S) Set-PhaseAProtectedAcl $P $S } $stage $operator
  foreach ($name in @('bundles','adjudications','operations')) {
    $directory = New-Item -ItemType Directory -Path (Join-Path $stage $name)
    & $module { param($P,$S) Set-PhaseAProtectedAcl $P $S } $directory.FullName $operator
  }
  $target = Get-PhaseATargetDigest -Path $stage -CanonicalPath $final
  $machine = if ($TestIdentity) {
    Get-PhaseAMachineDigest -MachineGuid '01234567-89ab-cdef-0123-456789abcdef' -SmbiosUuid 'fedcba98-7654-3210-fedc-ba9876543210' -DefinitionImport
  } else { Get-PhaseAMachineDigest }
  $config = [ordered]@{
    schema='applypilot.phase-a.evidence-store.v1'
    targetDigest=$target
    operatorSidDigest=(Get-PhaseAOperatorSidDigest $operator)
    machineDigest=$machine
    securityDescriptorSha256=(Get-PhaseASecurityDescriptorHash $stage)
    signingSpkiSha256=if ($SigningSpkiSha256) { $SigningSpkiSha256 } else { '0' * 64 }
    recoverySigningSpkiSha256=if ($RecoverySigningSpkiSha256) { $RecoverySigningSpkiSha256 } else { '0' * 64 }
  }
  $bytes = & $module { param($V) ConvertTo-PhaseACanonicalJsonBytes $V } $config
  Write-PhaseAStoreFile (Join-Path $stage 'store.json') $bytes $operator
  $null = Test-PhaseATestStore $stage $operator $target
  [ApplyPilot.PhaseA.EvidenceNative]::RenameDirectoryNoReplace($stage, $final)
  if ($TestIdentity) {
    $hash = Test-PhaseATestStore $final $operator
    return [pscustomobject]@{ StoreRoot=$final; StoreConfigSha256=$hash; Existing=$false }
  }
  return Assert-PhaseAEvidenceStore -StoreRoot $final -CanonicalOperatorSid $operator `
    -SigningSpkiPath $SigningSpkiPath -RecoverySigningSpkiPath $RecoverySigningSpkiPath `
    -SigningSpkiSha256 $SigningSpkiSha256 -RecoverySigningSpkiSha256 $RecoverySigningSpkiSha256 `
    -CustodyReceiptPath $CustodyReceiptPath -CustodySignaturePath $CustodySignaturePath
}

function Install-PhaseABootstrapReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [string]$ExpectedSigningSpkiSha256
  )
  $root = Join-Path $env:LOCALAPPDATA 'ApplyPilot\phase-a-evidence\bootstrap-operations'
  $full = [IO.Path]::GetFullPath($root)
  $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
  $oneDrive = if ($env:OneDrive) { [IO.Path]::GetFullPath($env:OneDrive) } else { '' }
  if ($full.StartsWith($repo, [StringComparison]::OrdinalIgnoreCase) -or
      ($oneDrive -and $full.StartsWith($oneDrive, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Bootstrap receipt root must be outside the repository and OneDrive.'
  }
  if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $root -Force
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $module = Get-Module PhaseAEvidenceStore
    & $module { param($P,$S) Set-PhaseAProtectedAcl $P $S } $root $sid
  }
  Install-PhaseASignedReceipt -ReceiptPath $ReceiptPath -SignaturePath $SignaturePath `
    -DestinationDirectory $root -SigningSpkiPath $SigningSpkiPath `
    -ExpectedSigningSpkiSha256 $ExpectedSigningSpkiSha256
}

function Get-PhaseAManifestDigest([string]$Root) {
  $manifest = Get-PhaseADirectoryManifest -Root $Root
  $module = Get-Module PhaseAEvidenceStore
  $bytes = & $module { param($Value) ConvertTo-PhaseACanonicalJsonBytes $Value } $manifest
  return [pscustomobject]@{ Value=$manifest; Bytes=$bytes; Sha256=(& $module { param($B) Get-PhaseASha256 $B } $bytes) }
}

function Invoke-PhaseAProvisioningCleanup {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StagingPath,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiPath,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiSha256,
    [Parameter(Mandatory)][string]$AuthorizationReceiptPath,
    [Parameter(Mandatory)][string]$AuthorizationSignaturePath,
    [Parameter(Mandatory)][string]$CompletionReceiptPath,
    [Parameter(Mandatory)][string]$CompletionSignaturePath,
    [Parameter(Mandatory)][string]$ExpectedAfterManifestPath
  )
  if (-not (Test-PhaseAElevatedToken)) { throw 'Staging cleanup requires an elevated token.' }
  $module = Get-Module PhaseAEvidenceStore
  $operator = & $module { param($S) Assert-PhaseACurrentOperator $S } $CanonicalOperatorSid
  $stage = & $module { param($P) Assert-PhaseALocalNtfsPath $P } $StagingPath
  if ([IO.Path]::GetFileName($stage) -cnotmatch '^\.provisioning-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
    throw 'Cleanup target is not an exact provisioning-stage identity.'
  }
  $parent = Split-Path -Parent $stage
  $validStore = Join-Path $parent 'v1'
  if (-not (Test-Path -LiteralPath $validStore -PathType Container)) {
    $bootstrap = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ApplyPilot\phase-a-evidence\bootstrap-operations'))
    foreach ($receipt in @($AuthorizationReceiptPath,$CompletionReceiptPath)) {
      if ([IO.Path]::GetFullPath((Split-Path -Parent $receipt)) -ine $bootstrap) {
        throw 'Before valid v1, cleanup receipts must be installed in bootstrap operations.'
      }
    }
    & $module { param($P,$S) Assert-PhaseAProtectedAcl $P $S } $bootstrap $operator
  }
  $before = Get-PhaseAManifestDigest $parent
  $expected = & $module { param($P) Read-PhaseACanonicalJson $P } $ExpectedAfterManifestPath
  & $module { param($V) Assert-PhaseAClosedFields $V @('schema','entries') } $expected.Value
  if ($expected.Value.schema -cne 'applypilot.phase-a.directory-manifest.v1') {
    throw 'Expected-after manifest has the wrong schema.'
  }
  $stageName = [IO.Path]::GetFileName($stage)
  $calculatedEntries = @($before.Value.entries | Where-Object {
    $_.relativePath -cne $stageName -and -not $_.relativePath.StartsWith("$stageName/", [StringComparison]::Ordinal)
  })
  $calculatedAfter = [ordered]@{ schema='applypilot.phase-a.directory-manifest.v1'; entries=$calculatedEntries }
  $calculatedBytes = & $module { param($V) ConvertTo-PhaseACanonicalJsonBytes $V } $calculatedAfter
  if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($calculatedBytes, $expected.Bytes)) {
    throw 'Expected-after manifest removes more or less than the exact staging identity.'
  }
  $target = Get-PhaseATargetDigest $stage
  $authorization = & $module { param($P) Read-PhaseACanonicalJson $P } $AuthorizationReceiptPath
  $operationId = [string]$authorization.Value.operationId
  $null = Test-PhaseASignedReceipt -ReceiptPath $AuthorizationReceiptPath `
    -SignaturePath $AuthorizationSignaturePath -SigningSpkiPath $RecoverySigningSpkiPath `
    -ExpectedSigningSpkiSha256 $RecoverySigningSpkiSha256 -ExpectedReceiptType 'operation-authorization' `
    -ExpectedOperationId $operationId -ExpectedTargetDigest $target `
    -ExpectedManifestBeforeSha256 $before.Sha256 -ExpectedManifestAfterSha256 $expected.Sha256
  $null = Test-PhaseASignedReceipt -ReceiptPath $CompletionReceiptPath `
    -SignaturePath $CompletionSignaturePath -SigningSpkiPath $RecoverySigningSpkiPath `
    -ExpectedSigningSpkiSha256 $RecoverySigningSpkiSha256 -ExpectedReceiptType 'operation-completion' `
    -ExpectedOperationId $operationId -ExpectedTargetDigest $target `
    -ExpectedManifestBeforeSha256 $before.Sha256 -ExpectedManifestAfterSha256 $expected.Sha256
  $identityLease = Open-PhaseAValidatedDirectoryLease -Path $stage
  try { $identity = Get-PhaseAFileIdentity -Handle $identityLease } finally { $identityLease.Dispose() }
  $confirmationLease = Open-PhaseAValidatedDirectoryLease -Path $stage
  try { $confirmation = Get-PhaseAFileIdentity -Handle $confirmationLease } finally { $confirmationLease.Dispose() }
  if ($confirmation.VolumeSerialNumber -ne $identity.VolumeSerialNumber -or $confirmation.FileId -cne $identity.FileId) {
    throw 'Provisioning-stage identity changed before cleanup.'
  }
  Remove-Item -LiteralPath $stage -Recurse -Force
  $actualAfter = Get-PhaseAManifestDigest $parent
  if ($actualAfter.Sha256 -cne $expected.Sha256) { throw 'Cleanup result does not match the authorized after manifest.' }
  return [pscustomobject]@{ RemovedTargetDigest=$target; OperationId=$operationId; ManifestAfterSha256=$actualAfter.Sha256 }
}

if ($PSCmdlet.ParameterSetName -eq 'DefinitionImport') { return }

Invoke-PhaseAEvidenceStoreProvision -StoreRoot $StoreRoot `
  -CanonicalOperatorSid $CanonicalOperatorSid -SigningSpkiPath $SigningSpkiPath `
  -RecoverySigningSpkiPath $RecoverySigningSpkiPath -SigningSpkiSha256 $SigningSpkiSha256 `
  -RecoverySigningSpkiSha256 $RecoverySigningSpkiSha256 -CustodyReceiptPath $CustodyReceiptPath `
  -CustodySignaturePath $CustodySignaturePath
