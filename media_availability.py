"""Metadata-only availability checks; never hydrate a cloud file to inspect it."""
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import sys
from server_localization import T

# macOS sys/stat.h: SF_DATALESS means the file's content is not local.
# Other platforms do not expose st_flags, so ordinary files remain local.
SF_DATALESS = 0x40000000
CLOUD_MESSAGE = ("This photo is stored in the cloud and is not downloaded. "
                 "In Finder, choose Download Now or Keep Downloaded, then "
                 "rescan the source in LightTable.")


@lru_cache(maxsize=1)
def _macos_getxattr():
    # Python's macOS build does not expose os.getxattr. A null output buffer
    # asks libSystem only for the attribute size, never its value or file data.
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    getxattr = library.getxattr
    getxattr.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
    getxattr.restype = ctypes.c_ssize_t
    return getxattr


def _dropbox_placeholder(path: Path | str, *, stat=None) -> bool:
    if sys.platform != 'darwin':
        return False
    try:
        stat = stat if stat is not None else Path(path).stat()
        if getattr(stat, 'st_size', None) != 0:
            return False
        return _macos_getxattr()(os.fsencode(path), b'com.dropbox.placeholder',
                                None, 0, 0, 0) >= 0
    except (OSError, ValueError):
        return False


def cloud_message(path: Path | str | None = None) -> str:
    if path is not None and _dropbox_placeholder(path):
        return T('This photo is stored online in Dropbox. In Finder, choose “Make available offline,” then retry.')
    return T("This photo is stored in the cloud and is not downloaded. "
             "In Finder, choose Download Now or Keep Downloaded, then "
             "rescan the source in LightTable.")


def from_stat(stat) -> str:
    return "cloud-only" if getattr(stat, "st_flags", 0) & SF_DATALESS else "local"


def availability(path: Path | str, *, stat=None) -> str:
    try:
        stat = stat if stat is not None else Path(path).stat()
        state = from_stat(stat)
        if state == 'local' and _dropbox_placeholder(path, stat=stat):
            return 'cloud-only'
        return state
    except OSError:
        return "unavailable"


def index_availability(path: Path | str) -> str:
    """Check for image bytes before indexing, without opening a cloud file.

    Dropbox's older sync placeholders can be zero bytes without the macOS
    dataless flag. Only its marker distinguishes them from genuinely empty files.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return "unavailable"
    state = availability(path, stat=stat)
    if state != "local":
        return state
    return "empty" if stat.st_size == 0 else "local"


def require_local(path: Path | str, *, stat=None) -> None:
    state = availability(path, stat=stat)
    if state != "local":
        raise OSError(cloud_message(path) if state == "cloud-only" else
                      T("This photo is unavailable. Reconnect its source and rescan."))
