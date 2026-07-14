Set-StrictMode -Version Latest

if (-not ('ApplyPilot.PhaseA.WindowsFile' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

namespace ApplyPilot.PhaseA
{
    public sealed class FileIdentity
    {
        public ulong VolumeSerialNumber { get; set; }
        public string FileId { get; set; }
        public uint NumberOfLinks { get; set; }
        public string FinalPath { get; set; }
    }

    public sealed class FileIdentityMaterial
    {
        private readonly byte[] fileId;

        internal FileIdentityMaterial(ulong volumeSerialNumber, byte[] fileId, string volumeGuidPath)
        {
            VolumeSerialNumber = volumeSerialNumber;
            this.fileId = (byte[])fileId.Clone();
            VolumeGuidPath = volumeGuidPath;
        }

        public ulong VolumeSerialNumber { get; private set; }
        public byte[] FileId { get { return (byte[])fileId.Clone(); } }
        public string VolumeGuidPath { get; private set; }
    }

    internal enum ValidatedAccess
    {
        Directory,
        Read,
        ReadWrite,
        ReadWriteDelete
    }

    public sealed class ValidatedHandle : IDisposable
    {
        private SafeFileHandle fileHandle;
        private List<SafeFileHandle> leases;

        internal ValidatedHandle(
            SafeFileHandle fileHandle,
            ValidatedAccess access,
            string authorizedRoot,
            string validatedPath,
            bool isDirectory,
            List<SafeFileHandle> leases,
            FileIdentity identity,
            string authorizedBasename,
            string authorizedBasenamePattern)
        {
            this.fileHandle = fileHandle;
            Access = access;
            AuthorizedRoot = authorizedRoot;
            ValidatedPath = validatedPath;
            IsDirectory = isDirectory;
            this.leases = leases;
            Identity = identity;
            AuthorizedBasename = authorizedBasename;
            AuthorizedBasenamePattern = authorizedBasenamePattern;
        }

        internal ValidatedAccess Access { get; private set; }
        internal string AuthorizedRoot { get; private set; }
        internal string ValidatedPath { get; set; }
        internal bool IsDirectory { get; private set; }
        internal FileIdentity Identity { get; set; }
        internal string AuthorizedBasename { get; private set; }
        internal string AuthorizedBasenamePattern { get; private set; }

        public SafeFileHandle FileHandle
        {
            get
            {
                EnsureOpen();
                return fileHandle;
            }
        }

        public bool IsDisposed
        {
            get { return fileHandle == null || fileHandle.IsClosed; }
        }

        internal void EnsureOpen()
        {
            if (IsDisposed || fileHandle.IsInvalid)
            {
                throw new InvalidOperationException("Handle is not an open module-validated handle.");
            }
        }

        public void Dispose()
        {
            SafeFileHandle target;
            List<SafeFileHandle> ancestors;
            lock (this)
            {
                target = fileHandle;
                ancestors = leases;
                fileHandle = null;
                leases = null;
            }
            if (target == null)
            {
                return;
            }
            try
            {
                target.Dispose();
            }
            finally
            {
                if (ancestors != null)
                {
                    for (int index = ancestors.Count - 1; index >= 0; index--)
                    {
                        ancestors[index].Dispose();
                    }
                    ancestors.Clear();
                }
            }
            GC.SuppressFinalize(this);
        }

        ~ValidatedHandle()
        {
            Dispose();
        }
    }

    internal static class NativeMethods
    {
        internal const uint GenericRead = 0x80000000;
        internal const uint GenericWrite = 0x40000000;
        internal const uint Delete = 0x00010000;
        internal const uint CreateNew = 1;
        internal const uint OpenExisting = 3;
        internal const uint FileFlagBackupSemantics = 0x02000000;
        internal const uint FileFlagOpenReparsePoint = 0x00200000;
        internal const uint FileAttributeDirectory = 0x00000010;
        internal const uint FileAttributeReparsePoint = 0x00000400;
        internal const uint FileAttributeNormal = 0x00000080;
        internal const int FileDispositionInfo = 4;
        internal const int FileRenameInfo = 3;
        internal const uint DriveFixed = 3;
        internal const uint VolumeNameGuid = 0x1;
        internal const uint OwnerSecurityInformation = 0x00000001;
        internal const uint GroupSecurityInformation = 0x00000002;
        internal const uint DaclSecurityInformation = 0x00000004;
        internal const int SeFileObject = 1;
        internal static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        internal struct FileIdInformation
        {
            internal ulong VolumeSerialNumber;
            internal ulong FileIdLow;
            internal ulong FileIdHigh;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct FileStandardInformation
        {
            internal long AllocationSize;
            internal long EndOfFile;
            internal uint NumberOfLinks;
            [MarshalAs(UnmanagedType.U1)]
            internal bool DeletePending;
            [MarshalAs(UnmanagedType.U1)]
            internal bool Directory;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct FileAttributeTagInformation
        {
            internal uint FileAttributes;
            internal uint ReparseTag;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct FileDispositionInformation
        {
            [MarshalAs(UnmanagedType.U1)]
            internal bool DeleteFile;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct SecurityAttributes
        {
            internal uint Length;
            internal IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            internal bool InheritHandle;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateFileW(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);

        [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateFileWithSecurityW(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            ref SecurityAttributes securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileAttributeTagInformationByHandleEx(
            SafeFileHandle file,
            int informationClass,
            out FileAttributeTagInformation information,
            uint bufferSize);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileIdInformationByHandleEx(
            SafeFileHandle file,
            int informationClass,
            out FileIdInformation information,
            uint bufferSize);

        [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileStandardInformationByHandleEx(
            SafeFileHandle file,
            int informationClass,
            out FileStandardInformation information,
            uint bufferSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern uint GetFinalPathNameByHandleW(
            SafeFileHandle file,
            [Out] char[] filePath,
            uint filePathSize,
            uint flags);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        internal static extern uint GetDriveTypeW(string rootPathName);

        [DllImport("advapi32.dll", SetLastError = true)]
        internal static extern uint GetSecurityInfo(
            SafeFileHandle handle,
            int objectType,
            uint securityInfo,
            out IntPtr owner,
            out IntPtr group,
            out IntPtr dacl,
            out IntPtr sacl,
            out IntPtr securityDescriptor);

        [DllImport("advapi32.dll")]
        internal static extern uint GetSecurityDescriptorLength(IntPtr securityDescriptor);

        [DllImport("kernel32.dll")]
        internal static extern IntPtr LocalFree(IntPtr memory);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetFileInformationByHandle(
            SafeFileHandle file,
            int informationClass,
            ref FileDispositionInformation information,
            uint bufferSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetFileInformationByHandle(
            SafeFileHandle file,
            int informationClass,
            IntPtr information,
            uint bufferSize);
    }

    public static class WindowsFile
    {
        private static readonly StringComparison PathComparison =
            StringComparison.OrdinalIgnoreCase;

        public static ValidatedHandle OpenValidatedDirectoryLease(string path)
        {
            string fullPath = NormalizeLocalPath(path);
            List<SafeFileHandle> ancestors = new List<SafeFileHandle>();
            SafeFileHandle target = null;
            bool transferred = false;
            try
            {
                List<string> components = GetComponents(fullPath);
                for (int index = 0; index < components.Count - 1; index++)
                {
                    ancestors.Add(OpenAndValidate(components[index], 0, true));
                }

                target = OpenAndValidate(
                    components[components.Count - 1], 0, true);
                FileIdentity identity = ReadIdentity(target);
                ValidatedHandle owner = new ValidatedHandle(
                    target,
                    ValidatedAccess.Directory,
                    fullPath,
                    fullPath,
                    true,
                    ancestors,
                    identity,
                    null,
                    null);
                transferred = true;
                return owner;
            }
            finally
            {
                if (!transferred)
                {
                    if (target != null)
                    {
                        target.Dispose();
                    }
                    DisposeAll(ancestors);
                }
            }
        }

        public static ValidatedHandle OpenValidatedFile(
            string path,
            string access,
            string authorizedRoot,
            string authorizedBasename)
        {
            string fullPath = NormalizeLocalPath(path);
            string root = NormalizeLocalPath(authorizedRoot);
            ValidateExactBasename(authorizedBasename);
            if (!String.Equals(Path.GetFileName(fullPath), authorizedBasename, PathComparison))
            {
                throw new InvalidOperationException("File basename does not match the authorization.");
            }
            EnsureWithinRoot(fullPath, root);

            ValidatedAccess validatedAccess;
            uint desiredAccess;
            switch (access)
            {
                case "Read":
                    validatedAccess = ValidatedAccess.Read;
                    desiredAccess = NativeMethods.GenericRead;
                    break;
                case "ReadWrite":
                    validatedAccess = ValidatedAccess.ReadWrite;
                    desiredAccess = NativeMethods.GenericRead | NativeMethods.GenericWrite;
                    break;
                case "ReadWriteDelete":
                    validatedAccess = ValidatedAccess.ReadWriteDelete;
                    desiredAccess = NativeMethods.GenericRead | NativeMethods.GenericWrite |
                        NativeMethods.Delete;
                    break;
                default:
                    throw new ArgumentOutOfRangeException("access");
            }

            List<SafeFileHandle> leases = new List<SafeFileHandle>();
            SafeFileHandle target = null;
            bool transferred = false;
            try
            {
                List<string> components = GetComponents(fullPath);
                for (int index = 0; index < components.Count - 1; index++)
                {
                    leases.Add(OpenAndValidate(components[index], 0, true));
                }

                target = OpenAndValidate(
                    components[components.Count - 1], desiredAccess, false);
                FileIdentity identity = ReadIdentity(target);
                if (identity.NumberOfLinks != 1)
                {
                    throw new InvalidOperationException("Validated files must have exactly one hard link.");
                }
                ValidatedHandle owner = new ValidatedHandle(
                    target,
                    validatedAccess,
                    root,
                    fullPath,
                    false,
                    leases,
                    identity,
                    authorizedBasename,
                    null);
                transferred = true;
                return owner;
            }
            finally
            {
                if (!transferred)
                {
                    if (target != null)
                    {
                        target.Dispose();
                    }
                    DisposeAll(leases);
                }
            }
        }

        public static ValidatedHandle NewValidatedFile(
            string path,
            string access,
            string authorizedRoot,
            string authorizedBasename,
            string authorizedBasenamePattern,
            byte[] securityDescriptor)
        {
            if (!String.Equals(access, "ReadWriteDelete", StringComparison.Ordinal))
            {
                throw new ArgumentOutOfRangeException("access");
            }
            string fullPath = NormalizeLocalPath(path);
            string root = NormalizeLocalPath(authorizedRoot);
            ValidateBasenameAuthorization(
                Path.GetFileName(fullPath),
                authorizedBasename,
                authorizedBasenamePattern);
            EnsureWithinRoot(fullPath, root);
            RawSecurityDescriptor expected = ValidateSecurityDescriptor(securityDescriptor);

            List<SafeFileHandle> leases = new List<SafeFileHandle>();
            SafeFileHandle target = null;
            bool transferred = false;
            bool created = false;
            try
            {
                List<string> components = GetComponents(fullPath);
                for (int index = 0; index < components.Count - 1; index++)
                {
                    leases.Add(OpenAndValidate(components[index], 0, true));
                }

                target = CreateProtectedFile(fullPath, securityDescriptor);
                created = true;
                ValidateOpenedObject(target, fullPath, false);
                FileIdentity identity = ReadIdentity(target);
                if (identity.NumberOfLinks != 1)
                {
                    throw new InvalidOperationException("Validated files must have exactly one hard link.");
                }
                ValidateSecurityDescriptor(target, expected);
                ValidatedHandle owner = new ValidatedHandle(
                    target,
                    ValidatedAccess.ReadWriteDelete,
                    root,
                    fullPath,
                    false,
                    leases,
                    identity,
                    authorizedBasename,
                    authorizedBasenamePattern);
                transferred = true;
                return owner;
            }
            finally
            {
                if (!transferred)
                {
                    if (target != null)
                    {
                        if (created && !target.IsInvalid && !target.IsClosed)
                        {
                            TryMarkDeleteByHandle(target);
                        }
                        target.Dispose();
                    }
                    DisposeAll(leases);
                }
            }
        }

        public static FileIdentity GetIdentity(ValidatedHandle handle)
        {
            ValidatedHandle validated = RequireValidated(handle);
            FileIdentity current = ReadIdentity(validated.FileHandle);
            EnsureIdentityMatches(current, validated.Identity);
            return current;
        }

        public static FileIdentityMaterial GetIdentityMaterial(ValidatedHandle handle)
        {
            ValidatedHandle validated = RequireValidated(handle);
            FileIdentity current = ReadIdentity(validated.FileHandle);
            EnsureIdentityMatches(current, validated.Identity);
            NativeMethods.FileIdInformation id = ReadFileId(validated.FileHandle);
            return new FileIdentityMaterial(
                id.VolumeSerialNumber,
                FileIdBytes(id),
                GetVolumeGuidPath(validated.FileHandle, validated.IsDirectory));
        }

        public static void AssertIdentity(
            ValidatedHandle handle,
            ulong expectedVolumeSerialNumber,
            string expectedFileId,
            uint expectedNumberOfLinks,
            string expectedFinalPath)
        {
            FileIdentity current = GetIdentity(handle);
            EnsureIdentityMatches(
                current,
                new FileIdentity {
                    VolumeSerialNumber = expectedVolumeSerialNumber,
                    FileId = expectedFileId,
                    NumberOfLinks = expectedNumberOfLinks,
                    FinalPath = NormalizeLocalPath(expectedFinalPath)
                });
        }

        public static void RenameNoReplace(ValidatedHandle handle, string destination)
        {
            ValidatedHandle validated = RequireValidated(handle);
            if (validated.IsDirectory || validated.Access != ValidatedAccess.ReadWriteDelete)
            {
                throw new InvalidOperationException(
                    "Rename requires a validated ReadWriteDelete file handle.");
            }
            FileIdentity before = GetIdentity(handle);
            string target = NormalizeLocalPath(destination);
            EnsureWithinRoot(target, validated.AuthorizedRoot);
            if (!String.Equals(
                Path.GetDirectoryName(target),
                Path.GetDirectoryName(validated.ValidatedPath),
                PathComparison))
            {
                throw new InvalidOperationException(
                    "Rename destination must use the validated source directory.");
            }
            ValidateBasenameAuthorization(
                Path.GetFileName(target),
                validated.AuthorizedBasename,
                validated.AuthorizedBasenamePattern);

            byte[] name = Encoding.Unicode.GetBytes(target);
            int rootOffset = IntPtr.Size == 8 ? 8 : 4;
            int nameLengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = nameLengthOffset + sizeof(uint);
            int size = checked(nameOffset + name.Length + sizeof(char));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                for (int index = 0; index < size; index++)
                {
                    Marshal.WriteByte(buffer, index, 0);
                }
                Marshal.WriteInt32(buffer, 0, 0);
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, nameLengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!NativeMethods.SetFileInformationByHandle(
                    validated.FileHandle,
                    NativeMethods.FileRenameInfo,
                    buffer,
                    (uint)size))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }

            FileIdentity after = ReadIdentity(validated.FileHandle);
            if (after.VolumeSerialNumber != before.VolumeSerialNumber ||
                !String.Equals(after.FileId, before.FileId, StringComparison.Ordinal) ||
                after.NumberOfLinks != 1 ||
                !String.Equals(after.FinalPath, target, PathComparison))
            {
                throw new InvalidOperationException("Renamed handle identity or final path is invalid.");
            }
            validated.ValidatedPath = target;
            validated.Identity = after;
        }

        public static void SetDeletionDisposition(ValidatedHandle handle)
        {
            ValidatedHandle validated = RequireValidated(handle);
            if (validated.IsDirectory || validated.Access != ValidatedAccess.ReadWriteDelete)
            {
                throw new InvalidOperationException(
                    "Deletion requires a validated ReadWriteDelete file handle.");
            }
            GetIdentity(handle);
            NativeMethods.FileDispositionInformation information =
                new NativeMethods.FileDispositionInformation { DeleteFile = true };
            if (!NativeMethods.SetFileInformationByHandle(
                validated.FileHandle,
                NativeMethods.FileDispositionInfo,
                ref information,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileDispositionInformation))))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }

        private static ValidatedHandle RequireValidated(ValidatedHandle handle)
        {
            if (handle == null)
            {
                throw new InvalidOperationException("Handle is not an open module-validated handle.");
            }
            handle.EnsureOpen();
            return handle;
        }

        private static SafeFileHandle OpenAndValidate(
            string path,
            uint desiredAccess,
            bool expectedDirectory)
        {
            uint flags = NativeMethods.FileFlagOpenReparsePoint;
            if (expectedDirectory)
            {
                flags |= NativeMethods.FileFlagBackupSemantics;
            }
            IntPtr native = NativeMethods.CreateFileW(
                path,
                desiredAccess,
                0,
                IntPtr.Zero,
                NativeMethods.OpenExisting,
                flags,
                IntPtr.Zero);
            if (native == NativeMethods.InvalidHandleValue)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            SafeFileHandle handle = new SafeFileHandle(native, true);
            try
            {
                ValidateOpenedObject(handle, path, expectedDirectory);
                return handle;
            }
            catch
            {
                handle.Dispose();
                throw;
            }
        }

        private static SafeFileHandle CreateProtectedFile(string path, byte[] descriptor)
        {
            IntPtr descriptorBuffer = Marshal.AllocHGlobal(descriptor.Length);
            try
            {
                Marshal.Copy(descriptor, 0, descriptorBuffer, descriptor.Length);
                NativeMethods.SecurityAttributes attributes = new NativeMethods.SecurityAttributes {
                    Length = (uint)Marshal.SizeOf(typeof(NativeMethods.SecurityAttributes)),
                    SecurityDescriptor = descriptorBuffer,
                    InheritHandle = false
                };
                IntPtr native = NativeMethods.CreateFileWithSecurityW(
                    path,
                    NativeMethods.GenericRead | NativeMethods.GenericWrite | NativeMethods.Delete,
                    0,
                    ref attributes,
                    NativeMethods.CreateNew,
                    NativeMethods.FileAttributeNormal | NativeMethods.FileFlagOpenReparsePoint,
                    IntPtr.Zero);
                if (native == NativeMethods.InvalidHandleValue)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                return new SafeFileHandle(native, true);
            }
            finally
            {
                Marshal.FreeHGlobal(descriptorBuffer);
            }
        }

        private static void ValidateOpenedObject(
            SafeFileHandle handle,
            string path,
            bool expectedDirectory)
        {
            NativeMethods.FileAttributeTagInformation tag;
            if (!NativeMethods.GetFileAttributeTagInformationByHandleEx(
                handle,
                9,
                out tag,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileAttributeTagInformation))))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            if ((tag.FileAttributes & NativeMethods.FileAttributeReparsePoint) != 0 ||
                tag.ReparseTag != 0)
            {
                throw new InvalidOperationException("Reparse points are not allowed.");
            }
            bool isDirectory =
                (tag.FileAttributes & NativeMethods.FileAttributeDirectory) != 0;
            if (isDirectory != expectedDirectory)
            {
                throw new InvalidOperationException(
                    expectedDirectory
                        ? "A path component is not a directory."
                        : "A directory cannot be opened in file position.");
            }
            string finalPath = GetFinalPath(handle);
            if (!String.Equals(finalPath, path, PathComparison))
            {
                throw new InvalidOperationException(
                    "Handle final path does not match the validated path component.");
            }
        }

        private static FileIdentity ReadIdentity(SafeFileHandle handle)
        {
            NativeMethods.FileIdInformation id = ReadFileId(handle);
            NativeMethods.FileStandardInformation standard;
            if (!NativeMethods.GetFileStandardInformationByHandleEx(
                handle,
                1,
                out standard,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileStandardInformation))))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            byte[] identifier = FileIdBytes(id);
            StringBuilder fileId = new StringBuilder(32);
            foreach (byte value in identifier)
            {
                fileId.Append(value.ToString("X2"));
            }
            return new FileIdentity {
                VolumeSerialNumber = id.VolumeSerialNumber,
                FileId = fileId.ToString(),
                NumberOfLinks = standard.NumberOfLinks,
                FinalPath = GetFinalPath(handle)
            };
        }

        private static NativeMethods.FileIdInformation ReadFileId(SafeFileHandle handle)
        {
            NativeMethods.FileIdInformation id;
            if (!NativeMethods.GetFileIdInformationByHandleEx(
                handle,
                18,
                out id,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileIdInformation))))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            return id;
        }

        private static byte[] FileIdBytes(NativeMethods.FileIdInformation id)
        {
            byte[] identifier = new byte[16];
            Array.Copy(BitConverter.GetBytes(id.FileIdLow), 0, identifier, 0, 8);
            Array.Copy(BitConverter.GetBytes(id.FileIdHigh), 0, identifier, 8, 8);
            return identifier;
        }

        private static string GetFinalPath(SafeFileHandle handle)
        {
            uint size = 512;
            while (true)
            {
                char[] buffer = new char[(int)size];
                uint length = NativeMethods.GetFinalPathNameByHandleW(handle, buffer, size, 0);
                if (length == 0)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (length < size)
                {
                    string path = ExtractNativePath(buffer, length);
                    if (path.StartsWith(@"\\?\UNC\", PathComparison))
                    {
                        return @"\\" + path.Substring(8);
                    }
                    if (path.StartsWith(@"\\?\", PathComparison))
                    {
                        return path.Substring(4);
                    }
                    return path;
                }
                size = checked(length + 1);
            }
        }

        private static string GetVolumeGuidPath(SafeFileHandle handle, bool isDirectory)
        {
            uint size = 512;
            while (true)
            {
                char[] buffer = new char[(int)size];
                uint length = NativeMethods.GetFinalPathNameByHandleW(
                    handle,
                    buffer,
                    size,
                    NativeMethods.VolumeNameGuid);
                if (length == 0)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (length < size)
                {
                    string path = ExtractNativePath(buffer, length).Replace('/', '\\');
                    if (path.IndexOf('\0') >= 0 ||
                        (!isDirectory && path.EndsWith("\\", StringComparison.Ordinal)) ||
                        !Regex.IsMatch(
                            path,
                            @"^\\\\\?\\Volume\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}\\.+$",
                            RegexOptions.CultureInvariant))
                    {
                        throw new InvalidOperationException("Handle did not resolve to a strict volume-GUID path.");
                    }
                    new UTF8Encoding(false, true).GetBytes(path);
                    return path;
                }
                size = checked(length + 1);
            }
        }

        private static string ExtractNativePath(char[] buffer, uint reportedLength)
        {
            int count = (int)Math.Min(reportedLength, (uint)buffer.Length);
            string value = new string(buffer, 0, count);
            int terminator = value.IndexOf('\0');
            if (terminator >= 0)
            {
                value = value.Substring(0, terminator);
            }
            if (value.Length == 0 || value.Length > reportedLength)
            {
                throw new InvalidOperationException("Native final path length is invalid.");
            }
            return value;
        }

        private static RawSecurityDescriptor ValidateSecurityDescriptor(byte[] descriptor)
        {
            if (descriptor == null || descriptor.Length == 0)
            {
                throw new ArgumentException("A security descriptor is required.");
            }
            RawSecurityDescriptor parsed;
            try
            {
                parsed = new RawSecurityDescriptor((byte[])descriptor.Clone(), 0);
            }
            catch (Exception error)
            {
                throw new ArgumentException("Security descriptor bytes are invalid.", error);
            }
            ControlFlags flags = parsed.ControlFlags;
            if ((flags & ControlFlags.SelfRelative) == 0 ||
                (flags & ControlFlags.DiscretionaryAclPresent) == 0 ||
                (flags & ControlFlags.DiscretionaryAclProtected) == 0 ||
                (flags & ControlFlags.SystemAclPresent) != 0 ||
                parsed.Owner == null || parsed.Group == null || parsed.DiscretionaryAcl == null)
            {
                throw new InvalidOperationException(
                    "Security descriptor must be self-relative with owner, group, and a protected DACL.");
            }
            return parsed;
        }

        private static void ValidateSecurityDescriptor(
            SafeFileHandle handle,
            RawSecurityDescriptor expected)
        {
            IntPtr owner;
            IntPtr group;
            IntPtr dacl;
            IntPtr sacl;
            IntPtr descriptor;
            uint status = NativeMethods.GetSecurityInfo(
                handle,
                NativeMethods.SeFileObject,
                NativeMethods.OwnerSecurityInformation |
                    NativeMethods.GroupSecurityInformation |
                    NativeMethods.DaclSecurityInformation,
                out owner,
                out group,
                out dacl,
                out sacl,
                out descriptor);
            if (status != 0)
            {
                throw new Win32Exception((int)status);
            }
            try
            {
                uint length = NativeMethods.GetSecurityDescriptorLength(descriptor);
                if (length == 0 || length > Int32.MaxValue)
                {
                    throw new InvalidOperationException("Created file security descriptor is invalid.");
                }
                byte[] bytes = new byte[(int)length];
                Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                RawSecurityDescriptor actual = new RawSecurityDescriptor(bytes, 0);
                if (!String.Equals(actual.Owner.Value, expected.Owner.Value, StringComparison.Ordinal) ||
                    !String.Equals(actual.Group.Value, expected.Group.Value, StringComparison.Ordinal) ||
                    actual.ControlFlags != expected.ControlFlags ||
                    !ByteArraysEqual(AclBytes(actual.DiscretionaryAcl), AclBytes(expected.DiscretionaryAcl)))
                {
                    throw new InvalidOperationException(
                        "Created file security descriptor does not match the protected descriptor.");
                }
            }
            finally
            {
                NativeMethods.LocalFree(descriptor);
            }
        }

        private static byte[] AclBytes(RawAcl acl)
        {
            byte[] bytes = new byte[acl.BinaryLength];
            acl.GetBinaryForm(bytes, 0);
            return bytes;
        }

        private static bool ByteArraysEqual(byte[] left, byte[] right)
        {
            if (left.Length != right.Length)
            {
                return false;
            }
            int difference = 0;
            for (int index = 0; index < left.Length; index++)
            {
                difference |= left[index] ^ right[index];
            }
            return difference == 0;
        }

        private static void TryMarkDeleteByHandle(SafeFileHandle handle)
        {
            NativeMethods.FileDispositionInformation information =
                new NativeMethods.FileDispositionInformation { DeleteFile = true };
            NativeMethods.SetFileInformationByHandle(
                handle,
                NativeMethods.FileDispositionInfo,
                ref information,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileDispositionInformation)));
        }

        private static void EnsureIdentityMatches(FileIdentity actual, FileIdentity expected)
        {
            if (actual.VolumeSerialNumber != expected.VolumeSerialNumber ||
                !String.Equals(actual.FileId, expected.FileId, StringComparison.Ordinal) ||
                actual.NumberOfLinks != expected.NumberOfLinks ||
                !String.Equals(actual.FinalPath, expected.FinalPath, PathComparison))
            {
                throw new InvalidOperationException("File identity or final path changed.");
            }
        }

        private static string NormalizeLocalPath(string path)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("Path is required.");
            }
            string candidate = path.Replace('/', '\\');
            if (candidate.IndexOf('\0') >= 0 || candidate.IndexOfAny(new char[] { '*', '?' }) >= 0)
            {
                throw new InvalidOperationException("Wildcards and embedded NULs are not allowed.");
            }
            if (candidate.StartsWith(@"\\", StringComparison.Ordinal) ||
                candidate.Length < 3 ||
                !Char.IsLetter(candidate[0]) ||
                candidate[1] != ':' ||
                candidate[2] != '\\')
            {
                throw new InvalidOperationException("Only absolute local drive paths are allowed.");
            }
            if (candidate.IndexOf(':', 2) >= 0)
            {
                throw new InvalidOperationException("Alternate data streams are not allowed.");
            }
            string[] rawSegments = candidate.Substring(3).Split('\\');
            for (int index = 0; index < rawSegments.Length; index++)
            {
                string segment = rawSegments[index];
                if (segment.Length == 0 && index == rawSegments.Length - 1)
                {
                    continue;
                }
                if (segment.Length == 0 || segment == "." || segment == ".." ||
                    EndsWithAliasCharacter(segment) ||
                    segment.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                {
                    throw new InvalidOperationException("Path contains an invalid or aliased component.");
                }
            }
            string fullPath = Path.GetFullPath(candidate).TrimEnd('\\');
            if (fullPath.Length == 2)
            {
                fullPath += "\\";
            }
            string driveRoot = Path.GetPathRoot(fullPath);
            if (String.IsNullOrEmpty(driveRoot) ||
                NativeMethods.GetDriveTypeW(driveRoot) != NativeMethods.DriveFixed)
            {
                throw new InvalidOperationException("Path is not on a fixed local drive.");
            }
            return fullPath;
        }

        private static void ValidateExactBasename(string basename)
        {
            if (String.IsNullOrWhiteSpace(basename) || basename == "." || basename == ".." ||
                basename.IndexOfAny(new char[] { '\\', '/', ':', '*', '?' }) >= 0 ||
                basename.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
                EndsWithAliasCharacter(basename))
            {
                throw new InvalidOperationException("Authorized basename is invalid.");
            }
        }

        private static void ValidateBasenameAuthorization(
            string basename,
            string authorizedBasename,
            string authorizedBasenamePattern)
        {
            ValidateExactBasename(basename);
            bool hasExact = !String.IsNullOrEmpty(authorizedBasename);
            bool hasPattern = !String.IsNullOrEmpty(authorizedBasenamePattern);
            if (hasExact == hasPattern)
            {
                throw new InvalidOperationException(
                    "Specify exactly one basename authorization.");
            }
            if (hasExact)
            {
                ValidateExactBasename(authorizedBasename);
                if (!String.Equals(basename, authorizedBasename, PathComparison))
                {
                    throw new InvalidOperationException("File basename does not match the authorization.");
                }
                return;
            }
            if (authorizedBasenamePattern.Length > 512 ||
                !authorizedBasenamePattern.StartsWith(@"\A", StringComparison.Ordinal) ||
                !authorizedBasenamePattern.EndsWith(@"\z", StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "Authorized basename pattern must be explicitly anchored with \\A and \\z.");
            }
            Regex expression;
            try
            {
                expression = new Regex(
                    authorizedBasenamePattern,
                    RegexOptions.CultureInvariant | RegexOptions.ExplicitCapture,
                    TimeSpan.FromMilliseconds(250));
            }
            catch (ArgumentException error)
            {
                throw new InvalidOperationException("Authorized basename pattern is invalid.", error);
            }
            if (!expression.IsMatch(basename))
            {
                throw new InvalidOperationException("File basename does not match the authorization pattern.");
            }
        }

        private static bool EndsWithAliasCharacter(string value)
        {
            return value.EndsWith(".", StringComparison.Ordinal) ||
                value.EndsWith(" ", StringComparison.Ordinal);
        }

        private static void EnsureWithinRoot(string path, string root)
        {
            if (String.Equals(path, root, PathComparison))
            {
                return;
            }
            string prefix = root.EndsWith("\\", StringComparison.Ordinal)
                ? root
                : root + "\\";
            if (!path.StartsWith(prefix, PathComparison))
            {
                throw new InvalidOperationException("Path is outside the authorized root.");
            }
        }

        private static List<string> GetComponents(string fullPath)
        {
            string root = Path.GetPathRoot(fullPath);
            List<string> components = new List<string>();
            components.Add(root);
            string relative = fullPath.Substring(root.Length);
            if (relative.Length == 0)
            {
                return components;
            }
            string current = root.TrimEnd('\\');
            foreach (string segment in relative.Split('\\'))
            {
                current = current + "\\" + segment;
                components.Add(current);
            }
            return components;
        }

        private static void DisposeAll(List<SafeFileHandle> handles)
        {
            for (int index = handles.Count - 1; index >= 0; index--)
            {
                handles[index].Dispose();
            }
        }
    }
}
'@
}

function Open-PhaseAValidatedDirectoryLease {
  param([Parameter(Mandatory)][string]$Path)
  return [ApplyPilot.PhaseA.WindowsFile]::OpenValidatedDirectoryLease($Path)
}

function Open-PhaseAValidatedFile {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][ValidateSet('Read', 'ReadWrite', 'ReadWriteDelete')][string]$Access,
    [Parameter(Mandatory)][string]$AuthorizedRoot,
    [Parameter(Mandatory)][string]$AuthorizedBasename
  )
  return [ApplyPilot.PhaseA.WindowsFile]::OpenValidatedFile(
    $Path,
    $Access,
    $AuthorizedRoot,
    $AuthorizedBasename
  )
}

function New-PhaseAValidatedFile {
  [CmdletBinding(DefaultParameterSetName = 'Exact')]
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$AuthorizedRoot,
    [Parameter(Mandatory, ParameterSetName = 'Exact')][string]$AuthorizedBasename,
    [Parameter(Mandatory, ParameterSetName = 'Pattern')][string]$AuthorizedBasenamePattern,
    [Parameter(Mandatory)][object]$SecurityDescriptor,
    [Parameter(Mandatory)][ValidateSet('ReadWriteDelete')][string]$Access
  )
  if ($SecurityDescriptor -is [byte[]]) {
    [byte[]]$descriptorBytes = [byte[]]$SecurityDescriptor.Clone()
  } elseif ($SecurityDescriptor.PSObject.Methods.Name -contains 'GetSecurityDescriptorBinaryForm') {
    [byte[]]$descriptorBytes = $SecurityDescriptor.GetSecurityDescriptorBinaryForm()
  } elseif (
    $SecurityDescriptor.PSObject.Properties.Name -contains 'BinaryLength' -and
    $SecurityDescriptor.PSObject.Methods.Name -contains 'GetBinaryForm'
  ) {
    [byte[]]$descriptorBytes = [byte[]]::new([int]$SecurityDescriptor.BinaryLength)
    $SecurityDescriptor.GetBinaryForm($descriptorBytes, 0)
  } else {
    throw 'SecurityDescriptor must be self-relative bytes or a security descriptor object.'
  }
  return [ApplyPilot.PhaseA.WindowsFile]::NewValidatedFile(
    $Path,
    $Access,
    $AuthorizedRoot,
    $AuthorizedBasename,
    $AuthorizedBasenamePattern,
    $descriptorBytes
  )
}

function Get-PhaseAFileIdentity {
  param([Parameter(Mandatory)][ApplyPilot.PhaseA.ValidatedHandle]$Handle)
  $identity = [ApplyPilot.PhaseA.WindowsFile]::GetIdentity($Handle)
  return [pscustomobject]@{
    VolumeSerialNumber = [uint64]$identity.VolumeSerialNumber
    FileId = [string]$identity.FileId
    NumberOfLinks = [uint32]$identity.NumberOfLinks
    FinalPath = [string]$identity.FinalPath
  }
}

function Get-PhaseAFileIdentityMaterial {
  param([Parameter(Mandatory)][ApplyPilot.PhaseA.ValidatedHandle]$Handle)
  $material = [ApplyPilot.PhaseA.WindowsFile]::GetIdentityMaterial($Handle)
  return [pscustomobject]@{
    VolumeSerialNumber = [uint64]$material.VolumeSerialNumber
    FileId = [byte[]]$material.FileId
    VolumeGuidPath = [string]$material.VolumeGuidPath
  }
}

function Assert-PhaseAFileIdentity {
  param(
    [Parameter(Mandatory)][ApplyPilot.PhaseA.ValidatedHandle]$Handle,
    [Parameter(Mandatory)][pscustomobject]$Expected
  )
  [ApplyPilot.PhaseA.WindowsFile]::AssertIdentity(
    $Handle,
    [uint64]$Expected.VolumeSerialNumber,
    [string]$Expected.FileId,
    [uint32]$Expected.NumberOfLinks,
    [string]$Expected.FinalPath
  )
}

function Rename-PhaseAFileNoReplace {
  param(
    [Parameter(Mandatory)][ApplyPilot.PhaseA.ValidatedHandle]$Handle,
    [Parameter(Mandatory)][string]$Destination
  )
  [ApplyPilot.PhaseA.WindowsFile]::RenameNoReplace($Handle, $Destination)
}

function Set-PhaseAFileDeletionDisposition {
  param([Parameter(Mandatory)][ApplyPilot.PhaseA.ValidatedHandle]$Handle)
  [ApplyPilot.PhaseA.WindowsFile]::SetDeletionDisposition($Handle)
}

Export-ModuleMember -Function @(
  'Open-PhaseAValidatedDirectoryLease',
  'Open-PhaseAValidatedFile',
  'New-PhaseAValidatedFile',
  'Get-PhaseAFileIdentity',
  'Get-PhaseAFileIdentityMaterial',
  'Assert-PhaseAFileIdentity',
  'Rename-PhaseAFileNoReplace',
  'Set-PhaseAFileDeletionDisposition'
)
