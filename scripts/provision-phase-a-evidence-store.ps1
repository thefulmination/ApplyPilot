[CmdletBinding(DefaultParameterSetName='Provision')]
param(
  [Parameter(ParameterSetName='DefinitionImport', Mandatory)][switch]$DefinitionImport,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CanonicalOperatorSid,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$ExpectedCommit,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodyReceiptPath,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodySignaturePath,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodyOperationId,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodyManifestBeforeSha256,
  [Parameter(ParameterSetName='Provision', Mandatory)][string]$CustodyManifestAfterSha256
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

function Invoke-PhaseAEvidenceStoreProvision {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiPath,
    [Parameter(Mandatory)][string]$SigningSpkiSha256,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiSha256,
    [Parameter(Mandatory)][string]$CustodyOperationId,
    [Parameter(Mandatory)][string]$CustodyManifestBeforeSha256,
    [Parameter(Mandatory)][string]$CustodyManifestAfterSha256,
    [string]$CustodyReceiptPath,
    [string]$CustodySignaturePath,
    [scriptblock]$HostReceiptMaterializer,
    [string]$TestMachineGuid,
    [string]$TestSmbiosUuid,
    [string]$TestAncestorBoundary,
    [switch]$CrashBeforePublication,
    [switch]$DefinitionImport
  )
  if (-not $DefinitionImport -and ($HostReceiptMaterializer -or $TestMachineGuid -or $TestSmbiosUuid -or $TestAncestorBoundary -or $CrashBeforePublication)) {
    throw 'Provisioning overrides require DefinitionImport.'
  }
  if (-not $DefinitionImport -and -not (Test-PhaseAElevatedToken)) { throw 'Evidence-store provisioning requires an elevated token.' }
  $module = Get-Module PhaseAEvidenceStore
  $operator = & $module { param($S) Assert-PhaseACurrentOperator $S } $CanonicalOperatorSid
  $final = & $module { param($P) Assert-PhaseALocalNtfsPath $P -AllowMissingLeaf } $StoreRoot
  $productionRoot = & $module { $script:ProductionStoreRoot }
  if (-not $DefinitionImport -and $final -ine $productionRoot) {
    throw 'Production evidence store root is not the exact native ProgramData path.'
  }
  $machine = if ($DefinitionImport -and $TestMachineGuid -and $TestSmbiosUuid) {
    Get-PhaseAMachineDigest -MachineGuid $TestMachineGuid -SmbiosUuid $TestSmbiosUuid -DefinitionImport
  } elseif ($TestMachineGuid -or $TestSmbiosUuid) { throw 'Both test machine identity values are required.' }
  else { Get-PhaseAMachineDigest }
  $assertArguments = @{
    CanonicalOperatorSid=$operator; ExpectedCommit=$ExpectedCommit; SigningSpkiPath=$SigningSpkiPath;
    RecoverySigningSpkiPath=$RecoverySigningSpkiPath; SigningSpkiSha256=$SigningSpkiSha256;
    RecoverySigningSpkiSha256=$RecoverySigningSpkiSha256; CustodyOperationId=$CustodyOperationId;
    CustodyManifestBeforeSha256=$CustodyManifestBeforeSha256; CustodyManifestAfterSha256=$CustodyManifestAfterSha256
  }
  if ($DefinitionImport) {
    $assertArguments.DefinitionImport=$true; $assertArguments.ExpectedMachineDigest=$machine
    if ($TestAncestorBoundary) { $assertArguments.AncestorBoundary=$TestAncestorBoundary }
  }
  if (Test-Path -LiteralPath $final) {
    return Assert-PhaseAEvidenceStore -StoreRoot $final @assertArguments
  }
  $signing = & $module { param($P,$H) Import-PhaseASpki $P $H } $SigningSpkiPath $SigningSpkiSha256; $signing.Dispose()
  $recovery = & $module { param($P,$H) Import-PhaseASpki $P $H } $RecoverySigningSpkiPath $RecoverySigningSpkiSha256; $recovery.Dispose()
  $parent = Split-Path -Parent $final
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent
  }
  & $module { param($P,$S,$B) Assert-PhaseAAncestorDeleteChild $P $S $B } $final $operator $TestAncestorBoundary
  $stage = Join-Path $parent ".provisioning-$([guid]::NewGuid().ToString('D'))"
  $null = New-Item -ItemType Directory -Path $stage
  & $module { param($P,$S) Set-PhaseAProtectedAcl $P $S } $stage $operator
  foreach ($name in @('bundles','adjudications','operations')) {
    $directory = New-Item -ItemType Directory -Path (Join-Path $stage $name)
    & $module { param($P,$S) Set-PhaseAProtectedAcl $P $S } $directory.FullName $operator
  }
  $target = Get-PhaseATargetDigest -Path $stage -CanonicalPath $final
  $config = [ordered]@{
    schema='applypilot.phase-a.evidence-store.v1'
    approvedCommit=$ExpectedCommit
    targetDigest=$target
    operatorSidDigest=(Get-PhaseAOperatorSidDigest $operator)
    machineDigest=$machine
    securityDescriptorSha256=(Get-PhaseASecurityDescriptorHash $stage)
    signingSpkiSha256=$SigningSpkiSha256
    recoverySigningSpkiSha256=$RecoverySigningSpkiSha256
  }
  $bytes = & $module { param($V) ConvertTo-PhaseACanonicalJsonBytes $V } $config
  Write-PhaseAStoreFile (Join-Path $stage 'store.json') $bytes $operator
  $configHash = & $module { param($B) Get-PhaseASha256 $B } $bytes
  if ($HostReceiptMaterializer) {
    $material = & $HostReceiptMaterializer ([pscustomobject]@{
      StoreRoot=$stage; TargetDigest=$target; OperatorSidDigest=(Get-PhaseAOperatorSidDigest $operator);
      MachineDigest=$machine; StoreConfigSha256=$configHash; RecoverySigningSpkiSha256=$RecoverySigningSpkiSha256;
      Commit=$ExpectedCommit; OperationId=$CustodyOperationId; ManifestBeforeSha256=$CustodyManifestBeforeSha256;
      ManifestAfterSha256=$CustodyManifestAfterSha256
    })
    $CustodyReceiptPath = [string]$material.ReceiptPath
    $CustodySignaturePath = [string]$material.SignaturePath
  }
  if ([string]::IsNullOrWhiteSpace($CustodyReceiptPath) -or [string]::IsNullOrWhiteSpace($CustodySignaturePath)) {
    throw 'A complete host-provisioning receipt pair is required before publication.'
  }
  $null = Install-PhaseASignedReceipt -ReceiptPath $CustodyReceiptPath -SignaturePath $CustodySignaturePath `
    -StoreRoot $stage -SigningSpkiPath $RecoverySigningSpkiPath `
    -ExpectedSigningSpkiSha256 $RecoverySigningSpkiSha256 -ExpectedReceiptType host-provisioning `
    -ExpectedCommit $ExpectedCommit -ExpectedOperationId $CustodyOperationId -ExpectedTargetDigest $target `
    -ExpectedOperatorSidDigest (Get-PhaseAOperatorSidDigest $operator) -ExpectedMachineDigest $machine `
    -ExpectedManifestBeforeSha256 $CustodyManifestBeforeSha256 -ExpectedManifestAfterSha256 $CustodyManifestAfterSha256 `
    -ExpectedStoreConfigSha256 $configHash -DefinitionImport
  $assertArguments.StoreRoot=$stage; $assertArguments.ExpectedTargetDigest=$target
  $validatedStage = Assert-PhaseAEvidenceStore @assertArguments
  if ($CrashBeforePublication) { throw 'Injected crash before evidence-store publication.' }
  [ApplyPilot.PhaseA.EvidenceNative]::RenameDirectoryNoReplace($stage, $final)
  $assertArguments.Remove('ExpectedTargetDigest')
  $assertArguments.Remove('StoreRoot')
  return Assert-PhaseAEvidenceStore -StoreRoot $final @assertArguments
}

function Install-PhaseABootstrapReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$ExpectedSigningSpkiSha256,
    [Parameter(Mandatory)][ValidateSet('credential-revocation','operation-authorization','operation-completion','host-provisioning')][string]$ExpectedReceiptType,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedOperationId,
    [Parameter(Mandatory)][string]$ExpectedTargetDigest,
    [Parameter(Mandatory)][string]$ExpectedOperatorSidDigest,
    [Parameter(Mandatory)][string]$ExpectedMachineDigest,
    [Parameter(Mandatory)][string]$ExpectedStoreConfigSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestBeforeSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestAfterSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedSourceApprovalReceiptSha256
  )
  $root = [IO.Path]::Combine(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData),
    'ApplyPilot', 'phase-a-evidence', 'bootstrap-operations')
  $full = [IO.Path]::GetFullPath($root)
  $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
  $oneDriveValue = [Environment]::GetEnvironmentVariable('OneDrive', 'User')
  $oneDrive = if ($oneDriveValue) { [IO.Path]::GetFullPath($oneDriveValue) } else { '' }
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
  $arguments = @{
    ReceiptPath=$ReceiptPath; SignaturePath=$SignaturePath; StoreRoot=$root; Bootstrap=$true;
    SigningSpkiPath=$SigningSpkiPath; ExpectedSigningSpkiSha256=$ExpectedSigningSpkiSha256;
    ExpectedReceiptType=$ExpectedReceiptType; ExpectedCommit=$ExpectedCommit; ExpectedOperationId=$ExpectedOperationId;
    ExpectedTargetDigest=$ExpectedTargetDigest; ExpectedOperatorSidDigest=$ExpectedOperatorSidDigest;
    ExpectedMachineDigest=$ExpectedMachineDigest; ExpectedStoreConfigSha256=$ExpectedStoreConfigSha256;
    ExpectedManifestBeforeSha256=$ExpectedManifestBeforeSha256; ExpectedManifestAfterSha256=$ExpectedManifestAfterSha256
  }
  if ($ExpectedHostProvisioningReceiptSha256) { $arguments.ExpectedHostProvisioningReceiptSha256=$ExpectedHostProvisioningReceiptSha256 }
  if ($ExpectedSourceApprovalReceiptSha256) { $arguments.ExpectedSourceApprovalReceiptSha256=$ExpectedSourceApprovalReceiptSha256 }
  Install-PhaseASignedReceipt @arguments
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
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedOperationId,
    [Parameter(Mandatory)][string]$ExpectedTargetDigest,
    [Parameter(Mandatory)][string]$ExpectedOperatorSidDigest,
    [Parameter(Mandatory)][string]$ExpectedMachineDigest,
    [Parameter(Mandatory)][string]$ExpectedStoreConfigSha256,
    [Parameter(Mandatory)][string]$ExpectedHostProvisioningReceiptSha256,
    [Parameter(Mandatory)][string]$ExpectedSourceApprovalReceiptSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestBeforeSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestAfterSha256,
    [Parameter(Mandatory)][string]$AuthorizationReceiptPath,
    [Parameter(Mandatory)][string]$AuthorizationSignaturePath,
    [Parameter(Mandatory)][string]$CompletionReceiptPath,
    [Parameter(Mandatory)][string]$CompletionSignaturePath,
    [Parameter(Mandatory)][string]$ExpectedAfterManifestPath,
    [int]$CrashAfterEntries = -1,
    [switch]$CrashAfterMutation,
    [string]$TestBootstrapRoot,
    [scriptblock]$BeforeCleanupDelete
  )
  if (-not $DefinitionImport -and -not (Test-PhaseAElevatedToken)) { throw 'Staging cleanup requires an elevated token.' }
  if ($TestBootstrapRoot -and -not $DefinitionImport) { throw 'Bootstrap override requires DefinitionImport.' }
  $module = Get-Module PhaseAEvidenceStore
  $operator = & $module { param($S) Assert-PhaseACurrentOperator $S } $CanonicalOperatorSid
  $stage = & $module { param($P) Assert-PhaseALocalNtfsPath $P -AllowMissingLeaf } $StagingPath
  if ([IO.Path]::GetFileName($stage) -cnotmatch '^\.provisioning-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
    throw 'Cleanup target is not an exact provisioning-stage identity.'
  }
  $parent = Split-Path -Parent $stage
  $bootstrap = [IO.Path]::Combine(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData),
    'ApplyPilot', 'phase-a-evidence', 'bootstrap-operations')
  if ($TestBootstrapRoot) { $bootstrap = [IO.Path]::GetFullPath($TestBootstrapRoot) }
  foreach ($receipt in @($AuthorizationReceiptPath,$CompletionReceiptPath)) {
    if ([IO.Path]::GetFullPath((Split-Path -Parent $receipt)) -ine $bootstrap) {
      throw 'Cleanup receipts require the protected bootstrap operations root without a full validated-store context.'
    }
  }
  & $module { param($P,$S) Assert-PhaseAProtectedAcl $P $S } $bootstrap $operator
  $expected = & $module { param($P) Read-PhaseACanonicalJson $P } $ExpectedAfterManifestPath
  & $module { param($V) Assert-PhaseAClosedFields $V @('schema','entries') } $expected.Value
  if ($expected.Value.schema -cne 'applypilot.phase-a.directory-manifest.v1') {
    throw 'Expected-after manifest has the wrong schema.'
  }
  $authorizationPair = $null
  $completionPair = $null
  try {
    $authorizationPair = & $module { param($R,$S,$O) Open-PhaseAProtectedReceiptPair $R $S $O } `
      $AuthorizationReceiptPath $AuthorizationSignaturePath $operator
    $completionPair = & $module { param($R,$S,$O) Open-PhaseAProtectedReceiptPair $R $S $O } `
      $CompletionReceiptPath $CompletionSignaturePath $operator
    $commonValidation = @{
      SigningSpkiPath=$RecoverySigningSpkiPath; ExpectedSigningSpkiSha256=$RecoverySigningSpkiSha256;
      ExpectedCommit=$ExpectedCommit; ExpectedOperationId=$ExpectedOperationId;
      ExpectedTargetDigest=$ExpectedTargetDigest; ExpectedOperatorSidDigest=$ExpectedOperatorSidDigest;
      ExpectedMachineDigest=$ExpectedMachineDigest; ExpectedStoreConfigSha256=$ExpectedStoreConfigSha256;
      ExpectedHostProvisioningReceiptSha256=$ExpectedHostProvisioningReceiptSha256;
      ExpectedSourceApprovalReceiptSha256=$ExpectedSourceApprovalReceiptSha256;
      ExpectedManifestBeforeSha256=$ExpectedManifestBeforeSha256;
      ExpectedManifestAfterSha256=$ExpectedManifestAfterSha256
    }
    $authorizationValidation = @{} + $commonValidation
    $authorizationValidation.ExpectedReceiptType = 'operation-authorization'
    $null = & $module { param($Pair,$Arguments)
      $Arguments.Receipt=$Pair.Receipt; $Arguments.SignatureRead=$Pair.Signature
      $result=Test-PhaseASignedReceiptCore @Arguments
      Assert-PhaseAProtectedReceiptPairIdentity $Pair
      return $result
    } $authorizationPair $authorizationValidation
    if ($expected.Sha256 -cne $ExpectedManifestAfterSha256) { throw 'Expected-after manifest hash is not authorized.' }
    if (Test-Path -LiteralPath $stage -PathType Container) {
    $before = Get-PhaseAManifestDigest $parent
    if ($before.Sha256 -cne $ExpectedManifestBeforeSha256) { throw 'Current cleanup manifest is not authorized.' }
    $stageName = [IO.Path]::GetFileName($stage)
    $calculatedEntries = @($before.Value.entries | Where-Object {
      $_.relativePath -cne $stageName -and -not $_.relativePath.StartsWith("$stageName/", [StringComparison]::Ordinal)
    })
    $calculatedAfter = [ordered]@{ schema='applypilot.phase-a.directory-manifest.v1'; entries=$calculatedEntries }
    $calculatedBytes = & $module { param($V) ConvertTo-PhaseACanonicalJsonBytes $V } $calculatedAfter
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($calculatedBytes, $expected.Bytes)) {
      throw 'Expected-after manifest removes more or less than the exact staging identity.'
    }
    $targetBinding = Get-PhaseATargetDigest $stage -PassThru
    if ($targetBinding.Digest -cne $ExpectedTargetDigest) { throw 'Authorized staging target identity changed.' }
    $identity = $targetBinding.Identity
      if ($BeforeCleanupDelete) { & $BeforeCleanupDelete $stage }
      $null = [ApplyPilot.PhaseA.EvidenceNative]::DeleteTreeNoFollow(
        $stage, [uint64]$identity.VolumeSerialNumber, [string]$identity.FileId, $CrashAfterEntries)
    } elseif (Test-Path -LiteralPath $stage) {
      throw 'Cleanup target changed from a directory.'
    }
    $actualAfter = Get-PhaseAManifestDigest $parent
    if ($actualAfter.Sha256 -cne $expected.Sha256) { throw 'Cleanup result does not match the authorized after manifest.' }
    if ($CrashAfterMutation) { throw 'Injected cleanup crash after mutation.' }
    $completionValidation = @{} + $commonValidation
    $completionValidation.ExpectedReceiptType = 'operation-completion'
    $null = & $module { param($Pair,$Arguments)
      $Arguments.Receipt=$Pair.Receipt; $Arguments.SignatureRead=$Pair.Signature
      $result=Test-PhaseASignedReceiptCore @Arguments
      Assert-PhaseAProtectedReceiptPairIdentity $Pair
      return $result
    } $completionPair $completionValidation
  } finally {
    & $module { param($Pair) Close-PhaseAProtectedReceiptPair $Pair } $completionPair
    & $module { param($Pair) Close-PhaseAProtectedReceiptPair $Pair } $authorizationPair
  }
  $completionRoot = $bootstrap
  $install = @{
    ReceiptPath=$CompletionReceiptPath; SignaturePath=$CompletionSignaturePath; StoreRoot=$completionRoot;
    SigningSpkiPath=$RecoverySigningSpkiPath; ExpectedSigningSpkiSha256=$RecoverySigningSpkiSha256;
    ExpectedReceiptType='operation-completion'; ExpectedCommit=$ExpectedCommit; ExpectedOperationId=$ExpectedOperationId;
    ExpectedTargetDigest=$ExpectedTargetDigest; ExpectedOperatorSidDigest=$ExpectedOperatorSidDigest;
    ExpectedMachineDigest=$ExpectedMachineDigest; ExpectedStoreConfigSha256=$ExpectedStoreConfigSha256;
    ExpectedHostProvisioningReceiptSha256=$ExpectedHostProvisioningReceiptSha256;
    ExpectedSourceApprovalReceiptSha256=$ExpectedSourceApprovalReceiptSha256;
    ExpectedManifestBeforeSha256=$ExpectedManifestBeforeSha256; ExpectedManifestAfterSha256=$ExpectedManifestAfterSha256
  }
  $install.Bootstrap=$true
  if ($TestBootstrapRoot) { $install.DefinitionImport=$true }
  $installed = Install-PhaseASignedReceipt @install
  return [pscustomobject]@{ RemovedTargetDigest=$ExpectedTargetDigest; OperationId=$ExpectedOperationId; ManifestAfterSha256=$actualAfter.Sha256; CompletionReceiptPath=$installed.ReceiptPath }
}

if ($PSCmdlet.ParameterSetName -eq 'DefinitionImport') { return }

$module = Get-Module PhaseAEvidenceStore
$production = & $module {
  [pscustomobject]@{
    StoreRoot=$script:ProductionStoreRoot
    SigningSpkiPath=$script:ProductionSigningSpkiPath
    RecoverySigningSpkiPath=$script:ProductionRecoverySpkiPath
    SigningSpkiSha256=$script:ProductionSigningSpkiSha256
    RecoverySigningSpkiSha256=$script:ProductionRecoverySpkiSha256
  }
}
Invoke-PhaseAEvidenceStoreProvision -StoreRoot $production.StoreRoot `
  -CanonicalOperatorSid $CanonicalOperatorSid -ExpectedCommit $ExpectedCommit `
  -SigningSpkiPath $production.SigningSpkiPath -RecoverySigningSpkiPath $production.RecoverySigningSpkiPath `
  -SigningSpkiSha256 $production.SigningSpkiSha256 -RecoverySigningSpkiSha256 $production.RecoverySigningSpkiSha256 `
  -CustodyReceiptPath $CustodyReceiptPath -CustodySignaturePath $CustodySignaturePath `
  -CustodyOperationId $CustodyOperationId -CustodyManifestBeforeSha256 $CustodyManifestBeforeSha256 `
  -CustodyManifestAfterSha256 $CustodyManifestAfterSha256
