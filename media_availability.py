# SPDX-License-Identifier: GPL-3.0-only
"""Metadata-only availability checks; never hydrate a cloud file to inspect it."""
import ctypes
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import sys
from server_localization import T

# macOS sys/stat.h: SF_DATALESS means the file's content is not local.
SF_DATALESS = 0x40000000
# Windows reports these without reading content. RECALL_ON_OPEN shares a bit
# with FILE_ATTRIBUTE_EA, so only trust that bit on a Cloud Files reparse point.
# https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
IO_REPARSE_TAG_CLOUD = 0x9000001A
CLOUD_MESSAGE = ("This photo is stored online or is not fully downloaded. "
                 "Make it available offline in your cloud storage app, wait "
                 "for the download to finish, then retry.")


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


def cloud_provider(path: Path | str | None) -> str | None:
    """Name known sync roots for recovery copy, never infer availability.

    Path checks are lexical: no resolution, directory walks, or file reads.
    Unrecognized/custom sync roots receive provider-neutral instructions.
    """
    if path is None:
        return None
    windows = sys.platform == 'win32'
    path_type = PureWindowsPath if windows else Path
    candidate = path_type(os.path.normpath(os.fspath(path)))
    home = path_type(Path.home())

    def inside(root):
        return candidate == root or root in candidate.parents

    if not windows:
        storage = home / 'Library' / 'CloudStorage'
        if storage in candidate.parents:
            domain = candidate.relative_to(storage).parts[0].casefold()
            for prefix, provider in (('googledrive', 'Google Drive'),
                                     ('google drive', 'Google Drive'),
                                     ('onedrive', 'OneDrive'), ('dropbox', 'Dropbox'),
                                     ('box', 'Box')):
                if domain == prefix or domain.startswith(prefix + '-'):
                    return provider
        if inside(home / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs'):
            return 'iCloud Drive'
        if inside(path_type('/Volumes/GoogleDrive')):
            return 'Google Drive'
    else:
        for key in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
            root = os.environ.get(key)
            if root and inside(path_type(root)):
                return 'OneDrive'
        for folder, provider in (('OneDrive', 'OneDrive'), ('Google Drive', 'Google Drive'),
                                 ('Dropbox', 'Dropbox'), ('Box', 'Box'),
                                 ('iCloudDrive', 'iCloud Drive')):
            if inside(home / folder):
                return provider
    if path is not None and _dropbox_placeholder(path):
        return 'Dropbox'
    return None


def cloud_message(path: Path | str | None = None) -> str:
    provider = cloud_provider(path)
    if provider == 'Dropbox' and sys.platform == 'darwin':
        return T('This photo is stored online in Dropbox. In Finder, choose “Make available offline,” then retry.')
    if provider == 'Dropbox':
        return T('This photo is stored online in Dropbox. Make it available offline in Dropbox, wait for the download to finish, then retry.')
    if provider == 'Google Drive':
        return T('This photo is stored online in Google Drive. Make it available offline in Drive for desktop, wait for the download to finish, then retry.')
    if provider == 'OneDrive':
        return T('This photo is stored online in OneDrive. Choose “Always keep on this device,” wait for the download to finish, then retry.')
    if provider == 'iCloud Drive':
        return T('This photo is stored online in iCloud Drive. Download the original with iCloud Drive, wait for the download to finish, then retry.')
    if provider == 'Box':
        return T('This photo is stored online in Box. Make its containing folder available offline in Box Drive, wait for the download to finish, then retry.')
    return T("This photo is stored online or is not fully downloaded. "
             "Make it available offline in your cloud storage app, wait "
             "for the download to finish, then retry.")


def from_stat(stat) -> str:
    attributes = getattr(stat, 'st_file_attributes', 0)
    # Cloud Files tag variants differ in bits 12..15.
    cloud_tag = (getattr(stat, 'st_reparse_tag', 0) & ~0xF000) == IO_REPARSE_TAG_CLOUD
    remote = (attributes & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
              or (cloud_tag and attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN))
    return "cloud-only" if getattr(stat, "st_flags", 0) & SF_DATALESS or remote else "local"


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
