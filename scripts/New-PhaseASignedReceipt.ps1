param(
  [Parameter(Mandatory)][ValidateSet(
    'applypilot.phase-a.runtime-source-approval',
    'applypilot.phase-a.evidence-adjudication',
    'applypilot.phase-a.credential-revocation',
    'applypilot.phase-a.provisioning-cleanup-authorization',
    'applypilot.phase-a.legacy-sidecar-destruction-authorization',
    'applypilot.phase-a.provisioning-cleanup-completion',
    'applypilot.phase-a.legacy-sidecar-destruction-completion',
    'applypilot.phase-a.host-provisioning')][string]$ReceiptType,
  [Parameter(Mandatory)][string]$OperatorSigningSpkiPath,
  [Parameter(Mandatory)][string]$ExpectedOperatorSigningKeySpkiSha256,
  [string]$ApprovedCommit,
  [string]$ApprovedTree,
  [string]$PlanSha256,
  [string]$SpecReviewTaskId,
  [string]$QualityReviewTaskId,
  [Collections.IDictionary]$CriticalFileSha256,
  [string]$SourceIdentityDigest,
  [string]$SelectedBundleSha256,
  [string]$StoreRoot,
  [string]$CanonicalOperatorSid,
  [scriptblock]$DefinitionBundleAuthenticator,
  [switch]$DefinitionImport,
  [string]$CredentialReferenceDigest,
  [string]$ProviderClass,
  [string]$RevokedAtUtc,
  [string]$StaleProbeAtUtc,
  [string]$ProviderEvidenceSha256,
  [string]$MachineIdentityDigest,
  [string]$OperationId,
  [string]$TargetIdentityDigest,
  [string]$BeforeManifestSha256,
  [string]$ExpectedAfterManifestSha256,
  [string]$EvidenceBundleSha256,
  [string]$CredentialInventoryRoot,
  [string]$CredentialRevocationSetRoot,
  [string]$OperatorSid,
  [string]$AuthorizationReceiptSha256,
  [string]$ActualAfterManifestSha256,
  [string]$SourceApprovalReceiptSha256,
  [string]$StoreConfigSha256,
  [string]$StoreTreeManifestSha256,
  [string]$RecoveryKeySpkiSha256,
  [string]$OperatorSidDigest,
  [string]$Nonce,
  [string]$CreatedAtUtc,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][switch]$CreateUnsigned,
  [Parameter(Mandatory, ParameterSetName='Unsigned')][string]$OutputDirectory,
  [Parameter(ParameterSetName='Unsigned')][string]$ProtectedOperatorSid,
  [Parameter(Mandatory, ParameterSetName='Verify')][switch]$VerifyReturnedSignature,
  [Parameter(Mandatory, ParameterSetName='Verify')][string]$ReceiptPath,
  [Parameter(Mandatory, ParameterSetName='Verify')][string]$SignaturePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PhaseAEvidenceStore.psm1') -Force
$module = Get-Module PhaseAEvidenceStore

$value = & $module {
  param($P)
  Assert-PhaseAHexDigest $P.ExpectedOperatorSigningKeySpkiSha256 'Operator signing key hash'
  $spki = Read-PhaseAValidatedBytes $P.OperatorSigningSpkiPath
  $rsa = Import-PhaseAOperatorSigningSpkiBytes $spki.Bytes $P.ExpectedOperatorSigningKeySpkiSha256
  $rsa.Dispose()
  if ($P.ContainsKey('CreatedAtUtc') -and $P.CreatedAtUtc) { Assert-PhaseAUtcSeconds $P.CreatedAtUtc 'createdAtUtc' }
  $zero = '0' * 64
  switch ($P.ReceiptType) {
    'applypilot.phase-a.runtime-source-approval' {
      if ($P.ApprovedCommit -cnotmatch $script:Hex40 -or $P.ApprovedTree -cnotmatch $script:Hex40) {
        throw 'Source approval commit and tree must be lowercase Git object IDs.'
      }
      Assert-PhaseAHexDigest $P.PlanSha256 'planSha256'
      Assert-PhaseAHexDigest $P.Nonce 'nonce'
      Assert-PhaseAUuid $P.SpecReviewTaskId 'specReview.taskId'
      Assert-PhaseAUuid $P.QualityReviewTaskId 'qualityReview.taskId'
      if ($null -eq $P.CriticalFileSha256 -or $P.CriticalFileSha256.Count -eq 0) {
        throw 'criticalFileSha256 must be a nonempty map.'
      }
      $critical = [ordered]@{}
      foreach ($name in @($P.CriticalFileSha256.Keys | Sort-Object -CaseSensitive)) {
        if ($name -cnotmatch '^[A-Za-z0-9._/-]+$' -or $name.StartsWith('/') -or
            $name.Contains('//') -or @($name.Split('/')) -contains '..') {
          throw 'Critical file names must be normalized repository-relative paths.'
        }
        Assert-PhaseAHexDigest ([string]$P.CriticalFileSha256[$name]) "criticalFileSha256.$name"
        $critical[$name] = [string]$P.CriticalFileSha256[$name]
      }
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; approvedCommit=$P.ApprovedCommit;
        approvedTree=$P.ApprovedTree; planSha256=$P.PlanSha256;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256;
        specReview=[ordered]@{ taskId=$P.SpecReviewTaskId; result='APPROVED' };
        qualityReview=[ordered]@{ taskId=$P.QualityReviewTaskId; result='APPROVED' };
        criticalFileSha256=$critical; nonce=$P.Nonce; createdAtUtc=$P.CreatedAtUtc
      }
    }
    'applypilot.phase-a.evidence-adjudication' {
      foreach ($item in @($P.SourceIdentityDigest,$P.SelectedBundleSha256,$P.Nonce)) {
        Assert-PhaseAHexDigest $item 'Adjudication digest'
      }
      if([string]::IsNullOrWhiteSpace($P.StoreRoot) -or [string]::IsNullOrWhiteSpace($P.CanonicalOperatorSid)) {
        throw 'Adjudication requires the authoritative evidence store and canonical operator SID.'
      }
      $candidateArguments=@{
        StoreRoot=$P.StoreRoot;CanonicalOperatorSid=$P.CanonicalOperatorSid;
        SourceIdentityDigest=$P.SourceIdentityDigest
      }
      if($P.DefinitionImport){$candidateArguments.DefinitionImport=$true;$candidateArguments.DefinitionBundleAuthenticator=$P.DefinitionBundleAuthenticator}
      elseif($P.DefinitionBundleAuthenticator){throw 'Bundle authenticator override requires DefinitionImport.'}
      $candidates = @(Get-PhaseAAuthenticatedBundleCandidates @candidateArguments)
      if ($candidates.Count -eq 0) { throw 'Adjudication candidates are required.' }
      foreach ($candidate in $candidates) { Assert-PhaseAHexDigest $candidate 'candidateBundleSha256' }
      $sorted = @($candidates | Sort-Object -CaseSensitive -Unique)
      if ($sorted.Count -ne $candidates.Count -or (Compare-Object $candidates $sorted -SyncWindow 0)) {
        throw 'Candidate bundle digests must be sorted and unique.'
      }
      if ($candidates -cnotcontains $P.SelectedBundleSha256) { throw 'Selected bundle is not a candidate.' }
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; sourceIdentityDigest=$P.SourceIdentityDigest;
        selectedBundleSha256=$P.SelectedBundleSha256; candidateBundleSha256=$candidates;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256;
        nonce=$P.Nonce; createdAtUtc=$P.CreatedAtUtc
      }
    }
    'applypilot.phase-a.credential-revocation' {
      if ($P.ApprovedCommit -cnotmatch $script:Hex40) { throw 'Revocation approvedCommit is invalid.' }
      foreach ($item in @($P.CredentialReferenceDigest,$P.ProviderEvidenceSha256,$P.MachineIdentityDigest,$P.Nonce)) {
        Assert-PhaseAHexDigest $item 'Credential revocation digest'
      }
      if ($P.ProviderClass -cnotin @('postgres','llm-api','review-api','other')) {
        throw 'providerClass is not supported.'
      }
      Assert-PhaseAUtcSeconds $P.RevokedAtUtc 'revokedAtUtc'
      Assert-PhaseAUtcSeconds $P.StaleProbeAtUtc 'staleProbeAtUtc'
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; approvedCommit=$P.ApprovedCommit;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256;
        credentialReferenceDigest=$P.CredentialReferenceDigest; providerClass=$P.ProviderClass;
        revokedAtUtc=$P.RevokedAtUtc; staleProbeAtUtc=$P.StaleProbeAtUtc; staleProbeResult='DENIED';
        providerEvidenceSha256=$P.ProviderEvidenceSha256; machineIdentityDigest=$P.MachineIdentityDigest;
        nonce=$P.Nonce
      }
    }
    { $_ -in @('applypilot.phase-a.provisioning-cleanup-authorization','applypilot.phase-a.legacy-sidecar-destruction-authorization') } {
      if ($P.ApprovedCommit -cnotmatch $script:Hex40) { throw 'Authorization approvedCommit is invalid.' }
      foreach ($item in @($P.OperationId,$P.TargetIdentityDigest,$P.BeforeManifestSha256,
          $P.ExpectedAfterManifestSha256,$P.EvidenceBundleSha256,$P.CredentialInventoryRoot,
          $P.CredentialRevocationSetRoot)) { Assert-PhaseAHexDigest $item 'Authorization digest' }
      Assert-PhaseASid $P.OperatorSid 'operatorSid'
      $cleanup = $P.ReceiptType -ceq 'applypilot.phase-a.provisioning-cleanup-authorization'
      if ($cleanup -and @($P.EvidenceBundleSha256,$P.CredentialInventoryRoot,$P.CredentialRevocationSetRoot | Where-Object { $_ -cne $zero }).Count) {
        throw 'Cleanup authorization non-applicable roots must be zero digests.'
      }
      if (-not $cleanup -and @($P.EvidenceBundleSha256,$P.CredentialInventoryRoot,$P.CredentialRevocationSetRoot | Where-Object { $_ -ceq $zero }).Count) {
        throw 'Legacy destruction authorization requires nonzero evidence and credential roots.'
      }
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; approvedCommit=$P.ApprovedCommit;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256; operationId=$P.OperationId;
        targetIdentityDigest=$P.TargetIdentityDigest; beforeManifestSha256=$P.BeforeManifestSha256;
        expectedAfterManifestSha256=$P.ExpectedAfterManifestSha256; evidenceBundleSha256=$P.EvidenceBundleSha256;
        credentialInventoryRoot=$P.CredentialInventoryRoot; credentialRevocationSetRoot=$P.CredentialRevocationSetRoot;
        operatorSid=$P.OperatorSid; createdAtUtc=$P.CreatedAtUtc
      }
    }
    { $_ -in @('applypilot.phase-a.provisioning-cleanup-completion','applypilot.phase-a.legacy-sidecar-destruction-completion') } {
      if ($P.ApprovedCommit -cnotmatch $script:Hex40) { throw 'Completion approvedCommit is invalid.' }
      foreach ($item in @($P.OperationId,$P.AuthorizationReceiptSha256,$P.ActualAfterManifestSha256)) {
        Assert-PhaseAHexDigest $item 'Completion digest'
      }
      Assert-PhaseAHexDigest $P.ExpectedAfterManifestSha256 'Authorized expected-after manifest'
      if ($P.ActualAfterManifestSha256 -cne $P.ExpectedAfterManifestSha256) {
        throw 'Completion actual-after must equal the authorized expected-after manifest.'
      }
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; approvedCommit=$P.ApprovedCommit;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256; operationId=$P.OperationId;
        authorizationReceiptSha256=$P.AuthorizationReceiptSha256;
        actualAfterManifestSha256=$P.ActualAfterManifestSha256; result='COMPLETE'; createdAtUtc=$P.CreatedAtUtc
      }
    }
    'applypilot.phase-a.host-provisioning' {
      if ($P.ApprovedCommit -cnotmatch $script:Hex40) { throw 'Host provisioning approvedCommit is invalid.' }
      foreach ($item in @($P.SourceApprovalReceiptSha256,$P.MachineIdentityDigest,$P.StoreConfigSha256,
          $P.StoreTreeManifestSha256,$P.RecoveryKeySpkiSha256,$P.OperatorSidDigest)) {
        Assert-PhaseAHexDigest $item 'Host provisioning digest'
      }
      if ($P.RecoveryKeySpkiSha256 -ceq $P.ExpectedOperatorSigningKeySpkiSha256) {
        throw 'Recovery encryption and operator signing keys must be distinct.'
      }
      [ordered]@{
        schemaVersion=1; receiptType=$P.ReceiptType; approvedCommit=$P.ApprovedCommit;
        sourceApprovalReceiptSha256=$P.SourceApprovalReceiptSha256;
        operatorSigningKeySpkiSha256=$P.ExpectedOperatorSigningKeySpkiSha256;
        machineIdentityDigest=$P.MachineIdentityDigest; storeConfigSha256=$P.StoreConfigSha256;
        storeTreeManifestSha256=$P.StoreTreeManifestSha256; recoveryKeySpkiSha256=$P.RecoveryKeySpkiSha256;
        operatorSidDigest=$P.OperatorSidDigest; result='COMPLETE'; createdAtUtc=$P.CreatedAtUtc
      }
    }
  }
} $PSBoundParameters

$bytes = & $module { param($Value) ConvertTo-PhaseACanonicalJsonBytes $Value } $value
$digest = & $module { param($Bytes) Get-PhaseASha256 $Bytes } $bytes

if ($PSCmdlet.ParameterSetName -eq 'Verify') {
  $received = & $module { param($Path) Read-PhaseAValidatedBytes $Path } $ReceiptPath
  if ([IO.Path]::GetFileName($ReceiptPath) -cne "$digest.json" -or
      -not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($received.Bytes, $bytes)) {
    throw 'Returned receipt does not exactly match caller-authorized canonical bytes.'
  }
  $null = Test-PhaseASignedReceipt -ReceiptPath $ReceiptPath -SignaturePath $SignaturePath `
    -OperatorSigningSpkiPath $OperatorSigningSpkiPath `
    -ExpectedOperatorSigningKeySpkiSha256 $ExpectedOperatorSigningKeySpkiSha256 `
    -ExpectedReceiptType $ReceiptType -ExpectedBindings $value `
    -ExpectedAuthorizedAfterManifestSha256 $ExpectedAfterManifestSha256
  $received.Path
  exit 0
}

$output = & $module { param($Path) Assert-PhaseALocalNtfsPath $Path -AllowMissingLeaf } $OutputDirectory
$protectedOutput = $PSBoundParameters.ContainsKey('ProtectedOperatorSid')
if ($protectedOutput) {
  if ([string]::IsNullOrWhiteSpace($ProtectedOperatorSid)) {
    throw 'Protected unsigned creation requires an operator SID.'
  }
  if (-not (Test-Path -LiteralPath $output -PathType Container)) {
    throw 'Protected unsigned output directory must already exist.'
  }
} elseif (-not (Test-Path -LiteralPath $output -PathType Container)) {
  $null = New-Item -ItemType Directory -Path $output
}
$destination = Join-Path $output "$digest.json"
if ($protectedOutput) {
  $null = & $module {
    param($Path, $Bytes, $Sid)
    Write-PhaseACreateNew $Path $Bytes $Sid
  } $destination $bytes $ProtectedOperatorSid
} else {
  $stream = [IO.FileStream]::new($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try { $stream.Write($bytes); $stream.Flush($true) } finally { $stream.Dispose() }
}
$destination
