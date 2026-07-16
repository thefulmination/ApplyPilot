[CmdletBinding(DefaultParameterSetName='Provision')]
param(
  [Parameter(ParameterSetName='DefinitionImport',Mandatory)][switch]$DefinitionImport,
  [Parameter(ParameterSetName='Provision',Mandatory)][string]$CanonicalOperatorSid,
  [Parameter(ParameterSetName='Provision',Mandatory)][string]$ExpectedCommit,
  [Parameter(ParameterSetName='Provision',Mandatory)][string]$ExpectedReceiptBindingsPath,
  [Parameter(ParameterSetName='Provision')][string]$SourceApprovalReceiptPath,
  [Parameter(ParameterSetName='Provision')][string]$SourceApprovalSignaturePath,
  [Parameter(ParameterSetName='Provision')][string]$HostProvisioningReceiptPath,
  [Parameter(ParameterSetName='Provision')][string]$HostProvisioningSignaturePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSScriptRoot 'PhaseAEvidenceStore.psm1') -Force

function Test-PhaseAElevatedToken {
  $principal=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-PhaseAProtectedFile([string]$Path,[byte[]]$Bytes,[string]$OperatorSid) {
  $module=Get-Module PhaseAEvidenceStore
  $full=& $module {param($p)Assert-PhaseALocalNtfsPath $p -AllowMissingLeaf} $Path
  $stream=[IO.FileStream]::new($full,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
  try{$stream.Write($Bytes);$stream.Flush($true)}finally{$stream.Dispose()}
  & $module {param($p,$s)Set-PhaseAProtectedAcl $p $s -File} $full $OperatorSid
}

function Get-PhaseAManifestMaterial([string]$Path,[string]$CanonicalRootPath) {
  $value=Get-PhaseADirectoryManifest $Path -CanonicalRootPath $CanonicalRootPath
  $module=Get-Module PhaseAEvidenceStore
  $bytes=& $module {param($v)ConvertTo-PhaseACanonicalJsonBytes $v} $value
  [pscustomobject]@{Value=$value;Bytes=$bytes;Sha256=(& $module {param($b)Get-PhaseASha256 $b} $bytes)}
}

function Get-PhaseANativeWin32ErrorCode([object]$InputObject) {
  $pending=[Collections.Generic.Stack[object]]::new()
  $seen=[Collections.Generic.List[object]]::new()
  if($null-ne $InputObject){$pending.Push($InputObject)}
  while($pending.Count-gt 0){
    $current=$pending.Pop()
    $alreadySeen=$false
    foreach($candidate in $seen){
      if([object]::ReferenceEquals($candidate,$current)){$alreadySeen=$true;break}
    }
    if($alreadySeen){continue}
    $seen.Add($current)
    if($current-is [ComponentModel.Win32Exception]){return [int]$current.NativeErrorCode}
    if($current-is [Management.Automation.ErrorRecord]-and $null-ne $current.Exception){
      $pending.Push($current.Exception)
    }
    if($current-is [Exception]-and $null-ne $current.InnerException){
      $pending.Push($current.InnerException)
    }
    if($current-is [Management.Automation.IContainsErrorRecord]){
      $record=$current.ErrorRecord
      if($null-ne $record){$pending.Push($record)}
    }
  }
  $null
}

function Assert-PhaseACleanupStagingPathAbsent([string]$StagingPath) {
  $handle=$null
  try {
    try{$handle=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($StagingPath,$true)}
    catch {
      $code=Get-PhaseANativeWin32ErrorCode $_
      if($code-eq 2-or $code-eq 3){return}
      throw
    }
  } finally {if($null-ne $handle){$handle.Dispose()}}
  throw 'Cleanup staging path reappeared during post-mutation verification.'
}

function Get-PhaseAPostCleanupManifestMaterial {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ParentPath,[Parameter(Mandatory)][string]$StagingPath,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256,
    [scriptblock]$TestManifestReader,[scriptblock]$TestElapsedMillisecondsProvider,
    [switch]$DefinitionImport
  )
  if($TestManifestReader-and-not $DefinitionImport){throw 'Post-cleanup manifest override requires DefinitionImport.'}
  if($TestElapsedMillisecondsProvider-and-not $DefinitionImport){throw 'Post-cleanup clock override requires DefinitionImport.'}
  $maxAttempts=4;$maxElapsedMilliseconds=750;$retryDelayMilliseconds=20
  $timer=[Diagnostics.Stopwatch]::StartNew()
  $getElapsedMilliseconds={
    if($TestElapsedMillisecondsProvider){[int64](& $TestElapsedMillisecondsProvider)}
    else{[int64]$timer.ElapsedMilliseconds}
  }
  for($attempt=1;$attempt-le $maxAttempts;$attempt++){
    Assert-PhaseACleanupStagingPathAbsent $StagingPath
    try {
      $material=if($TestManifestReader){& $TestManifestReader $ParentPath $null}else{Get-PhaseAManifestMaterial $ParentPath}
    } catch {
      $failure=$_
      Assert-PhaseACleanupStagingPathAbsent $StagingPath
      if(([int64](& $getElapsedMilliseconds))-ge $maxElapsedMilliseconds){
        throw [InvalidOperationException]::new('Post-cleanup parent manifest deadline exceeded.',$failure.Exception)
      }
      $code=Get-PhaseANativeWin32ErrorCode $failure
      if($code-ne 2-and $code-ne 3){throw}
      if($attempt-ge $maxAttempts){
        throw [InvalidOperationException]::new('Post-cleanup parent manifest retries exhausted.',$failure.Exception)
      }
      $remaining=[Math]::Max([int64]0,$maxElapsedMilliseconds-([int64](& $getElapsedMilliseconds)))
      if($remaining-le 0){
        throw [InvalidOperationException]::new('Post-cleanup parent manifest retries exhausted.',$failure.Exception)
      }
      $sleepMilliseconds=[int][Math]::Min($retryDelayMilliseconds,$remaining)
      if($sleepMilliseconds-le 0){
        throw [InvalidOperationException]::new('Post-cleanup parent manifest retries exhausted.',$failure.Exception)
      }
      Start-Sleep -Milliseconds $sleepMilliseconds
      if(([int64](& $getElapsedMilliseconds))-ge $maxElapsedMilliseconds){
        throw [InvalidOperationException]::new('Post-cleanup parent manifest retries exhausted.',$failure.Exception)
      }
      continue
    }
    Assert-PhaseACleanupStagingPathAbsent $StagingPath
    if($material.Sha256-cne $ExpectedSha256){throw 'Actual-after manifest differs from authorization.'}
    if(([int64](& $getElapsedMilliseconds))-ge $maxElapsedMilliseconds){
      throw [InvalidOperationException]::new('Post-cleanup parent manifest deadline exceeded.')
    }
    return $material
  }
}

function Invoke-PhaseAEvidenceStoreProvision {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][Collections.IDictionary]$ExpectedReceiptBindingsByHash,
    [Parameter(Mandatory)][string]$OperatorSigningMetadataPath,
    [Parameter(Mandatory)][string]$OperatorSigningSpkiPath,
    [Parameter(Mandatory)][string]$RecoveryEncryptionMetadataPath,
    [Parameter(Mandatory)][string]$RecoveryEncryptionSpkiPath,
    [string]$SourceApprovalReceiptPath,[string]$SourceApprovalSignaturePath,
    [string]$HostProvisioningReceiptPath,[string]$HostProvisioningSignaturePath,
    [scriptblock]$HostReceiptMaterializer,[scriptblock]$DefinitionBundleAuthenticator,
    [string]$TestMachineGuid,[string]$TestSmbiosUuid,[string]$TestAncestorBoundary,
    [scriptblock]$BeforeStageValidation,[scriptblock]$BeforePublication,
    [switch]$CrashBeforePublication,[switch]$DefinitionImport
  )
  if(-not $DefinitionImport -and ($HostReceiptMaterializer-or $DefinitionBundleAuthenticator-or $TestMachineGuid-or $TestSmbiosUuid-or $TestAncestorBoundary-or $BeforeStageValidation-or $BeforePublication-or $CrashBeforePublication)){
    throw 'Provisioning overrides require DefinitionImport.'
  }
  if(-not $DefinitionImport -and -not (Test-PhaseAElevatedToken)){throw 'Evidence-store provisioning requires elevation.'}
  $module=Get-Module PhaseAEvidenceStore
  $operator=& $module {param($s)Assert-PhaseACurrentOperator $s} $CanonicalOperatorSid
  $final=& $module {param($p)Assert-PhaseALocalNtfsPath $p -AllowMissingLeaf} $StoreRoot
  $production=& $module {$script:ProductionStoreRoot}
  if(-not $DefinitionImport -and $final-ine $production){throw 'Production root is fixed to native ProgramData\ApplyPilot\Evidence\v1.'}
  $anchorArgs=@{OperatorSigningMetadataPath=$OperatorSigningMetadataPath;OperatorSigningSpkiPath=$OperatorSigningSpkiPath;
    RecoveryEncryptionMetadataPath=$RecoveryEncryptionMetadataPath;RecoveryEncryptionSpkiPath=$RecoveryEncryptionSpkiPath}
  if($DefinitionImport){$anchorArgs.DefinitionImport=$true}
  $anchors=Get-PhaseAProductionAnchors @anchorArgs
  $machine=if($TestMachineGuid-and $TestSmbiosUuid){Get-PhaseAMachineDigest -MachineGuid $TestMachineGuid -SmbiosUuid $TestSmbiosUuid -DefinitionImport}
    elseif($TestMachineGuid-or $TestSmbiosUuid){throw 'Both test machine identity values are required.'}else{Get-PhaseAMachineDigest}
  $validate=@{CanonicalOperatorSid=$operator;ExpectedCommit=$ExpectedCommit;ExpectedReceiptBindingsByHash=$ExpectedReceiptBindingsByHash;
    OperatorSigningMetadataPath=$OperatorSigningMetadataPath;OperatorSigningSpkiPath=$OperatorSigningSpkiPath;
    RecoveryEncryptionMetadataPath=$RecoveryEncryptionMetadataPath;RecoveryEncryptionSpkiPath=$RecoveryEncryptionSpkiPath}
  if($DefinitionImport){$validate.DefinitionImport=$true;$validate.ExpectedMachineIdentityDigest=$machine;if($TestAncestorBoundary){$validate.AncestorBoundary=$TestAncestorBoundary};if($DefinitionBundleAuthenticator){$validate.DefinitionBundleAuthenticator=$DefinitionBundleAuthenticator}}
  if(Test-Path -LiteralPath $final){return Assert-PhaseAEvidenceStore -StoreRoot $final @validate}
  $parent=Split-Path -Parent $final
  if(-not (Test-Path -LiteralPath $parent -PathType Container)){[IO.Directory]::CreateDirectory($parent)|Out-Null}
  & $module {param($p,$s,$b)Assert-PhaseAAncestorDeleteChild $p $s $b} $final $operator $TestAncestorBoundary
  $stage=Join-Path $parent ".provisioning-$([guid]::NewGuid().ToString('D'))"
  $sd=& $module {param($s)Get-PhaseAProtectedSecurityDescriptorBytes $s} $operator
  $stageHandle=[ApplyPilot.PhaseA.EvidenceNative]::CreateProtectedDirectory($stage,$sd)
  try {
    foreach($leaf in @('bundles','adjudications','operations')){
      $child=$null
      try{$child=[ApplyPilot.PhaseA.EvidenceNative]::CreateProtectedDirectory((Join-Path $stage $leaf),$sd)}finally{if($child){$child.Dispose()}}
    }
    if(-not $SourceApprovalReceiptPath-or-not $SourceApprovalSignaturePath){throw 'Provisioning requires a complete source-approval pair.'}
    $source=& $module {param($p)Read-PhaseACanonicalJson $p} $SourceApprovalReceiptPath
    if(-not $ExpectedReceiptBindingsByHash.Contains($source.Sha256)){throw 'Source approval is not caller-authorized.'}
    $null=Install-PhaseASignedReceipt -ReceiptPath $SourceApprovalReceiptPath -SignaturePath $SourceApprovalSignaturePath `
      -StoreRoot $stage -OperatorSigningSpkiPath $anchors.OperatorSigning.SpkiPath `
      -ExpectedOperatorSigningKeySpkiSha256 $anchors.OperatorSigning.SpkiSha256 `
      -ExpectedReceiptType applypilot.phase-a.runtime-source-approval -ExpectedBindings $ExpectedReceiptBindingsByHash[$source.Sha256] `
      -DefinitionImport:$DefinitionImport
    $target=Get-PhaseATargetDigest -Path $stage -CanonicalPath $final
    $config=[ordered]@{schemaVersion=1;storeType='applypilot.phase-a.evidence-store';approvedCommit=$ExpectedCommit;
      targetIdentityDigest=$target;operatorSidDigest=(Get-PhaseAOperatorSidDigest $operator);machineIdentityDigest=$machine;
      securityDescriptorSha256=(Get-PhaseASecurityDescriptorHash $stage);operatorSigningKeySpkiSha256=$anchors.OperatorSigning.SpkiSha256;
      recoveryKeySpkiSha256=$anchors.RecoveryEncryption.SpkiSha256;sourceApprovalReceiptSha256=$source.Sha256}
    $configBytes=& $module {param($v)ConvertTo-PhaseACanonicalJsonBytes $v} $config
    Write-PhaseAProtectedFile (Join-Path $stage 'store.json') $configBytes $operator
    $configHash=& $module {param($b)Get-PhaseASha256 $b} $configBytes
    $tree=Get-PhaseAManifestMaterial $stage $final
    if($HostReceiptMaterializer){
      $material=& $HostReceiptMaterializer ([pscustomobject]@{StoreRoot=$stage;ApprovedCommit=$ExpectedCommit;
        SourceApprovalReceiptSha256=$source.Sha256;OperatorSigningKeySpkiSha256=$anchors.OperatorSigning.SpkiSha256;
        MachineIdentityDigest=$machine;StoreConfigSha256=$configHash;StoreTreeManifestSha256=$tree.Sha256;
        RecoveryKeySpkiSha256=$anchors.RecoveryEncryption.SpkiSha256;OperatorSidDigest=(Get-PhaseAOperatorSidDigest $operator)})
      $HostProvisioningReceiptPath=[string]$material.ReceiptPath;$HostProvisioningSignaturePath=[string]$material.SignaturePath
    }
    if(-not $HostProvisioningReceiptPath-or-not $HostProvisioningSignaturePath){throw 'Provisioning requires a complete host-provisioning pair.'}
    $hostReceipt=& $module {param($p)Read-PhaseACanonicalJson $p} $HostProvisioningReceiptPath
    if(-not $ExpectedReceiptBindingsByHash.Contains($hostReceipt.Sha256)){throw 'Host provisioning is not caller-authorized.'}
    $null=Install-PhaseASignedReceipt -ReceiptPath $HostProvisioningReceiptPath -SignaturePath $HostProvisioningSignaturePath `
      -StoreRoot $stage -OperatorSigningSpkiPath $anchors.OperatorSigning.SpkiPath `
      -ExpectedOperatorSigningKeySpkiSha256 $anchors.OperatorSigning.SpkiSha256 `
      -ExpectedReceiptType applypilot.phase-a.host-provisioning -ExpectedBindings $ExpectedReceiptBindingsByHash[$hostReceipt.Sha256] `
      -DefinitionImport:$DefinitionImport
    $stageIdentity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($stageHandle)
    if($BeforeStageValidation){& $BeforeStageValidation $stage $stageHandle}
    $validate.ExpectedTargetIdentityDigest=$target;$validate.CanonicalStoreRoot=$final
    $null=Assert-PhaseAEvidenceStore -StoreRoot $stage @validate
    [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($stageHandle,$stageIdentity)
    if($CrashBeforePublication){throw 'Injected crash before evidence-store publication.'}
    if($BeforePublication){& $BeforePublication $stage $stageHandle}
    [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($stageHandle,$stageIdentity)
    [ApplyPilot.PhaseA.EvidenceNative]::RenameDirectoryHandleNoReplace($stageHandle,$final)
    $stageIdentity.FinalPath=$final
    [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($stageHandle,$stageIdentity)
  } finally {$stageHandle.Dispose()}
  $null=$validate.Remove('ExpectedTargetIdentityDigest')
  $null=$validate.Remove('CanonicalStoreRoot')
  Assert-PhaseAEvidenceStore -StoreRoot $final @validate
}

function Invoke-PhaseAProvisioningCleanup {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StagingPath,[Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$OperatorSigningSpkiPath,[Parameter(Mandatory)][string]$ExpectedOperatorSigningKeySpkiSha256,
    [Parameter(Mandatory)][string]$ExpectedCommit,[Parameter(Mandatory)][string]$AuthorizationReceiptPath,
    [Parameter(Mandatory)][string]$AuthorizationSignaturePath,[Parameter(Mandatory)]$ExpectedAuthorizationBindings,
    [Parameter(Mandatory)][string]$ExpectedAfterManifestPath,[string]$CompletionReceiptPath,[string]$CompletionSignaturePath,
    [string]$CompletionRequestPath,[string]$TestBootstrapRoot,[scriptblock]$TestPostCleanupManifestReader,
    [switch]$DefinitionImport,[switch]$CrashAfterMutation
  )
  if($TestPostCleanupManifestReader-and-not $DefinitionImport){throw 'Post-cleanup manifest override requires DefinitionImport.'}
  if(-not $DefinitionImport -and -not (Test-PhaseAElevatedToken)){throw 'Staging cleanup requires elevation.'}
  if($TestBootstrapRoot-and-not $DefinitionImport){throw 'Bootstrap override requires DefinitionImport.'}
  $module=Get-Module PhaseAEvidenceStore;$operator=& $module {param($s)Assert-PhaseACurrentOperator $s} $CanonicalOperatorSid
  $stage=& $module {param($p)Assert-PhaseALocalNtfsPath $p -AllowMissingLeaf} $StagingPath
  if([IO.Path]::GetFileName($stage)-cnotmatch '^\.provisioning-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'){throw 'Cleanup target name is invalid.'}
  $parent=Split-Path -Parent $stage
  $bootstrap=if($TestBootstrapRoot){& $module {param($p)Assert-PhaseALocalNtfsPath $p} $TestBootstrapRoot}else{[IO.Path]::Combine([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData),'ApplyPilot','phase-a-evidence','bootstrap-operations')}
  & $module {param($p,$s)Assert-PhaseAProtectedAcl $p $s} $bootstrap $operator
  foreach($path in @($AuthorizationReceiptPath,$AuthorizationSignaturePath)){
    $validated=& $module {param($p)Assert-PhaseALocalNtfsPath $p} $path
    if((Split-Path -Parent $validated)-ine $bootstrap){throw 'Authorization pair is outside bootstrap root.'}
  }
  $pair=& $module {param($r,$s,$o)Open-PhaseAProtectedReceiptPair $r $s $o} $AuthorizationReceiptPath $AuthorizationSignaturePath $operator
  try {
    $null=& $module {param($pair,$key,$hash,$expected)Test-PhaseASignedReceiptCore -Receipt $pair.Receipt -SignatureRead $pair.Signature `
      -OperatorSigningSpkiPath $key -ExpectedOperatorSigningKeySpkiSha256 $hash `
      -ExpectedReceiptType applypilot.phase-a.provisioning-cleanup-authorization -ExpectedBindings $expected} `
      $pair $OperatorSigningSpkiPath $ExpectedOperatorSigningKeySpkiSha256 $ExpectedAuthorizationBindings
    & $module {param($p)Assert-PhaseAProtectedReceiptPairIdentity $p} $pair
    $auth=$pair.Receipt.Value
    if($auth.approvedCommit-cne $ExpectedCommit-or $auth.operatorSid-cne $operator){throw 'Cleanup authority bindings are wrong.'}
    $expected=& $module {param($p)Read-PhaseACanonicalJson $p} $ExpectedAfterManifestPath
    if($expected.Sha256-cne $auth.expectedAfterManifestSha256){throw 'Expected-after manifest is not authorized.'}
    if(Test-Path -LiteralPath $stage -PathType Container){
      if($CompletionReceiptPath-or$CompletionSignaturePath-or$CompletionRequestPath){throw 'Completion cannot predate mutation.'}
      $before=Get-PhaseAManifestMaterial $parent
      if($before.Sha256-cne $auth.beforeManifestSha256){throw 'Before manifest is not authorized.'}
      $target=Get-PhaseATargetDigest $stage -PassThru
      if($target.Digest-cne $auth.targetIdentityDigest){throw 'Staging identity is not authorized.'}
      $stageTree=Get-PhaseADirectoryManifest $stage
      $remove=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal);$null=$remove.Add($stageTree.baseRootIdentityDigest)
      foreach($entry in $stageTree.entries){$null=$remove.Add([string]$entry.objectIdentityDigest)}
      $remaining=@($before.Value.entries|Where-Object{-not $remove.Contains([string]$_.objectIdentityDigest)})
      $calculated=[ordered]@{schemaVersion=1;manifestType='applypilot.phase-a.directory-manifest';baseRootIdentityDigest=$before.Value.baseRootIdentityDigest;entries=$remaining}
      $calculatedBytes=& $module {param($v)ConvertTo-PhaseACanonicalJsonBytes $v} $calculated
      if(-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($calculatedBytes,$expected.Bytes)){throw 'Expected-after manifest is not the exact subtraction.'}
      $null=[ApplyPilot.PhaseA.EvidenceNative]::DeleteTreeNoFollow($stage,[uint64]$target.Identity.VolumeSerialNumber,[string]$target.Identity.FileId,-1)
      if($CrashAfterMutation){throw 'Injected cleanup crash after mutation.'}
    }elseif(Test-Path -LiteralPath $stage){throw 'Cleanup target changed kind.'}
    $postManifestArgs=@{ParentPath=$parent;StagingPath=$stage;ExpectedSha256=$auth.expectedAfterManifestSha256}
    if($TestPostCleanupManifestReader){$postManifestArgs.TestManifestReader=$TestPostCleanupManifestReader;$postManifestArgs.DefinitionImport=$true}
    $actual=Get-PhaseAPostCleanupManifestMaterial @postManifestArgs
    if($actual.Sha256-cne $auth.expectedAfterManifestSha256){throw 'Actual-after manifest differs from authorization.'}
    if(-not $CompletionReceiptPath-and-not $CompletionSignaturePath){
      $requests=Join-Path $bootstrap 'completion-requests'
      if(-not(Test-Path -LiteralPath $requests)){[IO.Directory]::CreateDirectory($requests)|Out-Null;& $module {param($p,$s)Set-PhaseAProtectedAcl $p $s} $requests $operator}
      else{& $module {param($p,$s)Assert-PhaseAProtectedAcl $p $s} $requests $operator}
      $operationRequests=Join-Path $requests ([string]$auth.operationId)
      if(-not(Test-Path -LiteralPath $operationRequests)){[IO.Directory]::CreateDirectory($operationRequests)|Out-Null;& $module {param($p,$s)Set-PhaseAProtectedAcl $p $s} $operationRequests $operator}
      else{& $module {param($p,$s)Assert-PhaseAProtectedAcl $p $s} $operationRequests $operator}
      $existing=@(Get-ChildItem -LiteralPath $operationRequests -File -Filter '*.json')
      if($existing.Count -gt 1){throw 'Multiple unsigned completion requests exist for cleanup resume.'}
      if($existing.Count -eq 1){
        & $module {param($p,$s)Assert-PhaseAProtectedAcl $p $s -File} $existing[0].FullName $operator
        $requestRead=& $module {param($p)Read-PhaseACanonicalJson $p} $existing[0].FullName
        $requestValue=$requestRead.Value
        & $module {param($v)Assert-PhaseAClosedFields $v @('schemaVersion','receiptType','approvedCommit','operatorSigningKeySpkiSha256','operationId','authorizationReceiptSha256','actualAfterManifestSha256','result','createdAtUtc')} $requestValue
        if($requestRead.Sha256-cne $existing[0].BaseName -or $requestValue.schemaVersion-ne 1 -or
            $requestValue.receiptType-cne 'applypilot.phase-a.provisioning-cleanup-completion' -or
            $requestValue.approvedCommit-cne $ExpectedCommit -or $requestValue.operatorSigningKeySpkiSha256-cne $ExpectedOperatorSigningKeySpkiSha256 -or
            $requestValue.operationId-cne $auth.operationId -or $requestValue.authorizationReceiptSha256-cne $pair.Receipt.Sha256 -or
            $requestValue.actualAfterManifestSha256-cne $actual.Sha256 -or $requestValue.result-cne 'COMPLETE'){
          throw 'Existing unsigned completion request does not match the completed mutation.'
        }
        return [pscustomobject]@{State='COMPLETION_REQUIRED';CompletionRequestPath=$existing[0].FullName;ActualAfterManifestSha256=$actual.Sha256}
      }
      $created=[DateTimeOffset]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'",[Globalization.CultureInfo]::InvariantCulture)
      $request=& (Join-Path $PSScriptRoot 'New-PhaseASignedReceipt.ps1') -ReceiptType applypilot.phase-a.provisioning-cleanup-completion `
        -OperatorSigningSpkiPath $OperatorSigningSpkiPath -ExpectedOperatorSigningKeySpkiSha256 $ExpectedOperatorSigningKeySpkiSha256 `
        -ApprovedCommit $ExpectedCommit -OperationId $auth.operationId -AuthorizationReceiptSha256 $pair.Receipt.Sha256 `
        -ActualAfterManifestSha256 $actual.Sha256 -ExpectedAfterManifestSha256 $auth.expectedAfterManifestSha256 `
        -CreatedAtUtc $created -CreateUnsigned -OutputDirectory $operationRequests -ProtectedOperatorSid $operator
      return [pscustomobject]@{State='COMPLETION_REQUIRED';CompletionRequestPath=$request;ActualAfterManifestSha256=$actual.Sha256}
    }
    if(-not $CompletionReceiptPath-or-not $CompletionSignaturePath-or-not $CompletionRequestPath){throw 'Resume requires the request and complete signed pair.'}
    $expectedRequestDirectory=[IO.Path]::Combine($bootstrap,'completion-requests',[string]$auth.operationId)
    $validatedRequest=& $module {param($p)Assert-PhaseALocalNtfsPath $p} $CompletionRequestPath
    if((Split-Path -Parent $validatedRequest)-ine $expectedRequestDirectory){throw 'Completion request is not keyed to the authorized operationId.'}
    & $module {param($p,$s)Assert-PhaseAProtectedAcl $p $s -File} $CompletionRequestPath $operator
    $request=& $module {param($p)Read-PhaseACanonicalJson $p} $CompletionRequestPath
    $installed=Install-PhaseASignedReceipt -ReceiptPath $CompletionReceiptPath -SignaturePath $CompletionSignaturePath `
      -StoreRoot $bootstrap -OperatorSigningSpkiPath $OperatorSigningSpkiPath `
      -ExpectedOperatorSigningKeySpkiSha256 $ExpectedOperatorSigningKeySpkiSha256 `
      -ExpectedReceiptType applypilot.phase-a.provisioning-cleanup-completion -ExpectedBindings $request.Value `
      -ExpectedAuthorizedAfterManifestSha256 $auth.expectedAfterManifestSha256 -Bootstrap -DefinitionImport:$DefinitionImport
    [pscustomobject]@{State='COMPLETE';CompletionReceiptPath=$installed.ReceiptPath;ActualAfterManifestSha256=$actual.Sha256}
  } finally {& $module {param($p)Close-PhaseAProtectedReceiptPair $p} $pair}
}

if($PSCmdlet.ParameterSetName-eq 'DefinitionImport'){return}
$module=Get-Module PhaseAEvidenceStore
$production=& $module {[pscustomobject]@{StoreRoot=$script:ProductionStoreRoot;OperatorSigningMetadataPath=$script:ProductionOperatorSigningMetadataPath;
  OperatorSigningSpkiPath=$script:ProductionOperatorSigningSpkiPath;RecoveryEncryptionMetadataPath=$script:ProductionRecoveryEncryptionMetadataPath;
  RecoveryEncryptionSpkiPath=$script:ProductionRecoveryEncryptionSpkiPath}}
$bindings=(& $module {param($p)Read-PhaseACanonicalJson $p} $ExpectedReceiptBindingsPath).Value
Invoke-PhaseAEvidenceStoreProvision -StoreRoot $production.StoreRoot -CanonicalOperatorSid $CanonicalOperatorSid `
  -ExpectedCommit $ExpectedCommit -ExpectedReceiptBindingsByHash $bindings `
  -OperatorSigningMetadataPath $production.OperatorSigningMetadataPath -OperatorSigningSpkiPath $production.OperatorSigningSpkiPath `
  -RecoveryEncryptionMetadataPath $production.RecoveryEncryptionMetadataPath -RecoveryEncryptionSpkiPath $production.RecoveryEncryptionSpkiPath `
  -SourceApprovalReceiptPath $SourceApprovalReceiptPath -SourceApprovalSignaturePath $SourceApprovalSignaturePath `
  -HostProvisioningReceiptPath $HostProvisioningReceiptPath -HostProvisioningSignaturePath $HostProvisioningSignaturePath
