Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'PhaseAWindowsFile.psm1') -Force

$script:Utf8Strict = [Text.UTF8Encoding]::new($false, $true)
$script:Hex64 = '^[0-9a-f]{64}$'
$script:Hex40 = '^[0-9a-f]{40}$'
$script:UtcSeconds = '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'
$script:NativeProgramData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$script:ProductionStoreRoot = [IO.Path]::Combine($script:NativeProgramData, 'ApplyPilot', 'Evidence', 'v1')
$script:RepositoryRoot = Split-Path -Parent $PSScriptRoot
$script:PhaseAConfigRoot = [IO.Path]::Combine($script:RepositoryRoot, 'config', 'phase-a')
$script:ProductionOperatorSigningMetadataPath = [IO.Path]::Combine($script:RepositoryRoot, 'config', 'phase-a', 'operator-signing-key.json')
$script:ProductionOperatorSigningSpkiPath = [IO.Path]::Combine($script:RepositoryRoot, 'config', 'phase-a', 'operator-signing-key.spki.der')
$script:ProductionRecoveryEncryptionMetadataPath = [IO.Path]::Combine($script:RepositoryRoot, 'config', 'phase-a', 'recovery-encryption-key.json')
$script:ProductionRecoveryEncryptionSpkiPath = [IO.Path]::Combine($script:RepositoryRoot, 'config', 'phase-a', 'recovery-encryption-key.spki.der')
$script:PhaseABundleAuthenticator = $null
$script:ReceiptTypes = @(
  'applypilot.phase-a.runtime-source-approval',
  'applypilot.phase-a.evidence-adjudication',
  'applypilot.phase-a.credential-revocation',
  'applypilot.phase-a.provisioning-cleanup-authorization',
  'applypilot.phase-a.legacy-sidecar-destruction-authorization',
  'applypilot.phase-a.provisioning-cleanup-completion',
  'applypilot.phase-a.legacy-sidecar-destruction-completion',
  'applypilot.phase-a.host-provisioning'
)
$script:ReceiptFieldsByType = @{
  'applypilot.phase-a.runtime-source-approval' = @('schemaVersion','receiptType','approvedCommit','approvedTree',
    'planSha256','operatorSigningKeySpkiSha256','specReview','qualityReview','criticalFileSha256','nonce','createdAtUtc')
  'applypilot.phase-a.evidence-adjudication' = @('schemaVersion','receiptType','sourceIdentityDigest',
    'selectedBundleSha256','candidateBundleSha256','operatorSigningKeySpkiSha256','nonce','createdAtUtc')
  'applypilot.phase-a.credential-revocation' = @('schemaVersion','receiptType','approvedCommit',
    'operatorSigningKeySpkiSha256','credentialReferenceDigest','providerClass','revokedAtUtc','staleProbeAtUtc',
    'staleProbeResult','providerEvidenceSha256','machineIdentityDigest','nonce')
  'applypilot.phase-a.provisioning-cleanup-authorization' = @('schemaVersion','receiptType','approvedCommit',
    'operatorSigningKeySpkiSha256','operationId','targetIdentityDigest','beforeManifestSha256',
    'expectedAfterManifestSha256','evidenceBundleSha256','credentialInventoryRoot','credentialRevocationSetRoot',
    'operatorSid','createdAtUtc')
  'applypilot.phase-a.legacy-sidecar-destruction-authorization' = @('schemaVersion','receiptType','approvedCommit',
    'operatorSigningKeySpkiSha256','operationId','targetIdentityDigest','beforeManifestSha256',
    'expectedAfterManifestSha256','evidenceBundleSha256','credentialInventoryRoot','credentialRevocationSetRoot',
    'operatorSid','createdAtUtc')
  'applypilot.phase-a.provisioning-cleanup-completion' = @('schemaVersion','receiptType','approvedCommit',
    'operatorSigningKeySpkiSha256','operationId','authorizationReceiptSha256','actualAfterManifestSha256',
    'result','createdAtUtc')
  'applypilot.phase-a.legacy-sidecar-destruction-completion' = @('schemaVersion','receiptType','approvedCommit',
    'operatorSigningKeySpkiSha256','operationId','authorizationReceiptSha256','actualAfterManifestSha256',
    'result','createdAtUtc')
  'applypilot.phase-a.host-provisioning' = @('schemaVersion','receiptType','approvedCommit',
    'sourceApprovalReceiptSha256','operatorSigningKeySpkiSha256','machineIdentityDigest','storeConfigSha256',
    'storeTreeManifestSha256','recoveryKeySpkiSha256','operatorSidDigest','result','createdAtUtc')
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
        private const uint ReadControl = 0x00020000;
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
        private struct FileStandardInformation
        {
            public long AllocationSize;
            public long EndOfFile;
            public uint NumberOfLinks;
            [MarshalAs(UnmanagedType.U1)] public bool DeletePending;
            [MarshalAs(UnmanagedType.U1)] public bool Directory;
        }

        public sealed class RawFileIdentity
        {
            public ulong VolumeSerialNumber { get; set; }
            public string FileId { get; set; }
            public uint NumberOfLinks { get; set; }
            public string FinalPath { get; set; }
            public ulong Size { get; set; }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileDispositionInformation
        {
            [MarshalAs(UnmanagedType.U1)] public bool DeleteFile;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateFileW(string name, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateDirectoryW(string path, ref SecurityAttributes security);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle file,
            int informationClass, IntPtr information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileAttributeTagInformationByHandleEx(SafeFileHandle file,
            int informationClass, out FileAttributeTagInformation information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileIdInformationByHandleEx(SafeFileHandle file,
            int informationClass, out FileIdInformation information, uint size);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        private static extern bool GetFileStandardInformationByHandleEx(SafeFileHandle file,
            int informationClass, out FileStandardInformation information, uint size);

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

        public static SafeFileHandle OpenManifestObject(string path, bool directory)
        {
            string full = Path.GetFullPath(path);
            uint access = directory ? ReadControl : GenericRead;
            uint flags = FileFlagOpenReparsePoint | (directory ? FileFlagBackupSemantics : 0);
            uint share = directory ? 3U : 0U;
            IntPtr raw = CreateFileW(full, access, share, IntPtr.Zero, OpenExisting, flags, IntPtr.Zero);
            if (raw == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            var handle = new SafeFileHandle(raw, true);
            try
            {
                FileAttributeTagInformation tag;
                if (!GetFileAttributeTagInformationByHandleEx(handle, 9, out tag,
                    (uint)Marshal.SizeOf<FileAttributeTagInformation>()))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if ((tag.FileAttributes & FileAttributeReparsePoint) != 0 || tag.ReparseTag != 0)
                    throw new InvalidOperationException("Manifest objects cannot be reparse points.");
                bool actualDirectory = (tag.FileAttributes & FileAttributeDirectory) != 0;
                if (actualDirectory != directory) throw new InvalidOperationException("Manifest object kind changed.");
                string final = NormalizeFinalPath(GetFinalPathName(handle));
                if (!String.Equals(final, full.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Manifest object final path changed.");
                return handle;
            }
            catch { handle.Dispose(); throw; }
        }

        public static RawFileIdentity GetRawFileIdentity(SafeFileHandle handle)
        {
            FileIdInformation id;
            if (!GetFileIdInformationByHandleEx(handle, 18, out id, (uint)Marshal.SizeOf<FileIdInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            FileStandardInformation standard;
            if (!GetFileStandardInformationByHandleEx(handle, 1, out standard,
                (uint)Marshal.SizeOf<FileStandardInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (standard.NumberOfLinks != 1) throw new InvalidOperationException("Manifest objects must have one hard link.");
            byte[] identifier = new byte[16];
            Buffer.BlockCopy(BitConverter.GetBytes(id.FileIdLow), 0, identifier, 0, 8);
            Buffer.BlockCopy(BitConverter.GetBytes(id.FileIdHigh), 0, identifier, 8, 8);
            return new RawFileIdentity {
                VolumeSerialNumber=id.VolumeSerialNumber, FileId=Convert.ToHexString(identifier),
                NumberOfLinks=standard.NumberOfLinks, FinalPath=NormalizeFinalPath(GetFinalPathName(handle)),
                Size=checked((ulong)standard.EndOfFile)
            };
        }

        public static void AssertRawFileIdentity(SafeFileHandle handle, RawFileIdentity expected)
        {
            RawFileIdentity actual = GetRawFileIdentity(handle);
            if (actual.VolumeSerialNumber != expected.VolumeSerialNumber || actual.FileId != expected.FileId ||
                actual.NumberOfLinks != expected.NumberOfLinks || actual.Size != expected.Size ||
                !String.Equals(actual.FinalPath, expected.FinalPath, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Manifest object identity drifted: expected " +
                    expected.FinalPath + " links=" + expected.NumberOfLinks + " size=" + expected.Size +
                    "; actual " + actual.FinalPath + " links=" + actual.NumberOfLinks + " size=" + actual.Size + ".");
        }

        public static string GetHandleSecurityDescriptorHash(SafeFileHandle handle)
        {
            return Convert.ToHexString(SHA256.HashData(GetFileSecurityDescriptor(handle))).ToLowerInvariant();
        }

        public static string HashHandleContent(SafeFileHandle handle, ulong maximumSize)
        {
            RawFileIdentity before = GetRawFileIdentity(handle);
            if (before.Size > maximumSize || before.Size > 9007199254740991UL)
                throw new InvalidOperationException("Manifest file exceeds the bounded safe size.");
            using (var borrowed = new SafeFileHandle(handle.DangerousGetHandle(), false))
            using (var stream = new FileStream(borrowed, FileAccess.Read))
            using (var sha = SHA256.Create())
            {
                byte[] buffer = new byte[1024 * 1024];
                int read;
                ulong total = 0;
                while ((read = stream.Read(buffer, 0, buffer.Length)) != 0)
                {
                    total = checked(total + (ulong)read);
                    if (total > maximumSize) throw new InvalidOperationException("Manifest file grew beyond the bound.");
                    sha.TransformBlock(buffer, 0, read, null, 0);
                }
                sha.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                AssertRawFileIdentity(handle, before);
                return Convert.ToHexString(sha.Hash).ToLowerInvariant();
            }
        }

        private static string GetFinalPathName(SafeFileHandle handle)
        {
            uint size = 512;
            while (true)
            {
                var value = new StringBuilder((int)size);
                uint length = GetFinalPathNameByHandleW(handle, value, size, 0);
                if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                if (length < size) return value.ToString();
                size = checked(length + 1);
            }
        }

        private static string NormalizeFinalPath(string path)
        {
            if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + path.Substring(8);
            if (path.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) return path.Substring(4).TrimEnd('\\');
            return path.TrimEnd('\\');
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

        public static SafeFileHandle CreateProtectedDirectory(string path, byte[] securityDescriptor)
        {
            string full = Path.GetFullPath(path).TrimEnd('\\');
            GCHandle pin = GCHandle.Alloc(securityDescriptor, GCHandleType.Pinned);
            try
            {
                var attributes = new SecurityAttributes {
                    Length=Marshal.SizeOf<SecurityAttributes>(),
                    SecurityDescriptor=pin.AddrOfPinnedObject(), InheritHandle=false
                };
                if (!CreateDirectoryW(full, ref attributes)) throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally { pin.Free(); }
            IntPtr raw = CreateFileW(full, ReadControl | Delete, 7, IntPtr.Zero, OpenExisting,
                FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
            if (raw == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            var handle = new SafeFileHandle(raw, true);
            try
            {
                FileAttributeTagInformation tag;
                if (!GetFileAttributeTagInformationByHandleEx(handle, 9, out tag,
                    (uint)Marshal.SizeOf<FileAttributeTagInformation>()))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if ((tag.FileAttributes & FileAttributeReparsePoint) != 0 || tag.ReparseTag != 0 ||
                    (tag.FileAttributes & FileAttributeDirectory) == 0)
                    throw new InvalidOperationException("Created staging object is not a plain directory.");
                RawFileIdentity identity = GetRawFileIdentity(handle);
                if (!String.Equals(identity.FinalPath, full, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Created staging directory identity changed before lease acquisition.");
                return handle;
            }
            catch { handle.Dispose(); throw; }
        }

        public static void RenameDirectoryHandleNoReplace(SafeFileHandle handle, string destination)
        {
            RawFileIdentity before = GetRawFileIdentity(handle);
            string target = Path.GetFullPath(destination).TrimEnd('\\');
            if (Directory.Exists(target) || File.Exists(target)) throw new IOException("Directory destination already exists.");
            byte[] name = Encoding.Unicode.GetBytes(target);
            int rootOffset = IntPtr.Size == 8 ? 8 : 4;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + sizeof(uint);
            int size = checked(nameOffset + name.Length + sizeof(char));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                for (int index = 0; index < size; index++) Marshal.WriteByte(buffer, index, 0);
                Marshal.WriteInt32(buffer, 0, 0);
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandle(handle, FileRenameInfo, buffer, (uint)size))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally { Marshal.FreeHGlobal(buffer); }
            RawFileIdentity after = GetRawFileIdentity(handle);
            if (after.VolumeSerialNumber != before.VolumeSerialNumber || after.FileId != before.FileId ||
                !String.Equals(after.FinalPath, target, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Published directory identity changed during same-handle rename.");
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

        public static bool VerifyPssSha256ExactSalt(RSA rsa, byte[] content, byte[] signature)
        {
                if (rsa == null || rsa.KeySize != 3072 || signature.Length != 384) return false;
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
                    string rawNumber = element.GetRawText();
                    if (rawNumber.IndexOf('.') >= 0 || rawNumber.IndexOf('e') >= 0 || rawNumber.IndexOf('E') >= 0)
                        throw new InvalidDataException("Floating-point JSON numbers are not allowed.");
                    long signed;
                    ulong unsigned;
                    const long safe = 9007199254740991L;
                    if (element.TryGetInt64(out signed) && signed >= -safe && signed <= safe)
                        writer.WriteNumberValue(signed);
                    else if (element.TryGetUInt64(out unsigned) && unsigned <= (ulong)safe)
                        writer.WriteNumberValue(unsigned);
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
    Value = ($text | ConvertFrom-Json -AsHashtable -Depth 32 -DateKind String)
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

function Assert-PhaseAUtcSeconds([string]$Value, [string]$Name) {
  if ($Value -cnotmatch $script:UtcSeconds) { throw "$Name must be exact UTC seconds." }
  $parsed = [DateTimeOffset]::MinValue
  if (-not [DateTimeOffset]::TryParseExact($Value, "yyyy-MM-dd'T'HH:mm:ss'Z'",
      [Globalization.CultureInfo]::InvariantCulture,
      [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$parsed)) {
    throw "$Name is not a valid UTC timestamp."
  }
}

function Assert-PhaseAUuid([string]$Value, [string]$Name) {
  $parsed = [guid]::Empty
  if (-not [guid]::TryParseExact($Value, 'D', [ref]$parsed) -or $parsed -eq [guid]::Empty) {
    throw "$Name must be a nonzero UUID."
  }
}

function Assert-PhaseASid([string]$Value, [string]$Name) {
  try { $sid = [Security.Principal.SecurityIdentifier]::new($Value) } catch { throw "$Name is not a canonical SID." }
  if ($sid.Value -cne $Value) { throw "$Name is not a canonical SID." }
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

function Get-PhaseATargetIdentityDigestFromParts([uint64]$VolumeSerialNumber,[string]$FileId,[string]$CanonicalVolumeGuidPath) {
  if($FileId -cnotmatch '^[0-9A-Fa-f]{32}$'){throw 'FILE_ID_128 must contain exactly 16 bytes.'}
  $pathBytes=$script:Utf8Strict.GetBytes($CanonicalVolumeGuidPath)
  if($pathBytes.Length -gt [uint32]::MaxValue){throw 'Canonical volume-GUID path is too long.'}
  $stream=[IO.MemoryStream]::new()
  try{
    $writer=[IO.BinaryWriter]::new($stream,[Text.Encoding]::ASCII,$true)
    $writer.Write([Text.Encoding]::ASCII.GetBytes("applypilot.phase-a.target.v1`0"))
    $writer.Write($VolumeSerialNumber)
    $writer.Write([Convert]::FromHexString($FileId))
    $writer.Write([uint32]$pathBytes.Length)
    $writer.Write($pathBytes);$writer.Flush()
    return Get-PhaseASha256 $stream.ToArray()
  }finally{$stream.Dispose()}
}

function Get-PhaseARelativePathDigest([string]$RelativePath) {
  if([string]::IsNullOrEmpty($RelativePath) -or $RelativePath.Contains('/') -or
      $RelativePath.StartsWith('\') -or $RelativePath.EndsWith('\') -or
      @($RelativePath.Split('\')) -contains '..'){throw 'Held relative path is not canonical.'}
  $relativeBytes=$script:Utf8Strict.GetBytes($RelativePath)
  $domain=[Text.Encoding]::ASCII.GetBytes("applypilot.phase-a.relative-path.v1`0")
  $bytes=[byte[]]::new($domain.Length+$relativeBytes.Length)
  [Array]::Copy($domain,0,$bytes,0,$domain.Length)
  [Array]::Copy($relativeBytes,0,$bytes,$domain.Length,$relativeBytes.Length)
  return Get-PhaseASha256 $bytes
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
    $digest=Get-PhaseATargetIdentityDigestFromParts ([uint64]$identity.VolumeSerialNumber) ([string]$identity.FileId) $volumePath
    if ($PassThru) {return [pscustomobject]@{ Digest=$digest; Identity=$identity; CanonicalVolumePath=$volumePath }}
    return $digest
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
  $handle = [ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($Path, -not $File)
  try {
    $bytes = [ApplyPilot.PhaseA.EvidenceNative]::GetFileSecurityDescriptor($handle)
    $acl = if ($File) {
      [Security.AccessControl.FileSecurity]::new()
    } else {
      [Security.AccessControl.DirectorySecurity]::new()
    }
    $acl.SetSecurityDescriptorBinaryForm($bytes)
    if (-not $acl.AreAccessRulesProtected) { throw 'DACL inheritance must be disabled.' }
    $actualOwner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
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
  } finally { $handle.Dispose() }
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

function New-PhaseAProtectedSecurity([string]$OperatorSid, [switch]$File) {
  $security = if ($File) { [Security.AccessControl.FileSecurity]::new() } else { [Security.AccessControl.DirectorySecurity]::new() }
  $security.SetAccessRuleProtection($true, $false)
  $owner = [Security.Principal.SecurityIdentifier]::new($OperatorSid)
  $security.SetOwner($owner)
  $security.SetGroup($owner)
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
  return $security
}

function Get-PhaseAProtectedSecurityDescriptorBytes([string]$OperatorSid, [switch]$File) {
  return ,((New-PhaseAProtectedSecurity $OperatorSid -File:$File).GetSecurityDescriptorBinaryForm())
}

function Set-PhaseAProtectedAcl([string]$Path, [string]$OperatorSid, [switch]$File) {
  $security=New-PhaseAProtectedSecurity $OperatorSid -File:$File
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
  param(
    [Parameter(Mandatory)][string]$Root,
    [string]$CanonicalRootPath,
    [uint64]$MaximumFileSize = 1073741824,
    [uint32]$MaximumEntries = 100000,
    [scriptblock]$BeforeObjectRevalidation,
    [switch]$DefinitionImport
  )
  if($BeforeObjectRevalidation -and -not $DefinitionImport){throw 'Manifest race injection requires DefinitionImport.'}
  if($MaximumFileSize -gt 9007199254740991){throw 'Manifest file bound exceeds interoperable JSON range.'}
  $full = Assert-PhaseALocalNtfsPath $Root
  $baseHandle=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($full,$true)
  $entries = [Collections.Generic.List[object]]::new()
  try{
    $baseIdentity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($baseHandle)
    $baseVolumePath=[ApplyPilot.PhaseA.EvidenceNative]::GetVolumeGuidPath($baseHandle).TrimEnd('\')
    $canonicalBaseVolumePath=$baseVolumePath
    if($CanonicalRootPath){
      $actualLeaf=[IO.Path]::GetFileName($full.TrimEnd('\'));$canonicalLeaf=[IO.Path]::GetFileName([IO.Path]::GetFullPath($CanonicalRootPath).TrimEnd('\'))
      if(-not $baseVolumePath.EndsWith($actualLeaf,[StringComparison]::OrdinalIgnoreCase)){throw 'Cannot derive canonical manifest root path.'}
      $canonicalBaseVolumePath=$baseVolumePath.Substring(0,$baseVolumePath.Length-$actualLeaf.Length)+$canonicalLeaf
    }
    $baseDigest=Get-PhaseATargetIdentityDigestFromParts ([uint64]$baseIdentity.VolumeSerialNumber) ([string]$baseIdentity.FileId) $canonicalBaseVolumePath
    $walk={param([string]$Directory)
      foreach($item in @(Get-ChildItem -LiteralPath $Directory -Force | Sort-Object Name -CaseSensitive)){
        if($entries.Count -ge $MaximumEntries){throw 'Manifest entry count exceeds the configured bound.'}
        if($item.Name.Contains(':') -or $item.Name.EndsWith('.') -or $item.Name.EndsWith(' ')){throw 'Manifest object has an alias or ADS name.'}
        $handle=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($item.FullName,[bool]$item.PSIsContainer)
        try{
          $identity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($handle)
          $objectVolumePath=[ApplyPilot.PhaseA.EvidenceNative]::GetVolumeGuidPath($handle).TrimEnd('\')
          $prefix=$baseVolumePath+'\'
          if(-not $objectVolumePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Manifest held object escaped the held base volume-GUID path.'}
          $relative=$objectVolumePath.Substring($prefix.Length).Replace('/','\')
          if([string]::IsNullOrEmpty($relative) -or $relative.StartsWith('\') -or $relative.EndsWith('\') -or
              $relative.Contains(':') -or @($relative.Split('\')) -contains '..'){throw 'Manifest relative path escaped or aliased the held base.'}
          $relativeDigest=Get-PhaseARelativePathDigest $relative
          $canonicalObjectVolumePath=$canonicalBaseVolumePath+'\'+$relative
          $objectDigest=Get-PhaseATargetIdentityDigestFromParts ([uint64]$identity.VolumeSerialNumber) ([string]$identity.FileId) $canonicalObjectVolumePath
          $securityDigest=[ApplyPilot.PhaseA.EvidenceNative]::GetHandleSecurityDescriptorHash($handle)
          if($item.PSIsContainer){
            $content='0'*64;$size=[uint64]0
            & $walk $identity.FinalPath
          }else{
            $content=[ApplyPilot.PhaseA.EvidenceNative]::HashHandleContent($handle,$MaximumFileSize)
            $size=[uint64]$identity.Size
          }
          if($BeforeObjectRevalidation){& $BeforeObjectRevalidation $identity.FinalPath}
          [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($handle,$identity)
          if([ApplyPilot.PhaseA.EvidenceNative]::GetHandleSecurityDescriptorHash($handle) -cne $securityDigest){throw 'Manifest object security descriptor drifted.'}
          $entries.Add([ordered]@{relativePathDigest=$relativeDigest;objectIdentityDigest=$objectDigest;
            kind=$(if($item.PSIsContainer){'directory'}else{'file'});contentSha256=$content;
            securityDescriptorSha256=$securityDigest;size=$size})
        }finally{$handle.Dispose()}
      }
    }
    & $walk $full
    [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($baseHandle,$baseIdentity)
    $sorted=@($entries.ToArray()|Sort-Object -CaseSensitive -Property { [string]$_['relativePathDigest'] })
    return [ordered]@{schemaVersion=1;manifestType='applypilot.phase-a.directory-manifest';
      baseRootIdentityDigest=$baseDigest;entries=$sorted}
  }finally{$baseHandle.Dispose()}
}

function Import-PhaseASpkiBytes([byte[]]$Bytes, [string]$ExpectedHash) {
  Assert-PhaseAHexDigest $ExpectedHash 'SPKI hash'
  if ((Get-PhaseASha256 $Bytes) -cne $ExpectedHash) { throw 'Signing SPKI hash does not match the committed anchor.' }
  $rsa = [Security.Cryptography.RSA]::Create()
  try {
    $read = 0
    $rsa.ImportSubjectPublicKeyInfo($Bytes, [ref]$read)
    if ($read -ne $Bytes.Length -or $rsa.KeySize -ne 3072) { throw 'Signing key must be exact RSA-3072 SPKI.' }
    $canonical = $rsa.ExportSubjectPublicKeyInfo()
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($Bytes, $canonical)) {
      throw 'Signing SPKI is not canonical DER.'
    }
    return $rsa
  } catch { $rsa.Dispose(); throw }
}

function Import-PhaseAOperatorSigningSpkiBytes([byte[]]$Bytes, [string]$ExpectedHash) {
  return Import-PhaseASpkiBytes $Bytes $ExpectedHash
}

function Import-PhaseARecoveryEncryptionSpkiBytes([byte[]]$Bytes, [string]$ExpectedHash) {
  return Import-PhaseASpkiBytes $Bytes $ExpectedHash
}

function Import-PhaseASpki([string]$Path, [string]$ExpectedHash) {
  $full = Assert-PhaseALocalNtfsPath $Path
  $root = Split-Path -Parent $full
  $handle = Open-PhaseAValidatedFile -Path $full -Access Read -AuthorizedRoot $root `
    -AuthorizedBasename ([IO.Path]::GetFileName($full))
  try {
    $read = Read-PhaseABytesFromHeldHandle $handle $full
    $rsa = Import-PhaseASpkiBytes $read.Bytes $ExpectedHash
    Assert-PhaseAFileIdentity -Handle $handle -Expected $read.Identity
    return $rsa
  } finally { $handle.Dispose() }
}

function Import-PhaseAAnchorMetadata {
  param(
    [Parameter(Mandatory)][string]$MetadataPath,
    [Parameter(Mandatory)][string]$SpkiPath,
    [Parameter(Mandatory)][string]$ExpectedPurpose,
    [Parameter(Mandatory)][string]$ExpectedSpkiFile,
    [Parameter(Mandatory)][ValidateSet('operator-signing','recovery-encryption')][string]$KeyClass
  )
  $metadata = Read-PhaseACanonicalJson $MetadataPath
  Assert-PhaseAClosedFields $metadata.Value @('schemaVersion','keyPurpose','spkiFile','spkiSha256')
  if ($metadata.Value.schemaVersion -ne 1 -or
      $metadata.Value.keyPurpose -cne $ExpectedPurpose -or
      $metadata.Value.spkiFile -cne $ExpectedSpkiFile) {
    throw 'Phase A anchor metadata identity is invalid.'
  }
  $hash = [string]$metadata.Value.spkiSha256
  Assert-PhaseAHexDigest $hash 'Anchor SPKI hash'
  $read = Read-PhaseAValidatedBytes $SpkiPath
  $rsa = if ($KeyClass -eq 'operator-signing') {
    Import-PhaseAOperatorSigningSpkiBytes $read.Bytes $hash
  } else {
    Import-PhaseARecoveryEncryptionSpkiBytes $read.Bytes $hash
  }
  $rsa.Dispose()
  return [pscustomobject]@{ MetadataPath=$metadata.Path; SpkiPath=$read.Path; SpkiSha256=$hash; KeyClass=$KeyClass }
}

function Get-PhaseAProductionAnchors {
  [CmdletBinding()]
  param(
    [string]$OperatorSigningMetadataPath = $script:ProductionOperatorSigningMetadataPath,
    [string]$OperatorSigningSpkiPath = $script:ProductionOperatorSigningSpkiPath,
    [string]$RecoveryEncryptionMetadataPath = $script:ProductionRecoveryEncryptionMetadataPath,
    [string]$RecoveryEncryptionSpkiPath = $script:ProductionRecoveryEncryptionSpkiPath,
    [switch]$DefinitionImport
  )
  if (-not $DefinitionImport -and (
      $OperatorSigningMetadataPath -ine $script:ProductionOperatorSigningMetadataPath -or
      $OperatorSigningSpkiPath -ine $script:ProductionOperatorSigningSpkiPath -or
      $RecoveryEncryptionMetadataPath -ine $script:ProductionRecoveryEncryptionMetadataPath -or
      $RecoveryEncryptionSpkiPath -ine $script:ProductionRecoveryEncryptionSpkiPath)) {
    throw 'Production anchor paths are fixed under config/phase-a.'
  }
  $signing = Import-PhaseAAnchorMetadata $OperatorSigningMetadataPath $OperatorSigningSpkiPath `
    'applypilot.phase-a.operator-receipt-signing' 'operator-signing-key.spki.der' 'operator-signing'
  $recovery = Import-PhaseAAnchorMetadata $RecoveryEncryptionMetadataPath $RecoveryEncryptionSpkiPath `
    'applypilot.phase-a.recovery-oaep-encryption' 'recovery-encryption-key.spki.der' 'recovery-encryption'
  if ($signing.SpkiSha256 -ceq $recovery.SpkiSha256) { throw 'Signing and recovery encryption anchors must be distinct.' }
  return [pscustomobject]@{ OperatorSigning=$signing; RecoveryEncryption=$recovery }
}

function Assert-PhaseAReceiptValue($Value, [string]$ExpectedReceiptType, $ExpectedBindings,
    [string]$ExpectedAuthorizedAfterManifestSha256) {
  if ($script:ReceiptTypes -cnotcontains $ExpectedReceiptType -or
      [string]$Value.receiptType -cne $ExpectedReceiptType) { throw 'Unexpected receipt type.' }
  Assert-PhaseAClosedFields $Value $script:ReceiptFieldsByType[$ExpectedReceiptType]
  if ($Value.schemaVersion -ne 1) { throw 'Receipt schemaVersion must equal one.' }
  Assert-PhaseAHexDigest ([string]$Value.operatorSigningKeySpkiSha256) 'operatorSigningKeySpkiSha256'
  if ($Value.PSObject.Properties.Name -contains 'createdAtUtc' -or $Value -is [Collections.IDictionary] -and $Value.Contains('createdAtUtc')) {
    Assert-PhaseAUtcSeconds ([string]$Value.createdAtUtc) 'createdAtUtc'
  }
  switch ($ExpectedReceiptType) {
    'applypilot.phase-a.runtime-source-approval' {
      if ([string]$Value.approvedCommit -cnotmatch $script:Hex40 -or [string]$Value.approvedTree -cnotmatch $script:Hex40) { throw 'Source approval Git bindings are invalid.' }
      Assert-PhaseAHexDigest ([string]$Value.planSha256) 'planSha256'; Assert-PhaseAHexDigest ([string]$Value.nonce) 'nonce'
      foreach ($reviewName in @('specReview','qualityReview')) {
        $review=$Value[$reviewName]; Assert-PhaseAClosedFields $review @('taskId','result')
        Assert-PhaseAUuid ([string]$review.taskId) "$reviewName.taskId"
        if ([string]$review.result -cne 'APPROVED') { throw 'Review result must be APPROVED.' }
      }
      if ($Value.criticalFileSha256 -isnot [Collections.IDictionary] -or $Value.criticalFileSha256.Count -eq 0) { throw 'criticalFileSha256 is invalid.' }
      foreach ($entry in $Value.criticalFileSha256.GetEnumerator()) { Assert-PhaseAHexDigest ([string]$entry.Value) "criticalFileSha256.$($entry.Key)" }
    }
    'applypilot.phase-a.evidence-adjudication' {
      foreach($name in @('sourceIdentityDigest','selectedBundleSha256','nonce')){Assert-PhaseAHexDigest ([string]$Value[$name]) $name}
      $items=@($Value.candidateBundleSha256);$sorted=@($items|Sort-Object -CaseSensitive -Unique)
      if($items.Count -eq 0 -or $items.Count -ne $sorted.Count -or (Compare-Object $items $sorted -SyncWindow 0) -or $items -cnotcontains $Value.selectedBundleSha256){throw 'Adjudication candidates are invalid.'}
      foreach($item in $items){Assert-PhaseAHexDigest ([string]$item) 'candidateBundleSha256'}
    }
    'applypilot.phase-a.credential-revocation' {
      if([string]$Value.approvedCommit -cnotmatch $script:Hex40){throw 'Revocation commit is invalid.'}
      foreach($name in @('credentialReferenceDigest','providerEvidenceSha256','machineIdentityDigest','nonce')){Assert-PhaseAHexDigest ([string]$Value[$name]) $name}
      if([string]$Value.providerClass -cnotin @('postgres','llm-api','review-api','other') -or [string]$Value.staleProbeResult -cne 'DENIED'){throw 'Revocation provider or stale probe result is invalid.'}
      Assert-PhaseAUtcSeconds ([string]$Value.revokedAtUtc) 'revokedAtUtc';Assert-PhaseAUtcSeconds ([string]$Value.staleProbeAtUtc) 'staleProbeAtUtc'
      if([DateTimeOffset]::Parse($Value.staleProbeAtUtc) -lt [DateTimeOffset]::Parse($Value.revokedAtUtc)){throw 'Stale probe predates revocation.'}
    }
    {$_ -like '*-authorization'} {
      if([string]$Value.approvedCommit -cnotmatch $script:Hex40){throw 'Authorization commit is invalid.'}
      foreach($name in @('operationId','targetIdentityDigest','beforeManifestSha256','expectedAfterManifestSha256','evidenceBundleSha256','credentialInventoryRoot','credentialRevocationSetRoot')){Assert-PhaseAHexDigest ([string]$Value[$name]) $name}
      Assert-PhaseASid ([string]$Value.operatorSid) 'operatorSid';$zero='0'*64
      $roots=@($Value.evidenceBundleSha256,$Value.credentialInventoryRoot,$Value.credentialRevocationSetRoot)
      if($ExpectedReceiptType -eq 'applypilot.phase-a.provisioning-cleanup-authorization' -and @($roots|Where-Object{$_ -cne $zero}).Count){throw 'Cleanup roots must be zero.'}
      if($ExpectedReceiptType -ne 'applypilot.phase-a.provisioning-cleanup-authorization' -and @($roots|Where-Object{$_ -ceq $zero}).Count){throw 'Legacy destruction roots must be nonzero.'}
    }
    {$_ -like '*-completion'} {
      if([string]$Value.approvedCommit -cnotmatch $script:Hex40 -or [string]$Value.result -cne 'COMPLETE'){throw 'Completion result or commit is invalid.'}
      foreach($name in @('operationId','authorizationReceiptSha256','actualAfterManifestSha256')){Assert-PhaseAHexDigest ([string]$Value[$name]) $name}
      Assert-PhaseAHexDigest $ExpectedAuthorizedAfterManifestSha256 'Authorized expected-after manifest'
      if([string]$Value.actualAfterManifestSha256 -cne $ExpectedAuthorizedAfterManifestSha256){throw 'Completion actual-after does not equal authorization expected-after.'}
    }
    'applypilot.phase-a.host-provisioning' {
      if([string]$Value.approvedCommit -cnotmatch $script:Hex40 -or [string]$Value.result -cne 'COMPLETE'){throw 'Host provisioning result or commit is invalid.'}
      foreach($name in @('sourceApprovalReceiptSha256','machineIdentityDigest','storeConfigSha256','storeTreeManifestSha256','recoveryKeySpkiSha256','operatorSidDigest')){Assert-PhaseAHexDigest ([string]$Value[$name]) $name}
      if([string]$Value.recoveryKeySpkiSha256 -ceq [string]$Value.operatorSigningKeySpkiSha256){throw 'Recovery and signing keys are not distinct.'}
    }
  }
  if($ExpectedBindings -isnot [Collections.IDictionary]){throw 'Caller-supplied expected bindings are mandatory.'}
  Assert-PhaseAClosedFields $ExpectedBindings $script:ReceiptFieldsByType[$ExpectedReceiptType]
  $actualBytes=ConvertTo-PhaseACanonicalJsonBytes $Value;$expectedBytes=ConvertTo-PhaseACanonicalJsonBytes $ExpectedBindings
  if(-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($actualBytes,$expectedBytes)){throw 'Receipt differs from caller-supplied expected bindings.'}
}

function Test-PhaseASignedReceiptCore {
  param($Receipt,$SignatureRead,[string]$OperatorSigningSpkiPath,[string]$ExpectedOperatorSigningKeySpkiSha256,
    [string]$ExpectedReceiptType,$ExpectedBindings,[string]$ExpectedAuthorizedAfterManifestSha256,[scriptblock]$BeforeSpkiRevalidation)
  if((Split-Path -Parent $Receipt.Path) -ine (Split-Path -Parent $SignatureRead.Path)){throw 'Receipt and signature must be adjacent.'}
  if([IO.Path]::GetFileName($Receipt.Path) -cne "$($Receipt.Sha256).json" -or [IO.Path]::GetFileName($SignatureRead.Path) -cne "$($Receipt.Sha256).sig"){throw 'Receipt pair names do not match canonical content.'}
  Assert-PhaseAReceiptValue $Receipt.Value $ExpectedReceiptType $ExpectedBindings $ExpectedAuthorizedAfterManifestSha256
  $spkiFull=Assert-PhaseALocalNtfsPath $OperatorSigningSpkiPath;$root=Split-Path -Parent $spkiFull
  $handle=Open-PhaseAValidatedFile $spkiFull Read $root ([IO.Path]::GetFileName($spkiFull));$rsa=$null
  try{
    $read=Read-PhaseABytesFromHeldHandle $handle $spkiFull;if($BeforeSpkiRevalidation){& $BeforeSpkiRevalidation $spkiFull}
    $rsa=Import-PhaseAOperatorSigningSpkiBytes $read.Bytes $ExpectedOperatorSigningKeySpkiSha256
    if([string]$Receipt.Value.operatorSigningKeySpkiSha256 -cne $ExpectedOperatorSigningKeySpkiSha256){throw 'Receipt operator signing key binding is wrong.'}
    Assert-PhaseAFileIdentity $handle $read.Identity
    if(-not [ApplyPilot.PhaseA.EvidenceNative]::VerifyPssSha256ExactSalt($rsa,$Receipt.Bytes,$SignatureRead.Bytes)){throw 'Receipt signature or PSS salt is invalid.'}
    Assert-PhaseAFileIdentity $handle $read.Identity;return $true
  }finally{if($rsa){$rsa.Dispose()};$handle.Dispose()}
}

function Test-PhaseASignedReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,[Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][Alias('SigningSpkiPath')][string]$OperatorSigningSpkiPath,
    [Parameter(Mandatory)][Alias('ExpectedSigningSpkiSha256')][string]$ExpectedOperatorSigningKeySpkiSha256,
    [Parameter(Mandatory)][string]$ExpectedReceiptType,[Parameter(Mandatory)]$ExpectedBindings,
    [string]$ExpectedAuthorizedAfterManifestSha256,[string]$ProtectedReceiptFileOwnerSid,
    [scriptblock]$BeforeSpkiRevalidation,[switch]$DefinitionImport)
  if($BeforeSpkiRevalidation -and -not $DefinitionImport){throw 'SPKI race injection requires DefinitionImport.'}
  $args=@{OperatorSigningSpkiPath=$OperatorSigningSpkiPath;ExpectedOperatorSigningKeySpkiSha256=$ExpectedOperatorSigningKeySpkiSha256;
    ExpectedReceiptType=$ExpectedReceiptType;ExpectedBindings=$ExpectedBindings;ExpectedAuthorizedAfterManifestSha256=$ExpectedAuthorizedAfterManifestSha256;BeforeSpkiRevalidation=$BeforeSpkiRevalidation}
  if($ProtectedReceiptFileOwnerSid){$pair=Open-PhaseAProtectedReceiptPair $ReceiptPath $SignaturePath $ProtectedReceiptFileOwnerSid;try{$args.Receipt=$pair.Receipt;$args.SignatureRead=$pair.Signature;$result=Test-PhaseASignedReceiptCore @args;Assert-PhaseAProtectedReceiptPairIdentity $pair;return $result}finally{Close-PhaseAProtectedReceiptPair $pair}}
  $args.Receipt=Read-PhaseACanonicalJson $ReceiptPath;$args.SignatureRead=Read-PhaseAValidatedBytes $SignaturePath
  return Test-PhaseASignedReceiptCore @args
}

function Write-PhaseACreateNew([string]$Path, [byte[]]$Bytes, [string]$OperatorSid) {
  $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try { $stream.Write($Bytes); $stream.Flush($true) } finally { $stream.Dispose() }
  Set-PhaseAProtectedAcl -Path $Path -OperatorSid $OperatorSid -File
}

function Open-PhaseAExpectedProtectedFile {
  param([string]$Path,[string]$Root,[string]$OwnerSid,[byte[]]$ExpectedBytes,[string]$Access='Read')
  $handle=Open-PhaseAValidatedFile $Path $Access $Root ([IO.Path]::GetFileName($Path))
  try{
    Assert-PhaseAProtectedFileHandleAcl $handle $OwnerSid
    $read=Read-PhaseABytesFromHeldHandle $handle $Path
    if(-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals($read.Bytes,$ExpectedBytes)){throw 'Receipt publication file bytes do not match.'}
    return [pscustomobject]@{Handle=$handle;Read=$read}
  }catch{$handle.Dispose();throw}
}

function Install-PhaseASignedReceipt {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$ReceiptPath,[Parameter(Mandatory)][string]$SignaturePath,
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][Alias('SigningSpkiPath')][string]$OperatorSigningSpkiPath,
    [Parameter(Mandatory)][Alias('ExpectedSigningSpkiSha256')][string]$ExpectedOperatorSigningKeySpkiSha256,
    [Parameter(Mandatory)][string]$ExpectedReceiptType,[Parameter(Mandatory)]$ExpectedBindings,
    [string]$ExpectedAuthorizedAfterManifestSha256,[switch]$Bootstrap,[switch]$DefinitionImport,
    [scriptblock]$DefinitionBundleAuthenticator,[scriptblock]$BeforeFinalPairRevalidation,
    [ValidateSet('after-receipt-stage','after-signature-stage','after-receipt-rename','after-signature-rename','before-pair-revalidation')][string]$CrashAfter
  )
  if($BeforeFinalPairRevalidation -and -not $DefinitionImport){throw 'Race injection requires DefinitionImport.'}
  if($DefinitionBundleAuthenticator -and -not $DefinitionImport){throw 'Bundle authenticator override requires DefinitionImport.'}
  $operator=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $root=Assert-PhaseALocalNtfsPath $StoreRoot
  if(-not $DefinitionImport){
    $expectedRoot=if($Bootstrap){[IO.Path]::Combine([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData),'ApplyPilot','phase-a-evidence','bootstrap-operations')}else{$script:ProductionStoreRoot}
    if($root -ine $expectedRoot){throw 'Receipt destination identity is not exact.'}
  }
  $leaf=if($ExpectedReceiptType -ceq 'applypilot.phase-a.evidence-adjudication'){'adjudications'}else{'operations'}
  if($Bootstrap){$destination=$root}else{$destination=[IO.Path]::Combine($root,$leaf)}
  Assert-PhaseAProtectedAcl $destination $operator
  $source=Open-PhaseAProtectedReceiptPair $ReceiptPath $SignaturePath $operator
  $finalReceipt=$null;$finalSignature=$null
  try{
    $validation=@{Receipt=$source.Receipt;SignatureRead=$source.Signature;OperatorSigningSpkiPath=$OperatorSigningSpkiPath;
      ExpectedOperatorSigningKeySpkiSha256=$ExpectedOperatorSigningKeySpkiSha256;ExpectedReceiptType=$ExpectedReceiptType;
      ExpectedBindings=$ExpectedBindings;ExpectedAuthorizedAfterManifestSha256=$ExpectedAuthorizedAfterManifestSha256}
    $null=Test-PhaseASignedReceiptCore @validation
    if($ExpectedReceiptType -ceq 'applypilot.phase-a.evidence-adjudication'){
      Assert-PhaseAAdjudicationCurrentCandidates $source.Receipt.Value $root $operator $DefinitionBundleAuthenticator -DefinitionImport:$DefinitionImport
    }
    Assert-PhaseAProtectedReceiptPairIdentity $source
    $hash=$source.Receipt.Sha256
    $receiptFinalPath=[IO.Path]::Combine($destination,"$hash.json")
    $signatureFinalPath=[IO.Path]::Combine($destination,"$hash.sig")
    if($source.Receipt.Path -ieq $receiptFinalPath -and $source.Signature.Path -ieq $signatureFinalPath){
      return [pscustomobject]@{ReceiptPath=$receiptFinalPath;SignaturePath=$signatureFinalPath;Existing=$true}
    }
    $receiptStage=[IO.Path]::Combine($destination,".$hash.receipt-stage")
    $signatureStage=[IO.Path]::Combine($destination,".$hash.signature-stage")
    foreach($part in @(
      [pscustomobject]@{Final=$receiptFinalPath;Stage=$receiptStage;Bytes=$source.Receipt.Bytes;Kind='receipt'},
      [pscustomobject]@{Final=$signatureFinalPath;Stage=$signatureStage;Bytes=$source.Signature.Bytes;Kind='signature'})){
      $published=$null
      if(Test-Path -LiteralPath $part.Final){
        $published=Open-PhaseAExpectedProtectedFile $part.Final $destination $operator $part.Bytes Read
      }else{
        if(-not (Test-Path -LiteralPath $part.Stage)){Write-PhaseACreateNew $part.Stage $part.Bytes $operator}
        $stage=$null
        try{
          $stage=Open-PhaseAExpectedProtectedFile $part.Stage $destination $operator $part.Bytes ReadWriteDelete
          if($part.Kind -eq 'receipt' -and $CrashAfter -eq 'after-receipt-stage'){throw 'Injected crash after receipt stage.'}
          if($part.Kind -eq 'signature' -and $CrashAfter -eq 'after-signature-stage'){throw 'Injected crash after signature stage.'}
          $beforeIdentity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($stage.Handle.FileHandle)
          [ApplyPilot.PhaseA.EvidenceNative]::RenameFileNoReplace($stage.Handle.FileHandle,$part.Final)
          $afterIdentity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($stage.Handle.FileHandle)
          if($afterIdentity.VolumeSerialNumber -ne $beforeIdentity.VolumeSerialNumber -or $afterIdentity.FileId -cne $beforeIdentity.FileId){throw 'Receipt publication identity changed during rename.'}
          $stage.Read.Path=$part.Final;$stage.Read.Identity=$afterIdentity
          $stage|Add-Member -NotePropertyName NativeIdentity -NotePropertyValue $true
          $published=$stage;$stage=$null
        }finally{if($stage){$stage.Handle.Dispose()}}
      }
      if($part.Kind -eq 'receipt'){
        $finalReceipt=$published
        if($CrashAfter -eq 'after-receipt-rename'){throw 'Injected crash after receipt rename.'}
      }else{
        $finalSignature=$published
        if($CrashAfter -eq 'after-signature-rename'){throw 'Injected crash after signature rename.'}
      }
    }
    if($CrashAfter -eq 'before-pair-revalidation'){throw 'Injected crash before pair revalidation.'}
    if($BeforeFinalPairRevalidation){& $BeforeFinalPairRevalidation ([pscustomobject]@{ReceiptPath=$receiptFinalPath;SignaturePath=$signatureFinalPath})}
    foreach($published in @($finalReceipt,$finalSignature)){
      if($published.PSObject.Properties.Name -contains 'NativeIdentity'){
        [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($published.Handle.FileHandle,$published.Read.Identity)
      }else{Assert-PhaseAFileIdentity $published.Handle $published.Read.Identity}
    }
    Assert-PhaseAProtectedFileHandleAcl $finalReceipt.Handle $operator
    Assert-PhaseAProtectedFileHandleAcl $finalSignature.Handle $operator
    $finalReceiptValue=ConvertFrom-PhaseACanonicalJsonRead ([pscustomobject]@{Bytes=$finalReceipt.Read.Bytes;Identity=$finalReceipt.Read.Identity;Path=$receiptFinalPath})
    $validation.Receipt=$finalReceiptValue;$validation.SignatureRead=[pscustomobject]@{Bytes=$finalSignature.Read.Bytes;Identity=$finalSignature.Read.Identity;Path=$signatureFinalPath}
    $null=Test-PhaseASignedReceiptCore @validation
    if($ExpectedReceiptType -ceq 'applypilot.phase-a.evidence-adjudication'){
      Assert-PhaseAAdjudicationCurrentCandidates $finalReceiptValue.Value $root $operator $DefinitionBundleAuthenticator -DefinitionImport:$DefinitionImport
    }
    Assert-PhaseAProtectedReceiptPairIdentity $source
    foreach($stagePath in @($receiptStage,$signatureStage)){
      if(Test-Path -LiteralPath $stagePath){$cleanup=Open-PhaseAValidatedFile $stagePath ReadWriteDelete $destination ([IO.Path]::GetFileName($stagePath));try{Set-PhaseAFileDeletionDisposition $cleanup}finally{$cleanup.Dispose()}}
    }
    return [pscustomobject]@{ReceiptPath=$receiptFinalPath;SignaturePath=$signatureFinalPath;Existing=$false}
  }finally{
    if($finalSignature){$finalSignature.Handle.Dispose()};if($finalReceipt){$finalReceipt.Handle.Dispose()}
    Close-PhaseAProtectedReceiptPair $source
  }
}

function Get-PhaseAReceiptInventory {
  [CmdletBinding()]
  param([Parameter(Mandatory,Position=0)][string]$Root,
    [Parameter(Mandatory,Position=1)][string]$OperatorSid,[switch]$HoldPairs)
  $inventory = [Collections.Generic.List[object]]::new()
  try{
    foreach ($leaf in @('bundles','adjudications','operations')) {
    $directory = [IO.Path]::Combine($Root, $leaf)
    Assert-PhaseAProtectedAcl $directory $OperatorSid
    $files = @(Get-ChildItem -LiteralPath $directory -Force)
    if (@($files | Where-Object { $_.PSIsContainer }).Count -ne 0) { throw 'Receipt directories cannot contain nested directories.' }
    foreach ($file in $files) {
      if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Receipt tree contains a reparse object.' }
      Assert-PhaseAProtectedAcl $file.FullName $OperatorSid -File
      if ($leaf -eq 'bundles') {
        $isFinal = $file.Name -cmatch '^(?<source>[0-9a-f]{64})-(?<preimage>[0-9a-f]{64})\.apeb$'
        $isStaging = $file.Name -cmatch '^\.staging-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        if (-not $isFinal -and -not $isStaging) {
          throw 'Bundles accepts only exact final .apeb names or exact staging residue names.'
        }
        $handle=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($file.FullName,$false)
        try{
          $identity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($handle)
          [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($handle,$identity)
        }finally{$handle.Dispose()}
      } elseif ($file.Name -cnotmatch '^(?<hash>[0-9a-f]{64})\.(?<extension>json|sig)$') {
        throw 'Receipt tree contains an unexpected file.'
      }
    }
    if($leaf -eq 'bundles'){continue}
    foreach ($group in @($files | Group-Object { if ($_.Name -cmatch '^([0-9a-f]{64})\.(json|sig)$') { $Matches[1] } else { $_.Name } })) {
      $names = @($group.Group.Name | Sort-Object)
      if ($names.Count -ne 2 -or $names[0] -cne "$($group.Name).json" -or $names[1] -cne "$($group.Name).sig") {
        throw 'Receipt tree contains an orphan or duplicate pair.'
      }
      $jsonPath = [IO.Path]::Combine($directory, "$($group.Name).json")
      $signaturePath=[IO.Path]::Combine($directory, "$($group.Name).sig")
      $pair=Open-PhaseAProtectedReceiptPair $jsonPath $signaturePath $OperatorSid
      if ($pair.Receipt.Sha256 -cne $group.Name) {Close-PhaseAProtectedReceiptPair $pair;throw 'Receipt inventory filename hash is wrong.' }
      $type=[string]$pair.Receipt.Value.receiptType
      if($leaf -eq 'adjudications' -and $type -cne 'applypilot.phase-a.evidence-adjudication'){
        Close-PhaseAProtectedReceiptPair $pair
        throw 'Adjudications accepts only adjudication receipts.'
      }
      if($leaf -eq 'operations' -and $type -eq 'applypilot.phase-a.evidence-adjudication'){
        Close-PhaseAProtectedReceiptPair $pair
        throw 'Operations cannot contain adjudication receipts.'
      }
      if($script:ReceiptTypes -cnotcontains $type){Close-PhaseAProtectedReceiptPair $pair;throw 'Receipt inventory contains an unsupported type.'}
      $inventory.Add([pscustomobject]@{
        Hash=$group.Name; Leaf=$leaf; JsonPath=$jsonPath;
        SignaturePath=$signaturePath; Value=$pair.Receipt.Value; Pair=$pair
      })
    }
    }
    $result=@($inventory)
    if(-not $HoldPairs){foreach($item in $result){if($item.Pair){Close-PhaseAProtectedReceiptPair $item.Pair;$item.Pair=$null}}}
    return $result
  }catch{
    foreach($item in @($inventory)){if($item.Pair){Close-PhaseAProtectedReceiptPair $item.Pair}}
    throw
  }
}

function Get-PhaseAAuthenticatedBundleCandidates {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$StoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$SourceIdentityDigest,
    [scriptblock]$DefinitionBundleAuthenticator,
    [switch]$DefinitionImport
  )
  Assert-PhaseAHexDigest $SourceIdentityDigest 'sourceIdentityDigest'
  if($DefinitionBundleAuthenticator -and -not $DefinitionImport){throw 'Bundle authenticator override requires DefinitionImport.'}
  $root=Assert-PhaseALocalNtfsPath $StoreRoot
  $operator=Assert-PhaseACurrentOperator $CanonicalOperatorSid
  $directory=[IO.Path]::Combine($root,'bundles')
  Assert-PhaseAProtectedAcl $directory $operator
  $result=[Collections.Generic.List[string]]::new()
  $finals=[Collections.Generic.List[object]]::new()
  foreach($file in @(Get-ChildItem -LiteralPath $directory -Force | Sort-Object Name)) {
    if($file.PSIsContainer){throw 'Bundles cannot contain directories.'}
    Assert-PhaseAProtectedAcl $file.FullName $operator -File
    if($file.Name -cmatch '^\.staging-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'){continue}
    if($file.Name -cnotmatch '^(?<source>[0-9a-f]{64})-(?<preimage>[0-9a-f]{64})\.apeb$'){
      throw 'Bundles contains an unexpected object.'
    }
    $finals.Add([pscustomobject]@{File=$file;Source=$Matches.source;Preimage=$Matches.preimage})
  }
  if($finals.Count -eq 0){return @()}
  $authenticator=if($DefinitionImport){$DefinitionBundleAuthenticator}else{$script:PhaseABundleAuthenticator}
  if(-not $authenticator){throw 'Authenticated bundle candidate enumeration requires the Task4 bundle authenticator.'}
  foreach($final in $finals){
    if($final.Source -cne $SourceIdentityDigest){continue}
    $file=$final.File;$expectedPreimage=$final.Preimage
    $handle=[ApplyPilot.PhaseA.EvidenceNative]::OpenManifestObject($file.FullName,$false)
    try {
      $identity=[ApplyPilot.PhaseA.EvidenceNative]::GetRawFileIdentity($handle)
      $authenticated=& $authenticator ([pscustomobject]@{
        Handle=$handle;FileName=$file.Name;SourceIdentityDigest=$SourceIdentityDigest;
        PreimageSha256=$expectedPreimage;Identity=$identity
      })
      Assert-PhaseAClosedFields $authenticated @('sourceIdentityDigest','preimageSha256','candidateBundleSha256')
      foreach($digest in @($authenticated.sourceIdentityDigest,$authenticated.preimageSha256,$authenticated.candidateBundleSha256)){
        Assert-PhaseAHexDigest ([string]$digest) 'Authenticated bundle digest'
      }
      if([string]$authenticated.sourceIdentityDigest -cne $SourceIdentityDigest -or
          [string]$authenticated.preimageSha256 -cne $expectedPreimage){throw 'Bundle authenticator returned mismatched identity material.'}
      [ApplyPilot.PhaseA.EvidenceNative]::AssertRawFileIdentity($handle,$identity)
      $result.Add([string]$authenticated.candidateBundleSha256)
    } finally { $handle.Dispose() }
  }
  $sorted=@($result | Sort-Object -CaseSensitive -Unique)
  if($sorted.Count -ne $result.Count){throw 'Authenticated bundle candidates are not unique.'}
  return $sorted
}

function Assert-PhaseAAdjudicationCurrentCandidates {
  param($ReceiptValue,[string]$StoreRoot,[string]$CanonicalOperatorSid,
    [scriptblock]$DefinitionBundleAuthenticator,[switch]$DefinitionImport)
  if([string]$ReceiptValue.receiptType -cne 'applypilot.phase-a.evidence-adjudication'){throw 'Current candidate validation requires an adjudication receipt.'}
  $arguments=@{StoreRoot=$StoreRoot;CanonicalOperatorSid=$CanonicalOperatorSid;SourceIdentityDigest=[string]$ReceiptValue.sourceIdentityDigest}
  if($DefinitionImport){$arguments.DefinitionImport=$true;$arguments.DefinitionBundleAuthenticator=$DefinitionBundleAuthenticator}
  $current=@(Get-PhaseAAuthenticatedBundleCandidates @arguments)
  $signed=@($ReceiptValue.candidateBundleSha256)
  if($current.Count -eq 0 -or $current.Count -ne $signed.Count -or (Compare-Object $current $signed -SyncWindow 0)){
    throw 'Signed adjudication candidates do not equal the current authenticated store candidate set.'
  }
  if($current -cnotcontains [string]$ReceiptValue.selectedBundleSha256){throw 'Selected bundle is not currently authenticated.'}
}

function Assert-PhaseAEvidenceStore {
  [CmdletBinding()]
  param(
    [string]$StoreRoot=$script:ProductionStoreRoot,
    [Parameter(Mandatory)][string]$CanonicalOperatorSid,
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][Collections.IDictionary]$ExpectedReceiptBindingsByHash,
    [string]$OperatorSigningMetadataPath=$script:ProductionOperatorSigningMetadataPath,
    [string]$OperatorSigningSpkiPath=$script:ProductionOperatorSigningSpkiPath,
    [string]$RecoveryEncryptionMetadataPath=$script:ProductionRecoveryEncryptionMetadataPath,
    [string]$RecoveryEncryptionSpkiPath=$script:ProductionRecoveryEncryptionSpkiPath,
    [string]$ExpectedTargetIdentityDigest,[string]$ExpectedMachineIdentityDigest,
    [string]$CanonicalStoreRoot,[string]$AncestorBoundary,[scriptblock]$DefinitionBundleAuthenticator,[switch]$DefinitionImport
  )
  $operator=Assert-PhaseACurrentOperator $CanonicalOperatorSid
  $root=Assert-PhaseALocalNtfsPath $StoreRoot
  if(-not $DefinitionImport -and $root -ine $script:ProductionStoreRoot){throw 'Production validation requires the exact native ProgramData evidence root.'}
  if($AncestorBoundary -and -not $DefinitionImport){throw 'Ancestor boundary override requires DefinitionImport.'}
  if($CanonicalStoreRoot -and -not $DefinitionImport){throw 'Canonical store root override requires DefinitionImport.'}
  if($DefinitionBundleAuthenticator -and -not $DefinitionImport){throw 'Bundle authenticator override requires DefinitionImport.'}
  $anchorArgs=@{OperatorSigningMetadataPath=$OperatorSigningMetadataPath;OperatorSigningSpkiPath=$OperatorSigningSpkiPath;
    RecoveryEncryptionMetadataPath=$RecoveryEncryptionMetadataPath;RecoveryEncryptionSpkiPath=$RecoveryEncryptionSpkiPath}
  if($DefinitionImport){$anchorArgs.DefinitionImport=$true}
  $anchors=Get-PhaseAProductionAnchors @anchorArgs
  Assert-PhaseAAncestorDeleteChild $root $operator $AncestorBoundary
  Assert-PhaseAProtectedAcl $root $operator
  $objects=@(Get-ChildItem -LiteralPath $root -Force|Sort-Object Name)
  if(($objects.Name -join ',') -cne 'adjudications,bundles,operations,store.json'){throw 'Evidence root object set is not exact.'}
  foreach($leaf in @('adjudications','bundles','operations')){
    $item=Get-Item -LiteralPath ([IO.Path]::Combine($root,$leaf)) -Force
    if(-not $item.PSIsContainer -or ($item.Attributes-band [IO.FileAttributes]::ReparsePoint)){throw 'Required evidence object is not a regular directory.'}
  }
  $configHandle=$null;$inventory=@()
  try{
  $tree=Get-PhaseADirectoryManifest $root -CanonicalRootPath $CanonicalStoreRoot
  $configPath=[IO.Path]::Combine($root,'store.json')
  $configHandle=Open-PhaseAValidatedFile $configPath Read $root 'store.json'
  Assert-PhaseAProtectedFileHandleAcl $configHandle $operator
  $configRead=Read-PhaseABytesFromHeldHandle $configHandle $configPath
  $config=ConvertFrom-PhaseACanonicalJsonRead $configRead
  Assert-PhaseAClosedFields $config.Value @('schemaVersion','storeType','approvedCommit','targetIdentityDigest','operatorSidDigest',
    'machineIdentityDigest','securityDescriptorSha256','operatorSigningKeySpkiSha256','recoveryKeySpkiSha256',
    'sourceApprovalReceiptSha256')
  foreach($name in @('targetIdentityDigest','operatorSidDigest','machineIdentityDigest','securityDescriptorSha256','operatorSigningKeySpkiSha256','recoveryKeySpkiSha256','sourceApprovalReceiptSha256')){Assert-PhaseAHexDigest ([string]$config.Value[$name]) $name}
  if(($ExpectedTargetIdentityDigest -or $ExpectedMachineIdentityDigest) -and -not $DefinitionImport){throw 'Identity overrides require DefinitionImport.'}
  $target=if($ExpectedTargetIdentityDigest){$ExpectedTargetIdentityDigest}else{Get-PhaseATargetDigest $root}
  $machine=if($ExpectedMachineIdentityDigest){$ExpectedMachineIdentityDigest}else{Get-PhaseAMachineDigest}
  if($config.Value.schemaVersion-ne 1 -or $config.Value.storeType-cne 'applypilot.phase-a.evidence-store' -or
      $config.Value.approvedCommit-cne $ExpectedCommit -or $config.Value.targetIdentityDigest-cne $target -or
      $config.Value.operatorSidDigest-cne (Get-PhaseAOperatorSidDigest $operator) -or
      $config.Value.machineIdentityDigest-cne $machine -or
      $config.Value.securityDescriptorSha256-cne (Get-PhaseASecurityDescriptorHash $root) -or
      $config.Value.operatorSigningKeySpkiSha256-cne $anchors.OperatorSigning.SpkiSha256 -or
      $config.Value.recoveryKeySpkiSha256-cne $anchors.RecoveryEncryption.SpkiSha256){throw 'Evidence store configuration is invalid.'}
  $inventory=@(Get-PhaseAReceiptInventory $root $operator -HoldPairs)
  $sources=@($inventory|Where-Object{$_.Value.receiptType-ceq 'applypilot.phase-a.runtime-source-approval'})
  $hosts=@($inventory|Where-Object{$_.Value.receiptType-ceq 'applypilot.phase-a.host-provisioning'})
  if($sources.Count-ne 1 -or $hosts.Count-ne 1 -or $sources[0].Leaf-cne 'operations' -or $hosts[0].Leaf-cne 'operations'){throw 'Store requires one source approval and one host provisioning pair.'}
  if($config.Value.sourceApprovalReceiptSha256-cne $sources[0].Hash){throw 'Store source approval binding is wrong.'}
  foreach($item in $inventory){
    if(-not $ExpectedReceiptBindingsByHash.Contains($item.Hash)){throw 'Caller authority is missing expected receipt bindings.'}
    $type=[string]$item.Value.receiptType
    $verify=@{Receipt=$item.Pair.Receipt;SignatureRead=$item.Pair.Signature;OperatorSigningSpkiPath=$anchors.OperatorSigning.SpkiPath;
      ExpectedOperatorSigningKeySpkiSha256=$anchors.OperatorSigning.SpkiSha256;ExpectedReceiptType=$type;
      ExpectedBindings=$ExpectedReceiptBindingsByHash[$item.Hash]}
    if($type -like '*-completion'){
      $authorizationHash=[string]$item.Value.authorizationReceiptSha256
      if(-not $ExpectedReceiptBindingsByHash.Contains($authorizationHash)){throw 'Completion authorization bindings are unavailable.'}
      $verify.ExpectedAuthorizedAfterManifestSha256=[string]$ExpectedReceiptBindingsByHash[$authorizationHash].expectedAfterManifestSha256
    }
    $null=Test-PhaseASignedReceiptCore @verify
    if($type -ceq 'applypilot.phase-a.evidence-adjudication'){
      Assert-PhaseAAdjudicationCurrentCandidates $item.Value $root $operator $DefinitionBundleAuthenticator -DefinitionImport:$DefinitionImport
    }
    Assert-PhaseAProtectedReceiptPairIdentity $item.Pair
  }
  $hostReceiptValue=$hosts[0].Value
  $hostRelativeDigests=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  foreach($relative in @("operations\$($hosts[0].Hash).json","operations\$($hosts[0].Hash).sig")){
    $null=$hostRelativeDigests.Add((Get-PhaseARelativePathDigest $relative))
  }
  $preHost=[ordered]@{schemaVersion=1;manifestType='applypilot.phase-a.directory-manifest';baseRootIdentityDigest=$tree.baseRootIdentityDigest;
    entries=@($tree.entries|Where-Object{-not $hostRelativeDigests.Contains([string]$_.relativePathDigest)})}
  $preHostHash=Get-PhaseASha256 (ConvertTo-PhaseACanonicalJsonBytes $preHost)
  if($hostReceiptValue.approvedCommit-cne $ExpectedCommit -or $hostReceiptValue.sourceApprovalReceiptSha256-cne $sources[0].Hash -or
      $hostReceiptValue.machineIdentityDigest-cne $machine -or $hostReceiptValue.storeConfigSha256-cne $config.Sha256 -or
      $hostReceiptValue.storeTreeManifestSha256-cne $preHostHash -or
      $hostReceiptValue.recoveryKeySpkiSha256-cne $anchors.RecoveryEncryption.SpkiSha256 -or
      $hostReceiptValue.operatorSidDigest-cne $config.Value.operatorSidDigest){throw 'Host provisioning receipt does not bind the validated store.'}
  Assert-PhaseAFileIdentity $configHandle $configRead.Identity
  Assert-PhaseAProtectedFileHandleAcl $configHandle $operator
  return [pscustomobject]@{Valid=$true;StoreRoot=$root;StoreConfigSha256=$config.Sha256;TargetIdentityDigest=$target;HostProvisioningReceiptSha256=$hosts[0].Hash}
  }finally{
    foreach($inventoryItem in @($inventory)){if($inventoryItem.Pair){Close-PhaseAProtectedReceiptPair $inventoryItem.Pair}}
    if($configHandle){$configHandle.Dispose()}
  }
}

Export-ModuleMember -Function @(
  'Assert-PhaseAEvidenceStore', 'ConvertTo-PhaseACanonicalJsonBytes',
  'Get-PhaseADirectoryManifest', 'Get-PhaseAProductionAnchors',
  'Get-PhaseAAuthenticatedBundleCandidates',
  'Get-PhaseAMachineDigest', 'Get-PhaseAOperatorSidDigest',
  'Get-PhaseASecurityDescriptorHash', 'Get-PhaseATargetDigest',
  'Install-PhaseASignedReceipt', 'Test-PhaseASignedReceipt'
)
