Set-StrictMode -Version Latest

if (-not ('ApplyPilot.PhaseA.WindowsFile' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
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

    internal enum ValidatedAccess
    {
        Directory,
        Read,
        ReadWrite,
        ReadWriteDelete
    }

    internal sealed class ValidatedState
    {
        private int leasesDisposed;

        internal ValidatedState(
            ValidatedAccess access,
            string authorizedRoot,
            string validatedPath,
            bool isDirectory,
            List<SafeFileHandle> leases,
            FileIdentity identity)
        {
            Access = access;
            AuthorizedRoot = authorizedRoot;
            ValidatedPath = validatedPath;
            IsDirectory = isDirectory;
            Leases = leases;
            Identity = identity;
        }

        internal ValidatedAccess Access { get; private set; }
        internal string AuthorizedRoot { get; private set; }
        internal string ValidatedPath { get; set; }
        internal bool IsDirectory { get; private set; }
        internal List<SafeFileHandle> Leases { get; private set; }
        internal FileIdentity Identity { get; set; }

        internal void DisposeLeases()
        {
            if (Interlocked.Exchange(ref leasesDisposed, 1) != 0)
            {
                return;
            }
            for (int index = Leases.Count - 1; index >= 0; index--)
            {
                Leases[index].Dispose();
            }
            Leases.Clear();
        }
    }

    internal static class NativeMethods
    {
        internal const uint GenericRead = 0x80000000;
        internal const uint GenericWrite = 0x40000000;
        internal const uint Delete = 0x00010000;
        internal const uint OpenExisting = 3;
        internal const uint FileFlagBackupSemantics = 0x02000000;
        internal const uint FileFlagOpenReparsePoint = 0x00200000;
        internal const uint FileAttributeDirectory = 0x00000010;
        internal const uint FileAttributeReparsePoint = 0x00000400;
        internal const int FileDispositionInfo = 4;
        internal const int FileRenameInfo = 3;
        internal const uint DriveFixed = 3;
        internal static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        internal struct FileTime
        {
            internal uint LowDateTime;
            internal uint HighDateTime;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct ByHandleFileInformation
        {
            internal uint FileAttributes;
            internal FileTime CreationTime;
            internal FileTime LastAccessTime;
            internal FileTime LastWriteTime;
            internal uint VolumeSerialNumber;
            internal uint FileSizeHigh;
            internal uint FileSizeLow;
            internal uint NumberOfLinks;
            internal uint FileIndexHigh;
            internal uint FileIndexLow;
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

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateFileW(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandle(
            SafeFileHandle file,
            out ByHandleFileInformation information);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandleEx(
            SafeFileHandle file,
            int informationClass,
            out FileAttributeTagInformation information,
            uint bufferSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern uint GetFinalPathNameByHandleW(
            SafeFileHandle file,
            StringBuilder filePath,
            uint filePathSize,
            uint flags);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        internal static extern uint GetDriveTypeW(string rootPathName);

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
        private static readonly ConditionalWeakTable<SafeFileHandle, ValidatedState> States =
            new ConditionalWeakTable<SafeFileHandle, ValidatedState>();

        public static SafeFileHandle OpenValidatedDirectoryLease(string path)
        {
            string fullPath = NormalizeLocalPath(path);
            List<SafeFileHandle> ancestors = new List<SafeFileHandle>();
            try
            {
                List<string> components = GetComponents(fullPath);
                for (int index = 0; index < components.Count - 1; index++)
                {
                    ancestors.Add(OpenAndValidate(components[index], 0, true));
                }

                SafeFileHandle target = OpenAndValidate(
                    components[components.Count - 1], 0, true);
                FileIdentity identity = ReadIdentity(target);
                ValidatedState state = new ValidatedState(
                    ValidatedAccess.Directory,
                    fullPath,
                    fullPath,
                    true,
                    ancestors,
                    identity);
                States.Add(target, state);
                MonitorLeaseLifetime(target, state);
                return target;
            }
            catch
            {
                DisposeAll(ancestors);
                throw;
            }
        }

        public static SafeFileHandle OpenValidatedFile(
            string path,
            string access,
            string authorizedRoot,
            string authorizedBasename)
        {
            string fullPath = NormalizeLocalPath(path);
            string root = NormalizeLocalPath(authorizedRoot);
            if (String.IsNullOrWhiteSpace(authorizedBasename) ||
                authorizedBasename.IndexOfAny(new char[] { '\\', '/', ':' }) >= 0 ||
                EndsWithAliasCharacter(authorizedBasename))
            {
                throw new InvalidOperationException("Authorized basename is invalid.");
            }
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
            try
            {
                List<string> components = GetComponents(fullPath);
                for (int index = 0; index < components.Count - 1; index++)
                {
                    leases.Add(OpenAndValidate(components[index], 0, true));
                }

                SafeFileHandle target = OpenAndValidate(
                    components[components.Count - 1], desiredAccess, false);
                FileIdentity identity = ReadIdentity(target);
                if (identity.NumberOfLinks != 1)
                {
                    target.Dispose();
                    throw new InvalidOperationException("Validated files must have exactly one hard link.");
                }
                ValidatedState state = new ValidatedState(
                    validatedAccess,
                    root,
                    fullPath,
                    false,
                    leases,
                    identity);
                States.Add(target, state);
                MonitorLeaseLifetime(target, state);
                return target;
            }
            catch
            {
                DisposeAll(leases);
                throw;
            }
        }

        public static FileIdentity GetIdentity(SafeFileHandle handle)
        {
            ValidatedState validated = RequireValidated(handle);
            FileIdentity current = ReadIdentity(handle);
            EnsureIdentityMatches(current, validated.Identity);
            return current;
        }

        public static void AssertIdentity(
            SafeFileHandle handle,
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

        public static void RenameNoReplace(SafeFileHandle handle, string destination)
        {
            ValidatedState validated = RequireValidated(handle);
            if (validated.IsDirectory || validated.Access != ValidatedAccess.ReadWriteDelete)
            {
                throw new InvalidOperationException(
                    "Rename requires a validated ReadWriteDelete file handle.");
            }
            GetIdentity(handle);
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
            if (File.Exists(target) || Directory.Exists(target))
            {
                throw new IOException("Rename destination already exists.");
            }

            byte[] name = Encoding.Unicode.GetBytes(target);
            int rootOffset = IntPtr.Size == 8 ? 8 : 4;
            int nameLengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = nameLengthOffset + sizeof(uint);
            int size = checked(nameOffset + name.Length);
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
                    handle,
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

            validated.ValidatedPath = target;
            validated.Identity = ReadIdentity(handle);
            if (!String.Equals(validated.Identity.FinalPath, target, PathComparison))
            {
                throw new InvalidOperationException("Renamed handle final path does not match destination.");
            }
        }

        public static void SetDeletionDisposition(SafeFileHandle handle)
        {
            ValidatedState validated = RequireValidated(handle);
            if (validated.IsDirectory || validated.Access != ValidatedAccess.ReadWriteDelete)
            {
                throw new InvalidOperationException(
                    "Deletion requires a validated ReadWriteDelete file handle.");
            }
            GetIdentity(handle);
            NativeMethods.FileDispositionInformation information =
                new NativeMethods.FileDispositionInformation { DeleteFile = true };
            if (!NativeMethods.SetFileInformationByHandle(
                handle,
                NativeMethods.FileDispositionInfo,
                ref information,
                (uint)Marshal.SizeOf(typeof(NativeMethods.FileDispositionInformation))))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }

        private static ValidatedState RequireValidated(SafeFileHandle handle)
        {
            ValidatedState validated;
            if (handle == null || !States.TryGetValue(handle, out validated))
            {
                throw new InvalidOperationException("Handle is not an open module-validated handle.");
            }
            if (handle.IsInvalid || handle.IsClosed)
            {
                validated.DisposeLeases();
                throw new InvalidOperationException("Handle is not an open module-validated handle.");
            }
            return validated;
        }

        private static void MonitorLeaseLifetime(
            SafeFileHandle handle,
            ValidatedState state)
        {
            WeakReference<SafeFileHandle> weakHandle =
                new WeakReference<SafeFileHandle>(handle);
            ThreadPool.QueueUserWorkItem(delegate
            {
                SafeFileHandle current;
                while (weakHandle.TryGetTarget(out current) && !current.IsClosed)
                {
                    current = null;
                    Thread.Sleep(25);
                }
                state.DisposeLeases();
            });
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
                NativeMethods.FileAttributeTagInformation tag;
                if (!NativeMethods.GetFileInformationByHandleEx(
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
                return handle;
            }
            catch
            {
                handle.Dispose();
                throw;
            }
        }

        private static FileIdentity ReadIdentity(SafeFileHandle handle)
        {
            NativeMethods.ByHandleFileInformation information;
            if (!NativeMethods.GetFileInformationByHandle(handle, out information))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            ulong fileIndex = ((ulong)information.FileIndexHigh << 32) |
                information.FileIndexLow;
            return new FileIdentity {
                VolumeSerialNumber = information.VolumeSerialNumber,
                FileId = fileIndex.ToString("X16"),
                NumberOfLinks = information.NumberOfLinks,
                FinalPath = GetFinalPath(handle)
            };
        }

        private static string GetFinalPath(SafeFileHandle handle)
        {
            uint size = 512;
            while (true)
            {
                StringBuilder buffer = new StringBuilder((int)size);
                uint length = NativeMethods.GetFinalPathNameByHandleW(handle, buffer, size, 0);
                if (length == 0)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if (length < size)
                {
                    string path = buffer.ToString();
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
            foreach (string segment in rawSegments)
            {
                if (segment.Length == 0)
                {
                    continue;
                }
                if (EndsWithAliasCharacter(segment))
                {
                    throw new InvalidOperationException("Trailing-dot and trailing-space aliases are not allowed.");
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

function Get-PhaseAFileIdentity {
  param([Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle)
  $identity = [ApplyPilot.PhaseA.WindowsFile]::GetIdentity($Handle)
  return [pscustomobject]@{
    VolumeSerialNumber = [uint64]$identity.VolumeSerialNumber
    FileId = [string]$identity.FileId
    NumberOfLinks = [uint32]$identity.NumberOfLinks
    FinalPath = [string]$identity.FinalPath
  }
}

function Assert-PhaseAFileIdentity {
  param(
    [Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle,
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
    [Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle,
    [Parameter(Mandatory)][string]$Destination
  )
  [ApplyPilot.PhaseA.WindowsFile]::RenameNoReplace($Handle, $Destination)
}

function Set-PhaseAFileDeletionDisposition {
  param([Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle)
  [ApplyPilot.PhaseA.WindowsFile]::SetDeletionDisposition($Handle)
}

Export-ModuleMember -Function @(
  'Open-PhaseAValidatedDirectoryLease',
  'Open-PhaseAValidatedFile',
  'Get-PhaseAFileIdentity',
  'Assert-PhaseAFileIdentity',
  'Rename-PhaseAFileNoReplace',
  'Set-PhaseAFileDeletionDisposition'
)
