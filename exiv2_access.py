# SPDX-License-Identifier: GPL-3.0-only
"""The one way LightTable reaches python-exiv2.

libexiv2 0.28 starts its XMP toolkit lazily, on the first XMP read, and keeps
registered namespaces in a process-wide table. Neither step is thread-safe,
and the binding releases the GIL while it opens and reads a file. Two threads
reading their first photos at the same moment could therefore crash the
server with SIGSEGV or leave it spinning. Doing both steps once, under a lock,
before the first read closes that window; concurrent reads are safe after it.

Import exiv2 only through ``load()``. ``tests/test_exiv2_access.py`` enforces
that for every application module.
"""
from __future__ import annotations

import threading

# Namespaces LightTable writes. Registering them later, while other threads
# parse XMP, would modify the table those readers consult.
XMP_NAMESPACES = (("http://ns.adobe.com/lightroom/1.0/", "lr"),)

_lock = threading.Lock()
_binding = None


def load():
    """Return python-exiv2 after its one-time, process-wide XMP setup.

    Raises ImportError when the binding is not installed, like a plain import.
    """
    global _binding
    if _binding is None:
        with _lock:
            if _binding is None:
                import exiv2

                exiv2.XmpParser.initialize()
                for uri, prefix in XMP_NAMESPACES:
                    try:
                        exiv2.XmpProperties.registerNs(uri, prefix)
                    except Exception:  # noqa: BLE001 - exiv2 may already define it
                        pass
                _binding = exiv2
    return _binding
