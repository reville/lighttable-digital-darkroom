# SPDX-License-Identifier: GPL-3.0-only
"""Write fatal Python/native stacks directly to disk, outside the log pipe.

The pipe's drain thread dies with the server on a segfault, so stderr alone
can lose the very trace needed for the next launch's diagnostic report.

A host that sets LIGHTTABLE_FAULT_LOG owns that file; the macOS shell reads
it after every unexpected exit. Without one, each server writes its own
``server-fault-<pid>.log`` beside the catalog and removes it on a clean exit,
so a file left behind names a crashed run for the next launch to report.
"""
import atexit
import faulthandler
import os
from pathlib import Path

_fault_file = None
_owned_path = None


def default_directory() -> Path:
    import platform_paths

    return platform_paths.catalog_file().parent / "Diagnostics"


def fault_log_for(pid) -> Path:
    """Where a server that owned its fault file wrote it."""
    return default_directory() / f"server-fault-{int(pid)}.log"


def owned_path() -> Path | None:
    """This process's own fault file, when no host provided one."""
    return _owned_path


def release() -> None:
    """A deliberate exit: stop recording and drop this process's own file."""
    global _fault_file, _owned_path
    try:
        faulthandler.disable()
        if _fault_file is not None:
            _fault_file.close()
            _fault_file = None
        if _owned_path is not None:
            _owned_path.unlink(missing_ok=True)
            _owned_path = None
    except (OSError, ValueError):
        pass


def install():
    global _fault_file, _owned_path
    if _fault_file is not None:
        return
    configured = os.environ.get("LIGHTTABLE_FAULT_LOG")
    try:
        destination = Path(configured) if configured else fault_log_for(os.getpid())
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        _fault_file = os.fdopen(descriptor, "wb", buffering=0)
        faulthandler.enable(file=_fault_file, all_threads=True)
        if not configured:
            _owned_path = destination
            atexit.register(release)
    except (OSError, ValueError):
        # Diagnostic failure must not become a startup failure.
        if _fault_file is not None:
            _fault_file.close()
            _fault_file = None
