"""Complete, stable file identities for decisions that must not use a prefix."""
from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

import media_availability
from server_localization import T


_WINDOWS = os.name == "nt"


class _WindowsBindings:
    """Native file handles; initialized lazily on Windows, once per process."""

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
        self.reopen_file = kernel.ReOpenFile
        self.reopen_file.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD]
        self.reopen_file.restype = wintypes.HANDLE
        self.query_file = kernel.GetFileInformationByHandleEx
        self.query_file.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                   wintypes.LPVOID, wintypes.DWORD]
        self.query_file.restype = wintypes.BOOL
        self.close_handle = kernel.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL
        self.open_osfhandle = msvcrt.open_osfhandle
        self.get_osfhandle = msvcrt.get_osfhandle
        self.invalid_handle = ctypes.c_void_p(-1).value
        self.basic_info = FileBasicInfo
        self.ctypes = ctypes

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
            raise OSError(T("The filesystem does not provide a file change time"))
        return int(info.ChangeTime)

    def reopen_content_fd(self, fd):
        # ReOpenFile preserves the object identity but has an independent file
        # pointer. Deny concurrent writers/deleters for the complete read; a
        # timestamp cannot detect every write made within one clock tick.
        # OPEN_NO_RECALL supplements the attribute-only availability checks.
        handle = self.reopen_file(self.get_osfhandle(fd), 0x80000000, 0x1,
                                  0x00100000 | 0x08000000)
        if handle == self.invalid_handle:
            raise self.ctypes.WinError()
        try:
            return self.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            self.close_handle(handle)
            raise


@lru_cache(maxsize=1)
def _windows_bindings():
    return _WindowsBindings()


def _stat_fields(stat) -> tuple[int, int, int, int, int]:
    # CPython 3.13 path stat keeps legacy ctime=creation, while fstat may
    # expose native change time. Normalize both to birthtime on Windows;
    # the native timestamp and complete digest below validate the revision.
    ctime = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns) if _WINDOWS else stat.st_ctime_ns
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            ctime)


def _windows_change_time(stat, *, path=None, fd=None):
    if fd is None and path is None:
        raise OSError(T("A file path or descriptor is required for Windows identity"))
    api = _windows_bindings()
    owned_fd = api.open_metadata_fd(path) if fd is None else None
    if owned_fd is not None:
        fd = owned_fd
    try:
        before = api.change_time(fd)
        if _stat_fields(os.fstat(fd)) != _stat_fields(stat):
            raise OSError(T("File changed before querying its identity"))
        after = api.change_time(fd)
        if before != after:
            raise OSError(T("File changed while querying its identity"))
        return after
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


def _require_windows_local(stat, path=None):
    media_availability.require_local(path, stat=stat)
    # OFFLINE, RECALL_ON_OPEN and RECALL_ON_DATA_ACCESS are exposed by Windows
    # stat without reading bytes. A read must never hydrate these placeholders.
    if getattr(stat, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000):
        raise OSError(T("This photo is unavailable. Reconnect its source and rescan."))


def _read_descriptor_digest(fd, size):
    """Read at most the observed size plus one byte, preserving the position."""
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.blake2b(digest_size=16)
        remaining = size
        while remaining >= 0:
            block = os.read(fd, min(1 << 20, remaining + 1))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
        if remaining != 0:
            raise OSError(T("File changed while querying its identity"))
        return digest.hexdigest()
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def _windows_content_signature(stat, *, path=None, fd=None):
    if fd is None and path is None:
        raise OSError(T("A file path or descriptor is required for Windows identity"))
    _require_windows_local(stat, path)
    api = _windows_bindings()
    with ExitStack() as handles:
        if fd is None:
            fd = api.open_metadata_fd(path)
            handles.callback(os.close, fd)
        _require_windows_local(os.fstat(fd), path)
        before = _windows_change_time(stat, fd=fd)
        read_fd = api.reopen_content_fd(fd)
        handles.callback(os.close, read_fd)
        _require_windows_local(os.fstat(read_fd), path)
        if _windows_change_time(stat, fd=read_fd) != before:
            raise OSError(T("File changed before querying its identity"))
        digest = _read_descriptor_digest(read_fd, stat.st_size)
        if _windows_change_time(stat, fd=read_fd) != before:
            raise OSError(T("File changed while querying its identity"))
        # The metadata handle may have been opened before a pathname swap.
        # Recheck that path while this object's deny-write/delete lock is held.
        if path is not None and _stat_fields(Path(path).stat()) != _stat_fields(stat):
            raise OSError(T("File changed while querying its identity"))
        return before, digest


def stat_signature(stat, *, path=None, fd=None) -> tuple[int | str, ...]:
    signature = _stat_fields(stat)
    if _WINDOWS:
        # ChangeTime is a timestamp, not a revision counter: same-size writes
        # can preserve it and mtime. Windows therefore rereads all local bytes
        # for every identity check, including warm-cache checks. This costs
        # disk I/O but never reuses stale content solely on timestamp equality.
        signature += _windows_content_signature(stat, path=path, fd=fd)
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
        raise OSError(T("file changed before hashing: {path}", path=path))
    if expected_revision is not None and expected_revision != (
            before.st_size, before.st_mtime_ns):
        raise OSError(T("file changed before hashing: {path}", path=path))
    if _WINDOWS:
        # The strong signature already read and validated the complete file.
        # Do not recursively call content_hash or perform another full read.
        return before_signature[-1]
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as stream:
        fd = stream.fileno()
        if stat_signature(os.fstat(fd), fd=fd) != before_signature:
            raise OSError(T("file changed before hashing: {path}", path=path))
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
        if stat_signature(os.fstat(fd), fd=fd) != before_signature:
            raise OSError(T("file changed while hashing: {path}", path=path))
    if stat_signature(path.stat(), path=path) != before_signature:
        raise OSError(T("file changed while hashing: {path}", path=path))
    return digest.hexdigest()
