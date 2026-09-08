"""Stat-only availability checks; never hydrate a cloud file to inspect it."""
from pathlib import Path

# macOS sys/stat.h: SF_DATALESS means the file's content is not local.
# Other platforms do not expose st_flags, so ordinary files remain local.
SF_DATALESS = 0x40000000
CLOUD_MESSAGE = ("This photo is stored in the cloud and is not downloaded. "
                 "In Finder, choose Download Now or Keep Downloaded, then "
                 "rescan the source in LightTable.")


def from_stat(stat) -> str:
    return "cloud-only" if getattr(stat, "st_flags", 0) & SF_DATALESS else "local"


def availability(path: Path | str, *, stat=None) -> str:
    try:
        return from_stat(stat if stat is not None else Path(path).stat())
    except OSError:
        return "unavailable"


def index_availability(path: Path | str) -> str:
    """Check for image bytes before indexing, without opening a cloud file.

    Older sync placeholders can be zero bytes without the macOS dataless
    flag. An empty file is not evidence that a downloadable original exists.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return "unavailable"
    state = from_stat(stat)
    if state != "local":
        return state
    return "empty" if stat.st_size == 0 else "local"


def require_local(path: Path | str, *, stat=None) -> None:
    state = availability(path, stat=stat)
    if state != "local":
        raise OSError(CLOUD_MESSAGE if state == "cloud-only" else
                      "This photo is unavailable. Reconnect its source and rescan.")
