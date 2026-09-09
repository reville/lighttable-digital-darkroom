"""Keep the Windows launcher's lifetime pipe out of helper-process stdin."""
from __future__ import annotations

from contextlib import suppress
import os
import sys
from typing import BinaryIO


def _set_standard_input(fd: int) -> None:
    """Update the Win32 standard handle as well as the CRT's file descriptor."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.SetStdHandle.restype = wintypes.BOOL
    if not kernel.SetStdHandle(0xFFFFFFF6, msvcrt.get_osfhandle(fd)):  # STD_INPUT_HANDLE
        raise ctypes.WinError(ctypes.get_last_error())


def separate_launcher_stdin() -> BinaryIO | None:
    """Return the owned control reader, leaving helpers with harmless stdin.

    Call before starting the launcher watcher. On Windows a synchronous pipe
    read can block a child's Python initialization if it inherits that same
    pipe as stdin. A private, non-inheritable duplicate retains launcher EOF;
    both the CRT and Win32 standard-input handles instead point at NUL.
    Other platforms and servers without the launcher's explicit opt-in keep
    their existing stdin. A setup error aborts startup, preserving its cause.
    """
    if sys.platform != "win32" or os.environ.get("LIGHTTABLE_WATCH_STDIN") != "1":
        return None

    control_fd = os.dup(0)
    reader = None
    null_fd = None
    try:
        # os.dup of standard descriptors is inheritable by default on Windows.
        os.set_inheritable(control_fd, False)
        reader = os.fdopen(control_fd, "rb", buffering=0)
        null_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        os.dup2(null_fd, 0, inheritable=True)
        # Use fd 0's handle, not null_fd's: null_fd is closed before returning.
        _set_standard_input(0)
        return reader
    except BaseException:
        # Cleanup must not replace a failure to create or redirect the handle.
        with suppress(OSError):
            if reader is not None:
                reader.close()
            else:
                os.close(control_fd)
        raise
    finally:
        if null_fd is not None:
            with suppress(OSError):
                os.close(null_fd)
