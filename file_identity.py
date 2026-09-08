"""Complete, stable file identities for decisions that must not use a prefix."""
from __future__ import annotations

import hashlib
import os
import struct
from functools import lru_cache
from pathlib import Path

import media_availability


_WINDOWS = os.name == "nt"


class _WindowsBindings:
    """File revision and guarded read handles; initialized lazily on Windows."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in (
                "CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime"
            )] + [("FileAttributes", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create_file = kernel.CreateFileW
        self.create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE]
        self.create_file.restype = wintypes.HANDLE
        self.query_file = kernel.GetFileInformationByHandleEx
        self.query_file.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                   wintypes.LPVOID, wintypes.DWORD]
        self.query_file.restype = wintypes.BOOL
        self.device_io = kernel.DeviceIoControl
        self.device_io.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.device_io.restype = wintypes.BOOL
        self.reopen_file = kernel.ReOpenFile
        self.reopen_file.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD]
        self.reopen_file.restype = wintypes.HANDLE
        self.close_handle = kernel.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL
        self.open_osfhandle = msvcrt.open_osfhandle
        self.get_osfhandle = msvcrt.get_osfhandle
        self.invalid_handle = ctypes.c_void_p(-1).value
        self.basic_info = FileBasicInfo
        self.ctypes = ctypes
        self.dword = wintypes.DWORD

    def open_metadata_fd(self, path):
        # FILE_READ_ATTRIBUTES; share read/write/delete; OPEN_EXISTING. This
        # requests no file data, including for a cloud placeholder. Follow the
        # same reparse target as Path.stat(), rather than opening the link.
        handle = self.create_file(os.fsdecode(os.fspath(path)), 0x80, 0x7,
                                  None, 3, 0, None)
        if handle == self.invalid_handle:
            raise self.ctypes.WinError()
        try:
            # The CRT fd takes ownership only after this succeeds. os.fstat
            # then validates the very same native handle, without reading data.
            return self.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            self.close_handle(handle)
            raise

    def change_time(self, fd):
        info = self.basic_info()
        if not self.query_file(self.get_osfhandle(fd), 0,
                               self.ctypes.byref(info), self.ctypes.sizeof(info)):
            raise self.ctypes.WinError()
        if info.ChangeTime <= 0:
            raise OSError("The filesystem does not provide a file change time")
        return int(info.ChangeTime)

    def file_usn(self, fd):
        # FSCTL_READ_FILE_USN_DATA is FILE_ANY_ACCESS: query this file handle,
        # without opening the volume, enabling its journal, or flushing writes.
        # SMB and volumes without an active journal can decline this query.
        versions = (self.ctypes.c_ushort * 2)(2, 3)
        output = self.ctypes.create_string_buffer(4096)
        returned = self.dword()
        if not self.device_io(self.get_osfhandle(fd), 0x000900EB,
                              versions, self.ctypes.sizeof(versions),
                              output, self.ctypes.sizeof(output),
                              self.ctypes.byref(returned), None):
            return None
        return _usn_record(output.raw[:returned.value])

    def open_data_fd(self, fd):
        # ReOpenFile retains the same file object with an independent read
        # position. A dup/seek would disturb a borrowed Python stream's offset.
        # Deny concurrent writers for this fallback read; an actively edited
        # source fails closed instead of hashing a mixture of revisions.
        handle = self.reopen_file(self.get_osfhandle(fd), 0x80000000, 0x5,
                                  0x08000000 | 0x00100000)
        if handle == self.invalid_handle:
            raise self.ctypes.WinError()
        try:
            return self.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            self.close_handle(handle)
            raise


def _usn_record(data):
    if len(data) < 8:
        return None
    length, major, _minor = struct.unpack_from("<IHH", data)
    offset = {2: 24, 3: 40}.get(major)
    if offset is None or not offset + 8 <= length <= len(data):
        return None
    usn = struct.unpack_from("<q", data, offset)[0]
    return usn if usn > 0 else None


@lru_cache(maxsize=1)
def _windows_bindings():
    return _WindowsBindings()


def _stat_fields(stat) -> tuple[int, int, int, int, int]:
    # CPython 3.13 path stat keeps legacy ctime=creation, while fstat may
    # expose native change time. Normalize both to birthtime on Windows;
    # the independent native revision below supplies content validation.
    ctime = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns) if _WINDOWS else stat.st_ctime_ns
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            ctime)


def _windows_revision(stat, *, path=None, fd=None):
    if fd is None and path is None:
        raise OSError("A file path or descriptor is required for Windows identity")
    api = _windows_bindings()
    owned_fd = api.open_metadata_fd(path) if fd is None else None
    if owned_fd is not None:
        fd = owned_fd
    try:
        before = api.change_time(fd)
        if _stat_fields(os.fstat(fd)) != _stat_fields(stat):
            raise OSError("File changed before querying its identity")
        usn = api.file_usn(fd)
        if usn is not None:
            revision = (1, usn)
        else:
            # Timestamps are not monotonic revision counters. Without a USN,
            # cache guards must verify complete bytes, including same-tick
            # rewrites. This intentionally costs a full read on those volumes;
            # NTFS journal-backed checks remain metadata-only.
            attrs = getattr(stat, "st_file_attributes", 0)
            if attrs & (0x1000 | 0x40000 | 0x400000):
                raise OSError("Download the cloud original before verifying its identity")
            media_availability.require_local(path or "original", stat=stat)
            digest = hashlib.blake2b(digest_size=16)
            with os.fdopen(api.open_data_fd(fd), "rb") as stream:
                if _stat_fields(os.fstat(stream.fileno())) != _stat_fields(stat):
                    raise OSError("File changed before querying its identity")
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
                if _stat_fields(os.fstat(stream.fileno())) != _stat_fields(stat):
                    raise OSError("File changed while querying its identity")
            revision = (2, int(digest.hexdigest(), 16))
        after = api.change_time(fd)
        if before != after or api.file_usn(fd) != usn:
            raise OSError("File changed while querying its identity")
        return (after, *revision)
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


def stat_signature(stat, *, path=None, fd=None) -> tuple[int, ...]:
    signature = _stat_fields(stat)
    if _WINDOWS:
        # Native ChangeTime alone can collide for rapid same-mtime rewrites.
        signature += _windows_revision(stat, path=path, fd=fd)
    return signature


def signature_key(stat, *, path=None, fd=None) -> str:
    return ":".join(map(str, stat_signature(stat, path=path, fd=fd)))


def content_hash(path: Path | str, *, expected_revision=None,
                 expected_signature=None) -> str:
    """Hash local bytes with bounded memory; reject a file changed during reading.

    ``expected_revision`` is an optional (size, mtime_ns) from an earlier walk.
    Callers must not attach a new digest to metadata from a different revision.
    """
    path = Path(path)
    before = path.stat()
    media_availability.require_local(path, stat=before)
    before_signature = stat_signature(before, path=path)
    if expected_signature is not None and expected_signature != ":".join(map(str, before_signature)):
        raise OSError(f"file changed before hashing: {path}")
    if expected_revision is not None and expected_revision != (
            before.st_size, before.st_mtime_ns):
        raise OSError(f"file changed before hashing: {path}")
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as stream:
        fd = stream.fileno()
        if stat_signature(os.fstat(fd), fd=fd) != before_signature:
            raise OSError(f"file changed before hashing: {path}")
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
        if stat_signature(os.fstat(fd), fd=fd) != before_signature:
            raise OSError(f"file changed while hashing: {path}")
    if stat_signature(path.stat(), path=path) != before_signature:
        raise OSError(f"file changed while hashing: {path}")
    return digest.hexdigest()
