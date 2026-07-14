Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'PhaseAWindowsFile.psm1') -Force

$script:Utf8Strict = [Text.UTF8Encoding]::new($false, $true)
$script:Hex64 = '^[0-9a-f]{64}$'
$script:ReceiptFields = @(
  'commit', 'hostProvisioningReceiptSha256', 'machineDigest',
  'manifestAfterSha256', 'manifestBeforeSha256', 'operationId',
  'operatorSidDigest', 'receiptType', 'schema', 'signingKeySpkiSha256',
  'sourceApprovalReceiptSha256', 'storeConfigSha256', 'targetDigest'
)
$script:ReceiptTypes = @(
  'source-approval', 'adjudication', 'credential-revocation',
  'operation-authorization', 'operation-completion', 'host-provisioning'
)

if (-not ('ApplyPilot.PhaseA.EvidenceNative' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace ApplyPilot.PhaseA
{
    public static class EvidenceNative
    {
        private const uint GenericRead = 0x80000000;
        private const uint GenericWrite = 0x40000000;
        private const uint Delete = 0x00010000;
        private const uint OpenExisting = 3;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const uint FileFlagOpenReparsePoint = 0x00200000;
        private const int FileRenameInfo = 3;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFileW(string name, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle file,
            int informationClass, IntPtr information, uint size);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle file,
            StringBuilder path, uint size, uint flags);

        public static string GetVolumeGuidPath(SafeFileHandle handle)
        {
            uint size = 512;
            while (true)
            {
                var buffer = new StringBuilder((int)size);
                uint length = GetFinalPathNameByHandleW(handle, buffer, size, 1);
                if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                if (length < size) return buffer.ToString();
                size = checked(length + 1);
            }
        }

        public static void RenameDirectoryNoReplace(string source, string destination)
        {
            IntPtr raw = CreateFileW(source, GenericRead | GenericWrite | Delete, 0,
                IntPtr.Zero, OpenExisting,
                FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
            if (raw == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            using (var handle = new SafeFileHandle(raw, true))
            {
                RenameNoReplace(handle, destination);
            }
        }

        public static void RenameFileNoReplace(SafeFileHandle handle, string destination)
        {
            RenameNoReplace(handle, destination);
        }

        private static void RenameNoReplace(SafeFileHandle handle, string destination)
        {
            byte[] name = Encoding.Unicode.GetBytes(destination);
            int rootOffset = IntPtr.Size == 8 ? 8 : 4;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + sizeof(uint);
            int size = checked(nameOffset + name.Length + sizeof(char));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                for (int i = 0; i < size; i++) Marshal.WriteByte(buffer, i, 0);
                Marshal.WriteInt32(buffer, 0, 0);
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandle(handle, FileRenameInfo, buffer, (uint)size))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static byte[] Canonicalize(byte[] input)
        {
            var options = new JsonDocumentOptions {
                AllowTrailingCommas = false, CommentHandling = JsonCommentHandling.Disallow
            };
            using (JsonDocument document = JsonDocument.Parse(input, options))
            using (var output = new MemoryStream())
            {
                var writerOptions = new JsonWriterOptions {
                    Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                    Indented = false, SkipValidation = false
                };
                using (var writer = new Utf8JsonWriter(output, writerOptions))
                {
                    WriteCanonical(document.RootElement, writer);
                }
                return output.ToArray();
            }
        }

        private static void WriteCanonical(JsonElement element, Utf8JsonWriter writer)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.Object:
                    writer.WriteStartObject();
                    var properties = new List<JsonProperty>();
                    var names = new HashSet<string>(StringComparer.Ordinal);
                    foreach (JsonProperty property in element.EnumerateObject())
                    {
                        if (!names.Add(property.Name))
                            throw new InvalidDataException("Duplicate JSON property.");
                        properties.Add(property);
                    }
                    properties.Sort((a, b) => StringComparer.Ordinal.Compare(a.Name, b.Name));
                    foreach (JsonProperty property in properties)
                    {
                        writer.WritePropertyName(property.Name);
                        WriteCanonical(property.Value, writer);
                    }
                    writer.WriteEndObject();
                    break;
                case JsonValueKind.Array:
                    writer.WriteStartArray();
                    foreach (JsonElement item in element.EnumerateArray())
                        WriteCanonical(item, writer);
                    writer.WriteEndArray();
                    break;
                case JsonValueKind.String:
                    writer.WriteStringValue(element.GetString());
                    break;
                case JsonValueKind.Number:
                    long signed;
                    ulong unsigned;
                    if (element.TryGetInt64(out signed)) writer.WriteNumberValue(signed);
                    else if (element.TryGetUInt64(out unsigned)) writer.WriteNumberValue(unsigned);
                    else throw new InvalidDataException("Only canonical integer JSON numbers are allowed.");
                    break;
                case JsonValueKind.True: writer.WriteBooleanValue(true); break;
                case JsonValueKind.False: writer.WriteBooleanValue(false); break;
                case JsonValueKind.Null: writer.WriteNullValue(); break;
                default: throw new InvalidDataException("Unsupported JSON value.");
            }
            writer.Flush();
        }
    }
}
'@
}

function Get-PhaseASha256([byte[]]$Bytes) {
  return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes)).ToLowerInvariant()
}

function ConvertTo-PhaseACanonicalJsonBytes($Value) {
  $json = $Value | ConvertTo-Json -Depth 32 -Compress
  $bytes = $script:Utf8Strict.GetBytes($json)
  return ,[ApplyPilot.PhaseA.EvidenceNative]::Canonicalize($bytes)
}

function Read-PhaseACanonicalJson([string]$Path) {
  $null = Assert-PhaseALocalNtfsPath $Path
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Canonical JSON path is not a file.' }
  $bytes = [IO.File]::ReadAllBytes($Path)
  $canonical = [ApplyPilot.PhaseA.EvidenceNative]::Canonicalize($bytes)
  if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($bytes, $canonical)) {
    throw 'JSON is not RFC8785 canonical.'
  }
  $text = $script:Utf8Strict.GetString($bytes)
  return [pscustomobject]@{
    Bytes = $bytes
    Value = ($text | ConvertFrom-Json -AsHashtable -Depth 32)
    Sha256 = Get-PhaseASha256 $bytes
  }
}

function Assert-PhaseAHexDigest([string]$Value, [string]$Name) {
  if ($Value -cnotmatch $script:Hex64) { throw "$Name must be a lowercase SHA-256 digest." }
}

function Assert-PhaseAClosedFields($Value, [string[]]$Expected) {
  if ($Value -isnot [Collections.IDictionary]) { throw 'JSON root must be an object.' }
  $actual = @($Value.Keys | Sort-Object)
  $wanted = @($Expected | Sort-Object)
  if ($actual.Count -ne $wanted.Count -or (Compare-Object $actual $wanted)) {
    throw 'JSON object does not match its closed schema.'
  }
}

function Assert-PhaseACurrentOperator([string]$CanonicalOperatorSid) {
  $canonical = [Security.Principal.SecurityIdentifier]::new($CanonicalOperatorSid).Value
  $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if ($current -cne $canonical) { throw 'Current token SID is not the canonical operator SID.' }
  if ($current -in @('S-1-5-18','S-1-5-19','S-1-5-20') -or $current.StartsWith('S-1-5-80-')) {
    throw 'SYSTEM and service identities cannot act as the canonical operator.'
  }
  return $canonical
}

function Get-PhaseAOperatorSidDigest {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$CanonicalOperatorSid)
  $sid = Assert-PhaseACurrentOperator $CanonicalOperatorSid
  $domain = [Text.Encoding]::ASCII.GetBytes("applypilot.phase-a.operator-sid.v1`0")
  $value = [Text.Encoding]::ASCII.GetBytes($sid)
  return Get-PhaseASha256 ($domain + $value)
}

function ConvertTo-PhaseAGuidBytes([string]$Value, [string]$Name) {
  $guid = [guid]::Empty
  if (-not [guid]::TryParseExact($Value, 'D', [ref]$guid) -or $guid -eq [guid]::Empty) {
    throw "$Name is missing, malformed, or all-zero."
  }
  return ,$guid.ToByteArray()
}

function Get-PhaseAMachineDigest {
  [CmdletBinding(DefaultParameterSetName='Machine')]
  param(
    [Parameter(ParameterSetName='Test', Mandatory)][string]$MachineGuid,
    [Parameter(ParameterSetName='Test', Mandatory)][string]$SmbiosUuid,
    [Parameter(ParameterSetName='Test', Mandatory)][switch]$DefinitionImport
  )
  if ($PSCmdlet.ParameterSetName -eq 'Machine') {
    $MachineGuid = [string](Get-ItemPropertyValue -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop)
    $SmbiosUuid = [string](Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop).UUID
  }
  $domain = [Text.Encoding]::ASCII.GetBytes("applypilot.phase-a.machine.v1`0")
  return Get-PhaseASha256 ($domain +
    (ConvertTo-PhaseAGuidBytes $MachineGuid 'MachineGuid') +
    (ConvertTo-PhaseAGuidBytes $SmbiosUuid 'SMBIOS UUID'))
}

function Assert-PhaseALocalNtfsPath([string]$Path, [switch]$AllowMissingLeaf) {
  if ([string]::IsNullOrWhiteSpace($Path) -or $Path.StartsWith('\\') -or -not [IO.Path]::IsPathFullyQualified($Path)) {
    throw 'Only absolute local paths are allowed.'
  }
  $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
  $root = [IO.Path]::GetPathRoot($full)
  $drive = [IO.DriveInfo]::new($root)
  if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Evidence path is not on a fixed drive.' }
  if ($drive.DriveFormat -cne 'NTFS') { throw 'Evidence path is not on NTFS.' }
  $current = $root.TrimEnd('\')
  $parts = $full.Substring($root.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)
  foreach ($part in $parts) {
    $current = Join-Path $current $part
    if (-not (Test-Path -LiteralPath $current)) {
      if ($AllowMissingLeaf) { break }
      throw "Path does not exist: $current"
    }
    $item = Get-Item -LiteralPath $current -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'Reparse points are not allowed in the path chain.'
    }
  }
  return $full
}

function Get-PhaseATargetDigest {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$Path,
    [string]$CanonicalPath
  )
  $full = Assert-PhaseALocalNtfsPath $Path
  $lease = Open-PhaseAValidatedDirectoryLease -Path $full
  try {
    $identity = Get-PhaseAFileIdentity -Handle $lease
    $volumePath = [ApplyPilot.PhaseA.EvidenceNative]::GetVolumeGuidPath($lease.FileHandle)
    if ($CanonicalPath) {
      $leaf = [IO.Path]::GetFileName($full)
      $canonicalLeaf = [IO.Path]::GetFileName([IO.Path]::GetFullPath($CanonicalPath).TrimEnd('\'))
      if (-not $volumePath.EndsWith($leaf, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cannot derive canonical target path.'
      }
      $volumePath = $volumePath.Substring(0, $volumePath.Length - $leaf.Length) + $canonicalLeaf
    }
    $pathBytes = $script:Utf8Strict.GetBytes($volumePath)
    $stream = [IO.MemoryStream]::new()
    try {
      $writer = [IO.BinaryWriter]::new($stream, [Text.Encoding]::ASCII, $true)
      $writer.Write([Text.Encoding]::ASCII.GetBytes("applypilot.phase-a.target.v1`0"))
      $writer.Write([uint64]$identity.VolumeSerialNumber)
      $writer.Write([Convert]::FromHexString([string]$identity.FileId))
      $writer.Write([uint32]$pathBytes.Length)
      $writer.Write($pathBytes)
      $writer.Flush()
      return Get-PhaseASha256 $stream.ToArray()
    } finally { $stream.Dispose() }
  } finally { $lease.Dispose() }
}

function Get-PhaseASecurityDescriptorHash {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$Path)
  $acl = Get-Acl -LiteralPath $Path
  $raw = [Security.AccessControl.RawSecurityDescriptor]::new(
    $acl.GetSecurityDescriptorBinaryForm(), 0)
  if ($null -eq $raw.Owner -or $null -eq $raw.Group -or $null -eq $raw.DiscretionaryAcl) {
    throw 'Security descriptor must contain owner, primary group, and DACL.'
  }
  $control = $raw.ControlFlags -band (-bnot [Security.AccessControl.ControlFlags]::SystemAclPresent)
  $clean = [Security.AccessControl.RawSecurityDescriptor]::new(
    $control, $raw.Owner, $raw.Group, $null, $raw.DiscretionaryAcl)
  $bytes = [byte[]]::new($clean.BinaryLength)
  $clean.GetBinaryForm($bytes, 0)
  return Get-PhaseASha256 $bytes
}

function Assert-PhaseAProtectedAcl([string]$Path, [string]$OperatorSid, [switch]$File) {
  $acl = Get-Acl -LiteralPath $Path
  if (-not $acl.AreAccessRulesProtected) { throw 'DACL inheritance must be disabled.' }
  if ($acl.Owner -notin @($OperatorSid, ([Security.Principal.SecurityIdentifier]::new($OperatorSid).Translate([Security.Principal.NTAccount]).Value))) {
    throw 'Unexpected evidence owner.'
  }
  $trusted = @($OperatorSid, 'S-1-5-18', 'S-1-5-32-544')
  $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 3) { throw 'Evidence DACL must contain exactly three ACEs.' }
  foreach ($rule in $rules) {
    if ($rule.IsInherited -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $trusted -cnotcontains $rule.IdentityReference.Value -or
        ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne [Security.AccessControl.FileSystemRights]::FullControl) {
      throw 'Evidence DACL contains an inherited or untrusted ACE.'
    }
    if (-not $File -and ($rule.InheritanceFlags -band
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit)) -eq 0) {
      throw 'Directory trust ACEs must propagate to children.'
    }
  }
}

function Set-PhaseAProtectedAcl([string]$Path, [string]$OperatorSid, [switch]$File) {
  $security = if ($File) { [Security.AccessControl.FileSecurity]::new() } else { [Security.AccessControl.DirectorySecurity]::new() }
  $security.SetAccessRuleProtection($true, $false)
  $owner = [Security.Principal.SecurityIdentifier]::new($OperatorSid)
  $security.SetOwner($owner)
  foreach ($sid in @($owner,
      [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
      [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $inheritance = if ($File) { [Security.AccessControl.InheritanceFlags]::None } else {
      [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
      [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
      [Security.AccessControl.PropagationFlags]::None,
      [Security.AccessControl.AccessControlType]::Allow)
    $null = $security.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $Path -AclObject $security
  Assert-PhaseAProtectedAcl -Path $Path -OperatorSid $OperatorSid -File:$File
}

function Assert-PhaseAAncestorDeleteChild([string]$Path, [string]$OperatorSid) {
  $trusted = @($OperatorSid, 'S-1-5-18', 'S-1-5-32-544')
  $current = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path)).Parent
  while ($null -ne $current) {
    $acl = Get-Acl -LiteralPath $current.FullName
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
      if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
          $trusted -cnotcontains $rule.IdentityReference.Value -and
          ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles)) {
        throw 'An ancestor grants untrusted DELETE_CHILD.'
      }
    }
    $current = $current.Parent
  }
}

function Get-PhaseADirectoryManifest {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$Root)
  $full = Assert-PhaseALocalNtfsPath $Root
  $entries = [Collections.Generic.List[object]]::new()
  foreach ($item in Get-ChildItem -LiteralPath $full -Force -Recurse | Sort-Object FullName) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Manifest tree contains a reparse point.' }
    $relative = [IO.Path]::GetRelativePath($full, $item.FullName).Replace('\', '/')
    if ($item.PSIsContainer) {
      $entries.Add([ordered]@{ kind='directory'; relativePath=$relative })
    } else {
      $handle = Open-PhaseAValidatedFile -Path $item.FullName -Access Read -AuthorizedRoot $full -AuthorizedBasename $item.Name
      try {
        $stream = [IO.FileStream]::new($handle.FileHandle, [IO.FileAccess]::Read)
        $buffer = [IO.MemoryStream]::new()
        try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() }
        finally { $buffer.Dispose(); $stream.Dispose() }
      } finally { $handle.Dispose() }
      $entries.Add([ordered]@{ kind='file'; length=[uint64]$bytes.Length; relativePath=$relative; sha256=(Get-PhaseASha256 $bytes) })
    }
  }
  return [ordered]@{ schema='applypilot.phase-a.directory-manifest.v1'; entries=@($entries) }
}

function Import-PhaseASpki([string]$Path, [string]$ExpectedHash) {
  $bytes = [IO.File]::ReadAllBytes((Assert-PhaseALocalNtfsPath $Path))
  if ($ExpectedHash) {
    Assert-PhaseAHexDigest $ExpectedHash 'SPKI hash'
    if ((Get-PhaseASha256 $bytes) -cne $ExpectedHash) { throw 'Signing SPKI hash does not match the committed anchor.' }
  }
  $rsa = [Security.Cryptography.RSA]::Create()
  try {
    $read = 0
    $rsa.ImportSubjectPublicKeyInfo($bytes, [ref]$read)
    if ($read -ne $bytes.Length -or $rsa.KeySize -ne 3072) { throw 'Signing key must be exact RSA-3072 SPKI.' }
    return $rsa
  } catch { $rsa.Dispose(); throw }
}

function Test-PhaseASignedReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [string]$ExpectedSigningSpkiSha256,
    [string]$ExpectedReceiptType,
    [string]$ExpectedCommit,
    [string]$ExpectedOperationId,
    [string]$ExpectedTargetDigest,
    [string]$ExpectedOperatorSidDigest,
    [string]$ExpectedMachineDigest,
    [string]$ExpectedManifestBeforeSha256,
    [string]$ExpectedManifestAfterSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedStoreConfigSha256,
    [string]$ExpectedSourceApprovalReceiptSha256
  )
  $receipt = Read-PhaseACanonicalJson $ReceiptPath
  $null = Assert-PhaseALocalNtfsPath $SignaturePath
  $null = Assert-PhaseALocalNtfsPath $SigningSpkiPath
  if (-not (Test-Path -LiteralPath $SignaturePath -PathType Leaf) -or
      -not (Test-Path -LiteralPath $SigningSpkiPath -PathType Leaf)) {
    throw 'Signature and SPKI inputs must be files.'
  }
  if ([IO.Path]::GetFileName($ReceiptPath) -cne "$($receipt.Sha256).json") { throw 'Receipt filename is not its content SHA-256.' }
  if ([IO.Path]::GetFileName($SignaturePath) -cne "$($receipt.Sha256).sig") { throw 'Signature filename does not match the receipt.' }
  Assert-PhaseAClosedFields $receipt.Value $script:ReceiptFields
  if ($receipt.Value.schema -cne 'applypilot.phase-a.signed-receipt.v1' -or
      $script:ReceiptTypes -cnotcontains $receipt.Value.receiptType) { throw 'Unsupported receipt schema or type.' }
  if ([string]$receipt.Value.commit -cnotmatch '^[0-9a-f]{40}$') { throw 'Receipt commit binding is invalid.' }
  $parsedOperation = [guid]::Empty
  if (-not [guid]::TryParseExact([string]$receipt.Value.operationId, 'D', [ref]$parsedOperation)) {
    throw 'Invalid operation ID.'
  }
  foreach ($name in @('signingKeySpkiSha256','targetDigest','operatorSidDigest','machineDigest',
      'storeConfigSha256','hostProvisioningReceiptSha256','sourceApprovalReceiptSha256',
      'manifestBeforeSha256','manifestAfterSha256')) {
    Assert-PhaseAHexDigest ([string]$receipt.Value[$name]) $name
  }
  $spkiBytes = [IO.File]::ReadAllBytes($SigningSpkiPath)
  $spkiHash = Get-PhaseASha256 $spkiBytes
  if ($receipt.Value.signingKeySpkiSha256 -cne $spkiHash) { throw 'Receipt signing-key binding is wrong.' }
  $rsa = Import-PhaseASpki $SigningSpkiPath $ExpectedSigningSpkiSha256
  try {
    $signature = [IO.File]::ReadAllBytes($SignaturePath)
    if ($signature.Length -ne 384 -or -not $rsa.VerifyData($receipt.Bytes, $signature,
        [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.RSASignaturePadding]::Pss)) { throw 'Receipt signature is invalid.' }
  } finally { $rsa.Dispose() }
  $bindings = @{
    receiptType=$ExpectedReceiptType; commit=$ExpectedCommit; operationId=$ExpectedOperationId;
    targetDigest=$ExpectedTargetDigest; operatorSidDigest=$ExpectedOperatorSidDigest;
    machineDigest=$ExpectedMachineDigest; manifestBeforeSha256=$ExpectedManifestBeforeSha256;
    manifestAfterSha256=$ExpectedManifestAfterSha256;
    hostProvisioningReceiptSha256=$ExpectedHostProvisioningReceiptSha256;
    storeConfigSha256=$ExpectedStoreConfigSha256;
    sourceApprovalReceiptSha256=$ExpectedSourceApprovalReceiptSha256
  }
  foreach ($binding in $bindings.GetEnumerator()) {
    if ($binding.Value -and [string]$receipt.Value[$binding.Key] -cne [string]$binding.Value) {
      throw "Receipt $($binding.Key) binding is wrong."
    }
  }
  return $true
}

function Write-PhaseACreateNew([string]$Path, [byte[]]$Bytes, [string]$OperatorSid) {
  $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try { $stream.Write($Bytes); $stream.Flush($true) } finally { $stream.Dispose() }
  Set-PhaseAProtectedAcl -Path $Path -OperatorSid $OperatorSid -File
}

function Install-PhaseASignedReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$DestinationDirectory,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [string]$ExpectedSigningSpkiSha256,
    [string]$ExpectedReceiptType,
    [string]$ExpectedCommit,
    [string]$ExpectedOperationId,
    [string]$ExpectedTargetDigest,
    [string]$ExpectedOperatorSidDigest,
    [string]$ExpectedMachineDigest,
    [string]$ExpectedManifestBeforeSha256,
    [string]$ExpectedManifestAfterSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedStoreConfigSha256,
    [string]$ExpectedSourceApprovalReceiptSha256,
    [ValidateSet('after-receipt-stage','after-signature-stage','after-receipt-rename','after-signature-rename','before-pair-revalidation')][string]$CrashAfter
  )
  $validation = @{
    ReceiptPath=$ReceiptPath; SignaturePath=$SignaturePath; SigningSpkiPath=$SigningSpkiPath
  }
  foreach ($name in @('ExpectedSigningSpkiSha256','ExpectedReceiptType','ExpectedCommit',
      'ExpectedOperationId','ExpectedTargetDigest','ExpectedOperatorSidDigest','ExpectedMachineDigest',
      'ExpectedManifestBeforeSha256','ExpectedManifestAfterSha256',
      'ExpectedHostProvisioningReceiptSha256','ExpectedStoreConfigSha256',
      'ExpectedSourceApprovalReceiptSha256')) {
    if ($PSBoundParameters.ContainsKey($name)) { $validation[$name] = $PSBoundParameters[$name] }
  }
  $null = Test-PhaseASignedReceipt @validation
  $destination = Assert-PhaseALocalNtfsPath $DestinationDirectory
  $operatorSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  Assert-PhaseAProtectedAcl -Path $destination -OperatorSid $operatorSid
  $receiptBytes = [IO.File]::ReadAllBytes($ReceiptPath)
  $signatureBytes = [IO.File]::ReadAllBytes($SignaturePath)
  $receiptValue = (Read-PhaseACanonicalJson $ReceiptPath).Value
  $allowedLeaf = switch ([string]$receiptValue.receiptType) {
    'source-approval' { @('bundles') }
    'adjudication' { @('adjudications') }
    default { @('operations','bootstrap-operations') }
  }
  if ($allowedLeaf -cnotcontains [IO.Path]::GetFileName($destination)) {
    throw 'Receipt type is not allowed at this destination.'
  }
  $finalReceipt = Join-Path $destination ([IO.Path]::GetFileName($ReceiptPath))
  $finalSignature = Join-Path $destination ([IO.Path]::GetFileName($SignaturePath))
  if ((Test-Path -LiteralPath $finalReceipt) -or (Test-Path -LiteralPath $finalSignature)) {
    if ((Test-Path -LiteralPath $finalReceipt -PathType Leaf) -and
        (Test-Path -LiteralPath $finalSignature -PathType Leaf) -and
        [Security.Cryptography.CryptographicOperations]::FixedTimeEquals([IO.File]::ReadAllBytes($finalReceipt), $receiptBytes) -and
        [Security.Cryptography.CryptographicOperations]::FixedTimeEquals([IO.File]::ReadAllBytes($finalSignature), $signatureBytes)) {
      $validation.ReceiptPath = $finalReceipt; $validation.SignaturePath = $finalSignature
      $null = Test-PhaseASignedReceipt @validation
      return [pscustomobject]@{ ReceiptPath=$finalReceipt; SignaturePath=$finalSignature; Existing=$true }
    }
    throw 'An orphan or conflicting receipt pair already exists.'
  }
  $nonce = [guid]::NewGuid().ToString('N')
  $stageReceipt = Join-Path $destination ".$([IO.Path]::GetFileName($ReceiptPath)).stage-$nonce"
  $stageSignature = Join-Path $destination ".$([IO.Path]::GetFileName($SignaturePath)).stage-$nonce"
  try {
    Write-PhaseACreateNew $stageReceipt $receiptBytes $operatorSid
    if ($CrashAfter -eq 'after-receipt-stage') { throw 'Injected crash after receipt stage.' }
    Write-PhaseACreateNew $stageSignature $signatureBytes $operatorSid
    if ($CrashAfter -eq 'after-signature-stage') { throw 'Injected crash after signature stage.' }
    $handle = Open-PhaseAValidatedFile -Path $stageReceipt -Access ReadWriteDelete -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($stageReceipt))
    $beforeReceiptIdentity = Get-PhaseAFileIdentity -Handle $handle
    try {
      [ApplyPilot.PhaseA.EvidenceNative]::RenameFileNoReplace($handle.FileHandle, $finalReceipt)
    } finally { $handle.Dispose() }
    $probe = Open-PhaseAValidatedFile -Path $finalReceipt -Access Read -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($finalReceipt))
    try {
      $expectedReceiptIdentity = Get-PhaseAFileIdentity -Handle $probe
      if ($expectedReceiptIdentity.VolumeSerialNumber -ne $beforeReceiptIdentity.VolumeSerialNumber -or
          $expectedReceiptIdentity.FileId -cne $beforeReceiptIdentity.FileId) { throw 'Receipt identity changed during rename.' }
    } finally { $probe.Dispose() }
    if ($CrashAfter -eq 'after-receipt-rename') { throw 'Injected crash after receipt rename.' }
    $handle = Open-PhaseAValidatedFile -Path $stageSignature -Access ReadWriteDelete -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($stageSignature))
    $beforeSignatureIdentity = Get-PhaseAFileIdentity -Handle $handle
    try {
      [ApplyPilot.PhaseA.EvidenceNative]::RenameFileNoReplace($handle.FileHandle, $finalSignature)
    } finally { $handle.Dispose() }
    $probe = Open-PhaseAValidatedFile -Path $finalSignature -Access Read -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($finalSignature))
    try {
      $expectedSignatureIdentity = Get-PhaseAFileIdentity -Handle $probe
      if ($expectedSignatureIdentity.VolumeSerialNumber -ne $beforeSignatureIdentity.VolumeSerialNumber -or
          $expectedSignatureIdentity.FileId -cne $beforeSignatureIdentity.FileId) { throw 'Signature identity changed during rename.' }
    } finally { $probe.Dispose() }
    if ($CrashAfter -eq 'after-signature-rename') { throw 'Injected crash after signature rename.' }
    if ($CrashAfter -eq 'before-pair-revalidation') { throw 'Injected crash before pair revalidation.' }
    $reopenedReceipt = Open-PhaseAValidatedFile -Path $finalReceipt -Access Read -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($finalReceipt))
    try { Assert-PhaseAFileIdentity -Handle $reopenedReceipt -Expected $expectedReceiptIdentity } finally { $reopenedReceipt.Dispose() }
    $reopenedSignature = Open-PhaseAValidatedFile -Path $finalSignature -Access Read -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($finalSignature))
    try { Assert-PhaseAFileIdentity -Handle $reopenedSignature -Expected $expectedSignatureIdentity } finally { $reopenedSignature.Dispose() }
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals([IO.File]::ReadAllBytes($finalReceipt), $receiptBytes) -or
        -not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals([IO.File]::ReadAllBytes($finalSignature), $signatureBytes)) {
      throw 'Published receipt pair changed after rename.'
    }
    $validation.ReceiptPath = $finalReceipt; $validation.SignaturePath = $finalSignature
    $null = Test-PhaseASignedReceipt @validation
    return [pscustomobject]@{ ReceiptPath=$finalReceipt; SignaturePath=$finalSignature; Existing=$false }
  } finally {
    foreach ($stage in @($stageReceipt,$stageSignature)) {
      if (Test-Path -LiteralPath $stage -PathType Leaf) { Remove-Item -LiteralPath $stage -Force }
    }
  }
}

function Assert-PhaseAEvidenceStore {
  [CmdletBinding()]
  param(
    [string]$StoreRoot = (Join-Path $env:ProgramData 'ApplyPilot\Evidence\v1'),
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiPath,
    [Parameter(Mandatory)][string]$SigningSpkiSha256,
    [Parameter(Mandatory)][string]$RecoverySigningSpkiSha256,
    [Parameter(Mandatory)][string]$CustodyReceiptPath,
    [Parameter(Mandatory)][string]$CustodySignaturePath
  )
  $operatorSid = Assert-PhaseACurrentOperator $CanonicalOperatorSid
  $root = Assert-PhaseALocalNtfsPath $StoreRoot
  Assert-PhaseAAncestorDeleteChild $root $operatorSid
  Assert-PhaseAProtectedAcl $root $operatorSid
  $directories = @(Get-ChildItem -LiteralPath $root -Directory -Force)
  if (@($directories.Name | Sort-Object) -join ',' -cne 'adjudications,bundles,operations') {
    throw 'Evidence store subdirectories are not exact.'
  }
  foreach ($directory in $directories) { Assert-PhaseAProtectedAcl $directory.FullName $operatorSid }
  $configPath = Join-Path $root 'store.json'
  $config = Read-PhaseACanonicalJson $configPath
  Assert-PhaseAClosedFields $config.Value @('schema','targetDigest','operatorSidDigest','machineDigest','securityDescriptorSha256','signingSpkiSha256','recoverySigningSpkiSha256')
  if ($config.Value.schema -cne 'applypilot.phase-a.evidence-store.v1' -or
      $config.Value.targetDigest -cne (Get-PhaseATargetDigest $root) -or
      $config.Value.operatorSidDigest -cne (Get-PhaseAOperatorSidDigest $operatorSid) -or
      $config.Value.machineDigest -cne (Get-PhaseAMachineDigest) -or
      $config.Value.securityDescriptorSha256 -cne (Get-PhaseASecurityDescriptorHash $root) -or
      $config.Value.signingSpkiSha256 -cne $SigningSpkiSha256 -or
      $config.Value.recoverySigningSpkiSha256 -cne $RecoverySigningSpkiSha256) { throw 'Evidence store configuration is invalid.' }
  $signing = Import-PhaseASpki $SigningSpkiPath $SigningSpkiSha256; $signing.Dispose()
  $recovery = Import-PhaseASpki $RecoverySigningSpkiPath $RecoverySigningSpkiSha256; $recovery.Dispose()
  $null = Test-PhaseASignedReceipt -ReceiptPath $CustodyReceiptPath -SignaturePath $CustodySignaturePath -SigningSpkiPath $RecoverySigningSpkiPath -ExpectedSigningSpkiSha256 $RecoverySigningSpkiSha256 -ExpectedReceiptType 'host-provisioning' -ExpectedTargetDigest $config.Value.targetDigest -ExpectedOperatorSidDigest $config.Value.operatorSidDigest -ExpectedMachineDigest $config.Value.machineDigest -ExpectedStoreConfigSha256 $config.Sha256
  return [pscustomobject]@{ Valid=$true; StoreRoot=$root; StoreConfigSha256=$config.Sha256; TargetDigest=$config.Value.targetDigest }
}

Export-ModuleMember -Function @(
  'Assert-PhaseAEvidenceStore', 'Get-PhaseADirectoryManifest',
  'Get-PhaseAMachineDigest', 'Get-PhaseAOperatorSidDigest',
  'Get-PhaseASecurityDescriptorHash', 'Get-PhaseATargetDigest',
  'Install-PhaseASignedReceipt', 'Test-PhaseASignedReceipt'
)
