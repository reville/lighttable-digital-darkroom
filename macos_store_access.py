# SPDX-License-Identifier: GPL-3.0-only
"""Resolve native-selected bookmarks in each Store Python process.

Keep scopes alive until process exit: render workers may outlive their HTTP
request. The file lives in the app container and is written atomically by Swift.
"""
import base64
import ctypes
import json
import os
import threading
from pathlib import Path

_lock = threading.RLock()
_active = {}
_cf = None

def _resolve(data):
    global _cf
    if _cf is None:
        _cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        ptr = ctypes.c_void_p
        for name, args, result in [
            ('CFDataCreate', [ptr, ptr, ctypes.c_long], ptr),
            ('CFURLCreateByResolvingBookmarkData', [ptr, ptr, ctypes.c_ulong, ptr, ptr, ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ptr)], ptr),
            ('CFURLStartAccessingSecurityScopedResource', [ptr], ctypes.c_bool),
            ('CFURLStopAccessingSecurityScopedResource', [ptr], None),
            ('CFRelease', [ptr], None),
            ('CFErrorGetCode', [ptr], ctypes.c_long),
        ]:
            f = getattr(_cf, name); f.argtypes = args; f.restype = result
    buffer = ctypes.create_string_buffer(data)
    cfdata = _cf.CFDataCreate(None, buffer, len(data))
    stale = ctypes.c_bool()
    error = ctypes.c_void_p()
    try:
        url = _cf.CFURLCreateByResolvingBookmarkData(None, cfdata, 0x100, None, None, ctypes.byref(stale), ctypes.byref(error))
    finally:
        _cf.CFRelease(cfdata)
    error_code = _cf.CFErrorGetCode(error) if error.value else None
    if error.value:
        _cf.CFRelease(error)
    if not url or stale.value:
        if url: _cf.CFRelease(url)
        raise PermissionError(f'Bookmark resolution failed (code={error_code}, stale={stale.value})')
    # Implicit security scope is acquired by resolution and retained by the URL.
    return url

def refresh():
    filename = os.environ.get('LIGHTTABLE_STORE_GRANTS_FILE')
    if not filename:
        return
    with _lock:
        grants = json.loads(Path(filename).read_text())
        for encoded in grants.values():
            if encoded not in _active:
                _active[encoded] = _resolve(base64.b64decode(encoded, validate=True))

def close():
    with _lock:
        for url in _active.values():
            _cf.CFRelease(url)
        _active.clear()
