"""Write fatal Python/native stacks directly to disk, outside the log pipe.

The pipe's drain thread dies with the server on a segfault, so stderr alone
can lose the very trace needed for the next launch's diagnostic report.
"""
import faulthandler
import os
from pathlib import Path

_fault_file = None


def install():
    global _fault_file
    path = os.environ.get("LIGHTTABLE_FAULT_LOG")
    if not path or _fault_file is not None:
        return
    try:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        _fault_file = os.fdopen(descriptor, "wb", buffering=0)
        faulthandler.enable(file=_fault_file, all_threads=True)
    except (OSError, ValueError):
        # Diagnostic failure must not become a startup failure.
        if _fault_file is not None:
            _fault_file.close()
            _fault_file = None
