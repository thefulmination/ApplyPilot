Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'PhaseAWindowsFile.psm1') -Force

$script:Utf8Strict = [Text.UTF8Encoding]::new($false, $true)
$script:Hex64 = '^[0-9a-f]{64}$'
$script:NativeProgramData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$script:ProductionStoreRoot = [IO.Path]::Combine($script:NativeProgramData, 'ApplyPilot', 'Evidence', 'v1')
$script:ProductionSigningSpkiPath = [IO.Path]::Combine($PSScriptRoot, 'phase-a-anchors', 'signing-spki.der')
$script:ProductionRecoverySpkiPath = [IO.Path]::Combine($PSScriptRoot, 'phase-a-anchors', 'recovery-spki.der')
$script:ProductionSigningSpkiSha256 = $null
$script:ProductionRecoverySpkiSha256 = $null
$script:CommonReceiptFields = @(
  'commit', 'hostProvisioningReceiptSha256', 'machineDigest',
  'manifestAfterSha256', 'manifestBeforeSha256', 'operationId',
  'operatorSidDigest', 'receiptType', 'schema', 'signingKeySpkiSha256',
  'sourceApprovalReceiptSha256', 'storeConfigSha256', 'targetDigest'
)
$script:ReceiptTypes = @(
  'source-approval', 'adjudication', 'credential-revocation',
  'operation-authorization', 'operation-completion', 'host-provisioning'
)
$script:ReceiptFieldsByType = @{
  'host-provisioning' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','manifestBeforeSha256','manifestAfterSha256')
  'source-approval' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','hostProvisioningReceiptSha256',
    'manifestBeforeSha256','manifestAfterSha256')
  'adjudication' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','hostProvisioningReceiptSha256',
    'sourceApprovalReceiptSha256','manifestBeforeSha256','manifestAfterSha256')
  'credential-revocation' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','hostProvisioningReceiptSha256',
    'manifestBeforeSha256','manifestAfterSha256')
  'operation-authorization' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','hostProvisioningReceiptSha256',
    'sourceApprovalReceiptSha256','manifestBeforeSha256','manifestAfterSha256')
  'operation-completion' = @('schema','receiptType','commit','signingKeySpkiSha256','operationId',
    'targetDigest','operatorSidDigest','machineDigest','storeConfigSha256','hostProvisioningReceiptSha256',
    'sourceApprovalReceiptSha256','manifestBeforeSha256','manifestAfterSha256')
}

if (-not ('ApplyPilot.PhaseA.EvidenceNative' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Numerics;
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
        private const int FileDispositionInfo = 4;
        private const uint FileAttributeDirectory = 0x10;
        private const uint FileAttributeReparsePoint = 0x400;

        [StructLayout(LayoutKind.Sequential)]
        private struct FileAttributeTagInformation
        {
            public uint FileAttributes;
            public uint ReparseTag;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdInformation
        {
            public ulong VolumeSerialNumber;
            public ulong FileIdLow;
            public ulong FileIdHigh;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileDispositionInformation
        {
            [MarshalAs(UnmanagedType.U1)] public bool DeleteFile;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFileW(string name, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle file,
            int informationClass, IntPtr information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileAttributeTagInformationByHandleEx(SafeFileHandle file,
            int informationClass, out FileAttributeTagInformation information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileIdInformationByHandleEx(SafeFileHandle file,
            int informationClass, out FileIdInformation information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "SetFileInformationByHandle", SetLastError = true)]
        private static extern bool SetFileDispositionByHandle(SafeFileHandle file,
            int informationClass, ref FileDispositionInformation information, uint size);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle file,
            StringBuilder path, uint size, uint flags);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint GetSecurityInfo(SafeFileHandle handle, int objectType,
            uint securityInfo, out IntPtr owner, out IntPtr group, out IntPtr dacl,
            out IntPtr sacl, out IntPtr securityDescriptor);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint GetSecurityDescriptorLength(IntPtr securityDescriptor);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static byte[] GetFileSecurityDescriptor(SafeFileHandle handle)
        {
            IntPtr owner, group, dacl, sacl, descriptor;
            uint error = GetSecurityInfo(handle, 1, 0x00000007, out owner, out group,
                out dacl, out sacl, out descriptor);
            if (error != 0) throw new Win32Exception((int)error);
            try
            {
                uint length = GetSecurityDescriptorLength(descriptor);
                if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                byte[] bytes = new byte[length];
                Marshal.Copy(descriptor, bytes, 0, checked((int)length));
                return bytes;
            }
            finally { LocalFree(descriptor); }
        }

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

        public static bool VerifyPssSha256ExactSalt(byte[] spki, byte[] content, byte[] signature)
        {
            using (RSA rsa = RSA.Create())
            {
                int read;
                rsa.ImportSubjectPublicKeyInfo(spki, out read);
                if (read != spki.Length || rsa.KeySize != 3072 || signature.Length != 384) return false;
                RSAParameters p = rsa.ExportParameters(false);
                BigInteger s = new BigInteger(signature, true, true);
                BigInteger n = new BigInteger(p.Modulus, true, true);
                BigInteger e = new BigInteger(p.Exponent, true, true);
                if (s >= n) return false;
                byte[] raw = BigInteger.ModPow(s, e, n).ToByteArray(true, true);
                byte[] em = new byte[384];
                if (raw.Length > em.Length) return false;
                Buffer.BlockCopy(raw, 0, em, em.Length - raw.Length, raw.Length);
                if (em[383] != 0xbc) return false;
                const int hLen = 32;
                const int sLen = 32;
                const int dbLen = 351;
                byte[] maskedDb = em.Take(dbLen).ToArray();
                byte[] h = em.Skip(dbLen).Take(hLen).ToArray();
                if ((maskedDb[0] & 0x80) != 0) return false;
                byte[] dbMask = Mgf1(h, dbLen);
                byte[] db = new byte[dbLen];
                for (int i = 0; i < dbLen; i++) db[i] = (byte)(maskedDb[i] ^ dbMask[i]);
                db[0] &= 0x7f;
                int psLen = dbLen - sLen - 1;
                for (int i = 0; i < psLen; i++) if (db[i] != 0) return false;
                if (db[psLen] != 1) return false;
                byte[] mHash = SHA256.HashData(content);
                byte[] prime = new byte[8 + hLen + sLen];
                Buffer.BlockCopy(mHash, 0, prime, 8, hLen);
                Buffer.BlockCopy(db, psLen + 1, prime, 8 + hLen, sLen);
                byte[] expected = SHA256.HashData(prime);
                return CryptographicOperations.FixedTimeEquals(h, expected);
            }
        }

        private static byte[] Mgf1(byte[] seed, int length)
        {
            byte[] result = new byte[length];
            int offset = 0;
            for (uint counter = 0; offset < length; counter++)
            {
                byte[] input = new byte[seed.Length + 4];
                Buffer.BlockCopy(seed, 0, input, 0, seed.Length);
                input[input.Length - 4] = (byte)(counter >> 24);
                input[input.Length - 3] = (byte)(counter >> 16);
                input[input.Length - 2] = (byte)(counter >> 8);
                input[input.Length - 1] = (byte)counter;
                byte[] digest = SHA256.HashData(input);
                int take = Math.Min(digest.Length, length - offset);
                Buffer.BlockCopy(digest, 0, result, offset, take);
                offset += take;
            }
            return result;
        }

        public static int DeleteTreeNoFollow(string rootPath, ulong expectedVolume, string expectedFileId,
            int crashAfterEntries)
        {
            int count = 0;
            var ancestors = OpenAncestorLeases(rootPath);
            try
            {
                using (SafeFileHandle root = OpenDeleteHandle(rootPath))
                {
                    AssertIdentity(root, expectedVolume, expectedFileId);
                    DeleteChildren(rootPath, ref count, crashAfterEntries);
                    SetDelete(root);
                }
            }
            finally { for (int i = ancestors.Count - 1; i >= 0; i--) ancestors[i].Dispose(); }
            return count;
        }

        private static List<SafeFileHandle> OpenAncestorLeases(string path)
        {
            string full = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar);
            string drive = Path.GetPathRoot(full);
            var paths = new List<string> { drive };
            string current = drive.TrimEnd(Path.DirectorySeparatorChar);
            string relative = full.Substring(drive.Length);
            string[] parts = relative.Split(new[] { Path.DirectorySeparatorChar }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = 0; i < parts.Length - 1; i++)
            {
                current = current + Path.DirectorySeparatorChar + parts[i];
                paths.Add(current);
            }
            var handles = new List<SafeFileHandle>();
            try
            {
                foreach (string ancestor in paths)
                {
                    IntPtr raw = CreateFileW(ancestor, 0, 0, IntPtr.Zero, OpenExisting,
                        FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
                    if (raw == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    var handle = new SafeFileHandle(raw, true);
                    FileAttributeTagInformation tag = ReadTag(handle);
                    if ((tag.FileAttributes & FileAttributeReparsePoint) != 0 || tag.ReparseTag != 0)
                    {
                        handle.Dispose();
                        throw new InvalidOperationException("Cleanup ancestor contains a reparse point.");
                    }
                    handles.Add(handle);
                }
                return handles;
            }
            catch
            {
                for (int i = handles.Count - 1; i >= 0; i--) handles[i].Dispose();
                throw;
            }
        }

        private static void DeleteChildren(string path, ref int count, int crashAfterEntries)
        {
            foreach (string childPath in Directory.EnumerateFileSystemEntries(path).OrderBy(x => x, StringComparer.Ordinal))
            {
                using (SafeFileHandle child = OpenDeleteHandle(childPath))
                {
                    FileAttributeTagInformation tag = ReadTag(child);
                    if ((tag.FileAttributes & FileAttributeReparsePoint) != 0 || tag.ReparseTag != 0)
                        throw new InvalidOperationException("Cleanup tree contains a reparse point.");
                    if ((tag.FileAttributes & FileAttributeDirectory) != 0)
                        DeleteChildren(childPath, ref count, crashAfterEntries);
                    SetDelete(child);
                }
                count++;
                if (crashAfterEntries >= 0 && count == crashAfterEntries)
                    throw new InvalidOperationException("Injected cleanup crash.");
            }
        }

        private static SafeFileHandle OpenDeleteHandle(string path)
        {
            IntPtr raw = CreateFileW(path, GenericRead | Delete, 3, IntPtr.Zero, OpenExisting,
                FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
            if (raw == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            var handle = new SafeFileHandle(raw, true);
            try { ReadTag(handle); return handle; }
            catch { handle.Dispose(); throw; }
        }

        private static FileAttributeTagInformation ReadTag(SafeFileHandle handle)
        {
            FileAttributeTagInformation tag;
            if (!GetFileAttributeTagInformationByHandleEx(handle, 9, out tag,
                (uint)Marshal.SizeOf<FileAttributeTagInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return tag;
        }

        private static void AssertIdentity(SafeFileHandle handle, ulong expectedVolume, string expectedFileId)
        {
            FileIdInformation id;
            if (!GetFileIdInformationByHandleEx(handle, 18, out id,
                (uint)Marshal.SizeOf<FileIdInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            byte[] bytes = new byte[16];
            Buffer.BlockCopy(BitConverter.GetBytes(id.FileIdLow), 0, bytes, 0, 8);
            Buffer.BlockCopy(BitConverter.GetBytes(id.FileIdHigh), 0, bytes, 8, 8);
            string actual = Convert.ToHexString(bytes);
            if (id.VolumeSerialNumber != expectedVolume || !String.Equals(actual, expectedFileId,
                StringComparison.Ordinal)) throw new InvalidOperationException("Cleanup target identity changed.");
        }

        private static void SetDelete(SafeFileHandle handle)
        {
            var disposition = new FileDispositionInformation { DeleteFile = true };
            if (!SetFileDispositionByHandle(handle, FileDispositionInfo, ref disposition,
                (uint)Marshal.SizeOf<FileDispositionInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
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

function Read-PhaseAValidatedBytes([string]$Path) {
  $full = Assert-PhaseALocalNtfsPath $Path
  if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw 'Validated byte path is not a file.' }
  $root = Split-Path -Parent $full
  $basename = [IO.Path]::GetFileName($full)
  $handle = Open-PhaseAValidatedFile -Path $full -Access Read -AuthorizedRoot $root -AuthorizedBasename $basename
  try {
    $identity = Get-PhaseAFileIdentity -Handle $handle
    $stream = [IO.FileStream]::new($handle.FileHandle, [IO.FileAccess]::Read)
    $buffer = [IO.MemoryStream]::new()
    try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() }
    finally { $buffer.Dispose(); $stream.Dispose() }
    return [pscustomobject]@{ Bytes=$bytes; Identity=$identity; Path=$full }
  } finally { $handle.Dispose() }
}

function Read-PhaseABytesFromHeldHandle($Handle, [string]$Path) {
  $identity = Get-PhaseAFileIdentity -Handle $Handle
  $borrowed = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new(
    $Handle.FileHandle.DangerousGetHandle(), $false)
  $stream = [IO.FileStream]::new($borrowed, [IO.FileAccess]::Read)
  $buffer = [IO.MemoryStream]::new()
  try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() }
  finally { $buffer.Dispose(); $stream.Dispose(); $borrowed.Dispose() }
  Assert-PhaseAFileIdentity -Handle $Handle -Expected $identity
  return [pscustomobject]@{ Bytes=$bytes; Identity=$identity; Path=$Path }
}

function ConvertFrom-PhaseACanonicalJsonRead($Read) {
  $canonical = [ApplyPilot.PhaseA.EvidenceNative]::Canonicalize($Read.Bytes)
  if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($Read.Bytes, $canonical)) {
    throw 'JSON is not RFC8785 canonical.'
  }
  $text = $script:Utf8Strict.GetString($Read.Bytes)
  return [pscustomobject]@{
    Bytes = $Read.Bytes
    Identity = $Read.Identity
    Path = $Read.Path
    Value = ($text | ConvertFrom-Json -AsHashtable -Depth 32)
    Sha256 = Get-PhaseASha256 $Read.Bytes
  }
}

function Read-PhaseACanonicalJson([string]$Path) {
  $read = Read-PhaseAValidatedBytes $Path
  return ConvertFrom-PhaseACanonicalJsonRead $read
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

function Assert-PhaseALocalNtfsPath(
  [string]$Path,
  [switch]$AllowMissingLeaf,
  [string]$DefinitionDriveFormat,
  [switch]$DefinitionImport
) {
  if ([string]::IsNullOrWhiteSpace($Path) -or $Path.StartsWith('\\') -or -not [IO.Path]::IsPathFullyQualified($Path)) {
    throw 'Only absolute local paths are allowed.'
  }
  $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
  $root = [IO.Path]::GetPathRoot($full)
  $drive = [IO.DriveInfo]::new($root)
  if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Evidence path is not on a fixed drive.' }
  if ($DefinitionDriveFormat -and -not $DefinitionImport) { throw 'Drive-format override requires DefinitionImport.' }
  $driveFormat = if ($DefinitionDriveFormat) { $DefinitionDriveFormat } else { $drive.DriveFormat }
  if ($driveFormat -cne 'NTFS') { throw 'Evidence path is not on NTFS.' }
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
    [string]$CanonicalPath,
    [switch]$PassThru
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
      $digest = Get-PhaseASha256 $stream.ToArray()
      if ($PassThru) {
        return [pscustomobject]@{ Digest=$digest; Identity=$identity; CanonicalVolumePath=$volumePath }
      }
      return $digest
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
  $actualOwner = ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value
  if ($actualOwner -cne $OperatorSid) {
    throw 'Unexpected evidence owner.'
  }
  $trusted = @($OperatorSid, 'S-1-5-18', 'S-1-5-32-544')
  $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 3) { throw 'Evidence DACL must contain exactly three ACEs.' }
  $expectedInheritance = if ($File) { [Security.AccessControl.InheritanceFlags]::None } else {
    [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
  }
  foreach ($sid in $trusted) {
    $matching = @($rules | Where-Object { $_.IdentityReference.Value -ceq $sid })
    if ($matching.Count -ne 1) { throw 'Evidence DACL must contain one ACE per trusted principal.' }
    $rule = $matching[0]
    if ($rule.IsInherited -or
        $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $rule.InheritanceFlags -ne $expectedInheritance -or
        $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
      throw 'Evidence DACL ACE rights, inheritance, or propagation are not exact.'
    }
  }
}

function Assert-PhaseAProtectedFileHandleAcl($Handle, [string]$OperatorSid) {
  $bytes = [ApplyPilot.PhaseA.EvidenceNative]::GetFileSecurityDescriptor($Handle.FileHandle)
  $raw = [Security.AccessControl.RawSecurityDescriptor]::new($bytes, 0)
  if ($null -eq $raw.Owner -or $null -eq $raw.Group -or $null -eq $raw.DiscretionaryAcl) {
    throw 'Protected receipt file security descriptor is incomplete.'
  }
  $expectedControl = [Security.AccessControl.ControlFlags]::DiscretionaryAclPresent -bor
    [Security.AccessControl.ControlFlags]::DiscretionaryAclAutoInherited -bor
    [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected -bor
    [Security.AccessControl.ControlFlags]::SelfRelative
  if ($raw.ControlFlags -ne $expectedControl) {
    throw 'Protected receipt file security descriptor control flags are not exact.'
  }
  if ($raw.Owner.Value -cne $OperatorSid) { throw 'Unexpected protected receipt file owner.' }
  if ($raw.Group.Value -cne $OperatorSid) { throw 'Unexpected protected receipt file primary group.' }
  $trusted = @($OperatorSid, 'S-1-5-18', 'S-1-5-32-544')
  $aces = @($raw.DiscretionaryAcl | ForEach-Object { $_ })
  if ($aces.Count -ne 3) { throw 'Protected receipt file DACL must contain exactly three ACEs.' }
  foreach ($sid in $trusted) {
    $matching = @($aces | Where-Object {
      $_ -is [Security.AccessControl.CommonAce] -and $_.SecurityIdentifier.Value -ceq $sid
    })
    if ($matching.Count -ne 1) { throw 'Protected receipt file DACL must contain one ACE per trusted principal.' }
    $ace = $matching[0]
    if ($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed -or
        $ace.AccessMask -ne [int][Security.AccessControl.FileSystemRights]::FullControl -or
        $ace.AceFlags -ne [Security.AccessControl.AceFlags]::None) {
      throw 'Protected receipt file ACE rights and flags are not exact.'
    }
  }
}

function Open-PhaseAProtectedReceiptPair {
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$OperatorSid
  )
  $receiptFull = Assert-PhaseALocalNtfsPath $ReceiptPath
  $signatureFull = Assert-PhaseALocalNtfsPath $SignaturePath
  $root = Split-Path -Parent $receiptFull
  if ((Split-Path -Parent $signatureFull) -ine $root) { throw 'Receipt and signature must be adjacent.' }
  $receiptHandle = $null
  $signatureHandle = $null
  try {
    $receiptHandle = Open-PhaseAValidatedFile -Path $receiptFull -Access Read `
      -AuthorizedRoot $root -AuthorizedBasename ([IO.Path]::GetFileName($receiptFull))
    $signatureHandle = Open-PhaseAValidatedFile -Path $signatureFull -Access Read `
      -AuthorizedRoot $root -AuthorizedBasename ([IO.Path]::GetFileName($signatureFull))
    Assert-PhaseAProtectedFileHandleAcl $receiptHandle $OperatorSid
    Assert-PhaseAProtectedFileHandleAcl $signatureHandle $OperatorSid
    $receiptRead = Read-PhaseABytesFromHeldHandle $receiptHandle $receiptFull
    $signatureRead = Read-PhaseABytesFromHeldHandle $signatureHandle $signatureFull
    return [pscustomobject]@{
      ReceiptHandle=$receiptHandle; SignatureHandle=$signatureHandle
      Receipt=(ConvertFrom-PhaseACanonicalJsonRead $receiptRead); Signature=$signatureRead
      ReceiptOwnerSid=$OperatorSid
    }
  } catch {
    if ($signatureHandle) { $signatureHandle.Dispose() }
    if ($receiptHandle) { $receiptHandle.Dispose() }
    throw
  }
}

function Assert-PhaseAProtectedReceiptPairIdentity($Pair) {
  Assert-PhaseAFileIdentity -Handle $Pair.ReceiptHandle -Expected $Pair.Receipt.Identity
  Assert-PhaseAFileIdentity -Handle $Pair.SignatureHandle -Expected $Pair.Signature.Identity
  Assert-PhaseAProtectedFileHandleAcl $Pair.ReceiptHandle $Pair.ReceiptOwnerSid
  Assert-PhaseAProtectedFileHandleAcl $Pair.SignatureHandle $Pair.ReceiptOwnerSid
}

function Close-PhaseAProtectedReceiptPair($Pair) {
  if ($null -eq $Pair) { return }
  try { if ($Pair.SignatureHandle) { $Pair.SignatureHandle.Dispose() } }
  finally { if ($Pair.ReceiptHandle) { $Pair.ReceiptHandle.Dispose() } }
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

function Assert-PhaseAAncestorDeleteChild([string]$Path, [string]$OperatorSid, [string]$Boundary) {
  $trusted = @($OperatorSid, 'S-1-5-18', 'S-1-5-32-544')
  $boundaryPath = if ($Boundary) { [IO.Path]::GetFullPath($Boundary).TrimEnd('\') } else { $null }
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
    if ($boundaryPath -and $current.FullName.TrimEnd('\') -ieq $boundaryPath) { break }
    $current = $current.Parent
  }
  if ($boundaryPath -and ($null -eq $current -or $current.FullName.TrimEnd('\') -ine $boundaryPath)) {
    throw 'Ancestor boundary is not an ancestor of the evidence root.'
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
  $bytes = (Read-PhaseAValidatedBytes $Path).Bytes
  if ($ExpectedHash) {
    Assert-PhaseAHexDigest $ExpectedHash 'SPKI hash'
    if ((Get-PhaseASha256 $bytes) -cne $ExpectedHash) { throw 'Signing SPKI hash does not match the committed anchor.' }
  }
  $rsa = [Security.Cryptography.RSA]::Create()
  try {
    $read = 0
    $rsa.ImportSubjectPublicKeyInfo($bytes, [ref]$read)
    if ($read -ne $bytes.Length -or $rsa.KeySize -ne 3072) { throw 'Signing key must be exact RSA-3072 SPKI.' }
    $canonical = $rsa.ExportSubjectPublicKeyInfo()
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($bytes, $canonical)) {
      throw 'Signing SPKI is not canonical DER.'
    }
    return $rsa
  } catch { $rsa.Dispose(); throw }
}

function Test-PhaseASignedReceiptCore {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)]$Receipt,
    [Parameter(Mandatory)]$SignatureRead,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$ExpectedSigningSpkiSha256,
    [Parameter(Mandatory)][ValidateSet('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion','host-provisioning')][string]$ExpectedReceiptType,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedOperationId,
    [Parameter(Mandatory)][string]$ExpectedTargetDigest,
    [Parameter(Mandatory)][string]$ExpectedOperatorSidDigest,
    [Parameter(Mandatory)][string]$ExpectedMachineDigest,
    [Parameter(Mandatory)][string]$ExpectedManifestBeforeSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestAfterSha256,
    [Parameter(Mandatory)][string]$ExpectedStoreConfigSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedSourceApprovalReceiptSha256
  )
  $spkiRead = Read-PhaseAValidatedBytes $SigningSpkiPath
  if ((Split-Path -Parent $receipt.Path) -ine (Split-Path -Parent $signatureRead.Path)) {
    throw 'Receipt and signature must be adjacent.'
  }
  if ([IO.Path]::GetFileName($receipt.Path) -cne "$($receipt.Sha256).json") { throw 'Receipt filename is not its content SHA-256.' }
  if ([IO.Path]::GetFileName($signatureRead.Path) -cne "$($receipt.Sha256).sig") { throw 'Signature filename does not match the receipt.' }
  if ($receipt.Value.receiptType -cne $ExpectedReceiptType -or
      $script:ReceiptTypes -cnotcontains $receipt.Value.receiptType) { throw 'Unsupported or unexpected receipt type.' }
  Assert-PhaseAClosedFields $receipt.Value $script:ReceiptFieldsByType[$ExpectedReceiptType]
  if ($receipt.Value.schema -cne 'applypilot.phase-a.signed-receipt.v1') { throw 'Unsupported receipt schema.' }
  if ([string]$receipt.Value.commit -cnotmatch '^[0-9a-f]{40}$') { throw 'Receipt commit binding is invalid.' }
  $parsedOperation = [guid]::Empty
  if (-not [guid]::TryParseExact([string]$receipt.Value.operationId, 'D', [ref]$parsedOperation)) {
    throw 'Invalid operation ID.'
  }
  foreach ($name in $script:ReceiptFieldsByType[$ExpectedReceiptType]) {
    if ($name -match 'Sha256$|Digest$') { Assert-PhaseAHexDigest ([string]$receipt.Value[$name]) $name }
  }
  $needsHost = $ExpectedReceiptType -in @('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion')
  $needsSource = $ExpectedReceiptType -in @('adjudication','operation-authorization','operation-completion')
  if ($needsHost -and [string]::IsNullOrWhiteSpace($ExpectedHostProvisioningReceiptSha256)) {
    throw 'Expected host-provisioning receipt binding is mandatory for this receipt type.'
  }
  if ($needsSource -and [string]::IsNullOrWhiteSpace($ExpectedSourceApprovalReceiptSha256)) {
    throw 'Expected source-approval receipt binding is mandatory for this receipt type.'
  }
  $spkiBytes = $spkiRead.Bytes
  $spkiHash = Get-PhaseASha256 $spkiBytes
  if ($receipt.Value.signingKeySpkiSha256 -cne $spkiHash) { throw 'Receipt signing-key binding is wrong.' }
  $rsa = Import-PhaseASpki $SigningSpkiPath $ExpectedSigningSpkiSha256
  $rsa.Dispose()
  if (-not [ApplyPilot.PhaseA.EvidenceNative]::VerifyPssSha256ExactSalt(
      $spkiBytes, $receipt.Bytes, $signatureRead.Bytes)) { throw 'Receipt signature or PSS salt length is invalid.' }
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

function Test-PhaseASignedReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,
    [Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$ExpectedSigningSpkiSha256,
    [Parameter(Mandatory)][ValidateSet('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion','host-provisioning')][string]$ExpectedReceiptType,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedOperationId,
    [Parameter(Mandatory)][string]$ExpectedTargetDigest,
    [Parameter(Mandatory)][string]$ExpectedOperatorSidDigest,
    [Parameter(Mandatory)][string]$ExpectedMachineDigest,
    [Parameter(Mandatory)][string]$ExpectedManifestBeforeSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestAfterSha256,
    [Parameter(Mandatory)][string]$ExpectedStoreConfigSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedSourceApprovalReceiptSha256,
    [string]$ProtectedReceiptFileOwnerSid
  )
  $arguments = @{} + $PSBoundParameters
  $arguments.Remove('ReceiptPath')
  $arguments.Remove('SignaturePath')
  $arguments.Remove('ProtectedReceiptFileOwnerSid')
  if ($ProtectedReceiptFileOwnerSid) {
    $pair = Open-PhaseAProtectedReceiptPair $ReceiptPath $SignaturePath $ProtectedReceiptFileOwnerSid
    try {
      $arguments.Receipt = $pair.Receipt
      $arguments.SignatureRead = $pair.Signature
      $result = Test-PhaseASignedReceiptCore @arguments
      Assert-PhaseAProtectedReceiptPairIdentity $pair
      return $result
    } finally { Close-PhaseAProtectedReceiptPair $pair }
  }
  $arguments.Receipt = Read-PhaseACanonicalJson $ReceiptPath
  $arguments.SignatureRead = Read-PhaseAValidatedBytes $SignaturePath
  return Test-PhaseASignedReceiptCore @arguments
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
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][string]$SigningSpkiPath,
    [Parameter(Mandatory)][string]$ExpectedSigningSpkiSha256,
    [Parameter(Mandatory)][ValidateSet('source-approval','adjudication','credential-revocation','operation-authorization','operation-completion','host-provisioning')][string]$ExpectedReceiptType,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExpectedOperationId,
    [Parameter(Mandatory)][string]$ExpectedTargetDigest,
    [Parameter(Mandatory)][string]$ExpectedOperatorSidDigest,
    [Parameter(Mandatory)][string]$ExpectedMachineDigest,
    [Parameter(Mandatory)][string]$ExpectedManifestBeforeSha256,
    [Parameter(Mandatory)][string]$ExpectedManifestAfterSha256,
    [Parameter(Mandatory)][string]$ExpectedStoreConfigSha256,
    [string]$ExpectedHostProvisioningReceiptSha256,
    [string]$ExpectedSourceApprovalReceiptSha256,
    [switch]$Bootstrap,
    [switch]$DefinitionImport,
    [scriptblock]$BeforeFinalPairRevalidation,
    [ValidateSet('after-receipt-stage','after-signature-stage','after-receipt-rename','after-signature-rename','before-pair-revalidation')][string]$CrashAfter
  )
  $validation = @{
    ReceiptPath=$ReceiptPath; SignaturePath=$SignaturePath; SigningSpkiPath=$SigningSpkiPath;
    ExpectedSigningSpkiSha256=$ExpectedSigningSpkiSha256; ExpectedReceiptType=$ExpectedReceiptType;
    ExpectedCommit=$ExpectedCommit; ExpectedOperationId=$ExpectedOperationId;
    ExpectedTargetDigest=$ExpectedTargetDigest; ExpectedOperatorSidDigest=$ExpectedOperatorSidDigest;
    ExpectedMachineDigest=$ExpectedMachineDigest; ExpectedManifestBeforeSha256=$ExpectedManifestBeforeSha256;
    ExpectedManifestAfterSha256=$ExpectedManifestAfterSha256; ExpectedStoreConfigSha256=$ExpectedStoreConfigSha256
  }
  if ($BeforeFinalPairRevalidation -and -not $DefinitionImport) { throw 'Race injection requires DefinitionImport.' }
  foreach ($name in @('ExpectedHostProvisioningReceiptSha256','ExpectedSourceApprovalReceiptSha256')) {
    if ($PSBoundParameters.ContainsKey($name)) { $validation[$name] = $PSBoundParameters[$name] }
  }
  $operatorSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if ($Bootstrap) { $validation.ProtectedReceiptFileOwnerSid = $operatorSid }
  $null = Test-PhaseASignedReceipt @validation
  $root = Assert-PhaseALocalNtfsPath $StoreRoot
  $leaf = switch ($ExpectedReceiptType) {
    'source-approval' { 'bundles' }
    'adjudication' { 'adjudications' }
    default { 'operations' }
  }
  if ($Bootstrap) {
    if ($leaf -cne 'operations') { throw 'Only operation receipts may use bootstrap operations.' }
    $expectedBootstrap = [IO.Path]::Combine(
      [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData),
      'ApplyPilot', 'phase-a-evidence', 'bootstrap-operations')
    if (-not $DefinitionImport -and $root -ine $expectedBootstrap) { throw 'Bootstrap destination identity is not exact.' }
    $destination = $root
  } else {
    if (-not $DefinitionImport -and $root -ine $script:ProductionStoreRoot) {
      throw 'Production receipt installation requires the exact native ProgramData evidence root.'
    }
    $config = Read-PhaseACanonicalJson ([IO.Path]::Combine($root, 'store.json'))
    if ($config.Sha256 -cne $ExpectedStoreConfigSha256) {
      throw 'Receipt destination store configuration binding is wrong.'
    }
    if (-not $DefinitionImport -and (Get-PhaseATargetDigest $root) -cne $config.Value.targetDigest) {
      throw 'Receipt destination store identity is wrong.'
    }
    $destination = Assert-PhaseALocalNtfsPath ([IO.Path]::Combine($root, $leaf))
    if ((Split-Path -Parent $destination) -ine $root) { throw 'Receipt destination escaped the validated store root.' }
  }
  Assert-PhaseAProtectedAcl -Path $destination -OperatorSid $operatorSid
  if ($Bootstrap) {
    $sourcePair = Open-PhaseAProtectedReceiptPair $ReceiptPath $SignaturePath $operatorSid
    try {
      $receiptBytes = $sourcePair.Receipt.Bytes
      $signatureBytes = $sourcePair.Signature.Bytes
      Assert-PhaseAProtectedReceiptPairIdentity $sourcePair
    } finally { Close-PhaseAProtectedReceiptPair $sourcePair }
  } else {
    $receiptBytes = (Read-PhaseAValidatedBytes $ReceiptPath).Bytes
    $signatureBytes = (Read-PhaseAValidatedBytes $SignaturePath).Bytes
  }
  $finalReceipt = Join-Path $destination ([IO.Path]::GetFileName($ReceiptPath))
  $finalSignature = Join-Path $destination ([IO.Path]::GetFileName($SignaturePath))
  if ((Test-Path -LiteralPath $finalReceipt) -or (Test-Path -LiteralPath $finalSignature)) {
    if ((Test-Path -LiteralPath $finalReceipt -PathType Leaf) -and
        (Test-Path -LiteralPath $finalSignature -PathType Leaf) -and
        [Security.Cryptography.CryptographicOperations]::FixedTimeEquals((Read-PhaseAValidatedBytes $finalReceipt).Bytes, $receiptBytes) -and
        [Security.Cryptography.CryptographicOperations]::FixedTimeEquals((Read-PhaseAValidatedBytes $finalSignature).Bytes, $signatureBytes)) {
      Assert-PhaseAProtectedAcl $finalReceipt $operatorSid -File
      Assert-PhaseAProtectedAcl $finalSignature $operatorSid -File
      $validation.ReceiptPath = $finalReceipt; $validation.SignaturePath = $finalSignature
      $null = Test-PhaseASignedReceipt @validation
      return [pscustomobject]@{ ReceiptPath=$finalReceipt; SignaturePath=$finalSignature; Existing=$true }
    }
    throw 'An orphan or conflicting receipt pair already exists.'
  }
  $nonce = [guid]::NewGuid().ToString('N')
  $stageReceipt = Join-Path $destination ".r-$nonce"
  $stageSignature = Join-Path $destination ".s-$nonce"
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
    if ($BeforeFinalPairRevalidation) {
      & $BeforeFinalPairRevalidation ([pscustomobject]@{ ReceiptPath=$finalReceipt; SignaturePath=$finalSignature })
    }
    $finalReceiptRead = Read-PhaseAValidatedBytes $finalReceipt
    $finalSignatureRead = Read-PhaseAValidatedBytes $finalSignature
    foreach ($comparison in @(@($finalReceiptRead.Identity,$expectedReceiptIdentity),@($finalSignatureRead.Identity,$expectedSignatureIdentity))) {
      if ($comparison[0].VolumeSerialNumber -ne $comparison[1].VolumeSerialNumber -or
          $comparison[0].FileId -cne $comparison[1].FileId) { throw 'Published receipt identity changed.' }
    }
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($finalReceiptRead.Bytes, $receiptBytes) -or
        -not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($finalSignatureRead.Bytes, $signatureBytes)) {
      throw 'Published receipt pair changed after rename.'
    }
    Assert-PhaseAProtectedAcl $finalReceipt $operatorSid -File
    Assert-PhaseAProtectedAcl $finalSignature $operatorSid -File
    $validation.ReceiptPath = $finalReceipt; $validation.SignaturePath = $finalSignature
    $null = Test-PhaseASignedReceipt @validation
    return [pscustomobject]@{ ReceiptPath=$finalReceipt; SignaturePath=$finalSignature; Existing=$false }
  } finally {
    foreach ($stage in @($stageReceipt,$stageSignature)) {
      if (Test-Path -LiteralPath $stage -PathType Leaf) {
        $cleanup = Open-PhaseAValidatedFile -Path $stage -Access ReadWriteDelete -AuthorizedRoot $destination -AuthorizedBasename ([IO.Path]::GetFileName($stage))
        try { Set-PhaseAFileDeletionDisposition -Handle $cleanup } finally { $cleanup.Dispose() }
      }
    }
  }
}

function Get-PhaseAReceiptInventory([string]$Root, [string]$OperatorSid) {
  $inventory = [Collections.Generic.List[object]]::new()
  foreach ($leaf in @('bundles','adjudications','operations')) {
    $directory = [IO.Path]::Combine($Root, $leaf)
    Assert-PhaseAProtectedAcl $directory $OperatorSid
    $files = @(Get-ChildItem -LiteralPath $directory -Force)
    if (@($files | Where-Object { $_.PSIsContainer }).Count -ne 0) { throw 'Receipt directories cannot contain nested directories.' }
    foreach ($file in $files) {
      if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Receipt tree contains a reparse object.' }
      Assert-PhaseAProtectedAcl $file.FullName $OperatorSid -File
      if ($file.Name -cnotmatch '^(?<hash>[0-9a-f]{64})\.(?<extension>json|sig)$') {
        throw 'Receipt tree contains an unexpected file.'
      }
    }
    foreach ($group in @($files | Group-Object { if ($_.Name -cmatch '^([0-9a-f]{64})\.(json|sig)$') { $Matches[1] } else { $_.Name } })) {
      $names = @($group.Group.Name | Sort-Object)
      if ($names.Count -ne 2 -or $names[0] -cne "$($group.Name).json" -or $names[1] -cne "$($group.Name).sig") {
        throw 'Receipt tree contains an orphan or duplicate pair.'
      }
      $jsonPath = [IO.Path]::Combine($directory, "$($group.Name).json")
      $value = Read-PhaseACanonicalJson $jsonPath
      if ($value.Sha256 -cne $group.Name) { throw 'Receipt inventory filename hash is wrong.' }
      $inventory.Add([pscustomobject]@{
        Hash=$group.Name; Leaf=$leaf; JsonPath=$jsonPath;
        SignaturePath=[IO.Path]::Combine($directory, "$($group.Name).sig"); Value=$value.Value
      })
    }
  }
  return @($inventory)
}

function Assert-PhaseAEvidenceStore {
  [CmdletBinding()]
  param(
    [string]$StoreRoot = $script:ProductionStoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [string]$SigningSpkiPath = $script:ProductionSigningSpkiPath,
    [string]$RecoverySigningSpkiPath = $script:ProductionRecoverySpkiPath,
    [string]$SigningSpkiSha256 = $script:ProductionSigningSpkiSha256,
    [string]$RecoverySigningSpkiSha256 = $script:ProductionRecoverySpkiSha256,
    [Parameter(Mandatory)][string]$CustodyOperationId,
    [Parameter(Mandatory)][string]$CustodyManifestBeforeSha256,
    [Parameter(Mandatory)][string]$CustodyManifestAfterSha256,
    [string]$ExpectedTargetDigest,
    [string]$ExpectedMachineDigest,
    [string]$AncestorBoundary,
    [switch]$DefinitionImport
  )
  $operatorSid = Assert-PhaseACurrentOperator $CanonicalOperatorSid
  $root = Assert-PhaseALocalNtfsPath $StoreRoot
  if (-not $DefinitionImport) {
    if ($root -ine $script:ProductionStoreRoot) { throw 'Production validation requires the exact native ProgramData evidence root.' }
    if ($SigningSpkiPath -ine $script:ProductionSigningSpkiPath -or
        $RecoverySigningSpkiPath -ine $script:ProductionRecoverySpkiPath -or
        [string]::IsNullOrWhiteSpace($script:ProductionSigningSpkiSha256) -or
        [string]::IsNullOrWhiteSpace($script:ProductionRecoverySpkiSha256) -or
        $SigningSpkiSha256 -cne $script:ProductionSigningSpkiSha256 -or
        $RecoverySigningSpkiSha256 -cne $script:ProductionRecoverySpkiSha256) {
      throw 'Production public anchors are not valid committed anchors.'
    }
  }
  foreach ($digest in @($SigningSpkiSha256,$RecoverySigningSpkiSha256)) { Assert-PhaseAHexDigest $digest 'Committed anchor' }
  if ($AncestorBoundary -and -not $DefinitionImport) { throw 'Ancestor boundary override requires DefinitionImport.' }
  Assert-PhaseAAncestorDeleteChild $root $operatorSid $AncestorBoundary
  Assert-PhaseAProtectedAcl $root $operatorSid
  $rootObjects = @(Get-ChildItem -LiteralPath $root -Force | Sort-Object Name)
  if (@($rootObjects.Name) -join ',' -cne 'adjudications,bundles,operations,store.json') {
    throw 'Evidence root object set is not exact.'
  }
  foreach ($directoryName in @('adjudications','bundles','operations')) {
    $directory = Get-Item -LiteralPath ([IO.Path]::Combine($root,$directoryName)) -Force
    if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw 'Required evidence object is not a regular directory.'
    }
  }
  $configPath = [IO.Path]::Combine($root, 'store.json')
  Assert-PhaseAProtectedAcl $configPath $operatorSid -File
  $config = Read-PhaseACanonicalJson $configPath
  Assert-PhaseAClosedFields $config.Value @('schema','approvedCommit','targetDigest','operatorSidDigest','machineDigest','securityDescriptorSha256','signingSpkiSha256','recoverySigningSpkiSha256')
  $target = if ($ExpectedTargetDigest) { $ExpectedTargetDigest } else { Get-PhaseATargetDigest $root }
  $machine = if ($ExpectedMachineDigest) { $ExpectedMachineDigest } else { Get-PhaseAMachineDigest }
  if (($ExpectedTargetDigest -or $ExpectedMachineDigest) -and -not $DefinitionImport) { throw 'Identity overrides require DefinitionImport.' }
  if ($config.Value.schema -cne 'applypilot.phase-a.evidence-store.v1' -or
      $config.Value.approvedCommit -cne $ExpectedCommit -or
      $config.Value.targetDigest -cne $target -or
      $config.Value.operatorSidDigest -cne (Get-PhaseAOperatorSidDigest $operatorSid) -or
      $config.Value.machineDigest -cne $machine -or
      $config.Value.securityDescriptorSha256 -cne (Get-PhaseASecurityDescriptorHash $root) -or
      $config.Value.signingSpkiSha256 -cne $SigningSpkiSha256 -or
      $config.Value.recoverySigningSpkiSha256 -cne $RecoverySigningSpkiSha256) { throw 'Evidence store configuration is invalid.' }
  $signing = Import-PhaseASpki $SigningSpkiPath $SigningSpkiSha256; $signing.Dispose()
  $recovery = Import-PhaseASpki $RecoverySigningSpkiPath $RecoverySigningSpkiSha256; $recovery.Dispose()
  $inventory = @(Get-PhaseAReceiptInventory $root $operatorSid)
  $hosts = @($inventory | Where-Object { $_.Value.receiptType -ceq 'host-provisioning' })
  if ($hosts.Count -ne 1 -or $hosts[0].Leaf -cne 'operations') { throw 'Store requires exactly one host-provisioning receipt pair.' }
  $hostHash = $hosts[0].Hash
  $sourceHashes = @($inventory | Where-Object { $_.Value.receiptType -ceq 'source-approval' } | ForEach-Object Hash)
  foreach ($item in $inventory) {
    $type = [string]$item.Value.receiptType
    $expectedLeaf = switch ($type) { 'source-approval' {'bundles'} 'adjudication' {'adjudications'} default {'operations'} }
    if ($item.Leaf -cne $expectedLeaf) { throw 'Receipt is stored in the wrong exact destination.' }
    if ($type -ne 'host-provisioning' -and $item.Value.hostProvisioningReceiptSha256 -cne $hostHash) {
      throw 'Receipt host-provisioning binding is missing or wrong.'
    }
    if ($type -in @('adjudication','operation-authorization','operation-completion') -and
        $sourceHashes -cnotcontains $item.Value.sourceApprovalReceiptSha256) {
      throw 'Receipt source-approval binding is missing or wrong.'
    }
    $keyPath = if ($type -in @('host-provisioning','credential-revocation','operation-authorization','operation-completion')) { $RecoverySigningSpkiPath } else { $SigningSpkiPath }
    $keyHash = if ($keyPath -ieq $RecoverySigningSpkiPath) { $RecoverySigningSpkiSha256 } else { $SigningSpkiSha256 }
    $expectedOperation = if ($type -ceq 'host-provisioning') { $CustodyOperationId } else { [string]$item.Value.operationId }
    $expectedBefore = if ($type -ceq 'host-provisioning') { $CustodyManifestBeforeSha256 } else { [string]$item.Value.manifestBeforeSha256 }
    $expectedAfter = if ($type -ceq 'host-provisioning') { $CustodyManifestAfterSha256 } else { [string]$item.Value.manifestAfterSha256 }
    $arguments = @{
      ReceiptPath=$item.JsonPath; SignaturePath=$item.SignaturePath; SigningSpkiPath=$keyPath;
      ExpectedSigningSpkiSha256=$keyHash; ExpectedReceiptType=$type; ExpectedCommit=$ExpectedCommit;
      ExpectedOperationId=$expectedOperation; ExpectedTargetDigest=$target;
      ExpectedOperatorSidDigest=$config.Value.operatorSidDigest; ExpectedMachineDigest=$machine;
      ExpectedManifestBeforeSha256=$expectedBefore; ExpectedManifestAfterSha256=$expectedAfter;
      ExpectedStoreConfigSha256=$config.Sha256
    }
    if ($type -ne 'host-provisioning') { $arguments.ExpectedHostProvisioningReceiptSha256=$hostHash }
    if ($type -in @('adjudication','operation-authorization','operation-completion')) {
      $arguments.ExpectedSourceApprovalReceiptSha256=[string]$item.Value.sourceApprovalReceiptSha256
    }
    $null = Test-PhaseASignedReceipt @arguments
  }
  return [pscustomobject]@{ Valid=$true; StoreRoot=$root; StoreConfigSha256=$config.Sha256; TargetDigest=$target; HostProvisioningReceiptSha256=$hostHash }
}

Export-ModuleMember -Function @(
  'Assert-PhaseAEvidenceStore', 'Get-PhaseADirectoryManifest',
  'Get-PhaseAMachineDigest', 'Get-PhaseAOperatorSidDigest',
  'Get-PhaseASecurityDescriptorHash', 'Get-PhaseATargetDigest',
  'Install-PhaseASignedReceipt', 'Test-PhaseASignedReceipt'
)
