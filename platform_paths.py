"""Writable LightTable locations shared by the server and command-line client.

Linux follows the XDG base-directory specification. XDG environment values
must be absolute paths; relative and empty values fall back to the standard
home-directory locations. Explicit LIGHTTABLE overrides retain their existing
expanduser behavior. Resolving a path never creates a directory.

Desktop hosts must use the same layout. macOS and Windows retain their
historical defaults, including source-tree prefs/cache when run without a host.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def _override(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def xdg_directory(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    if value and Path(value).is_absolute():
        return Path(value)
    return fallback


def data_directory(*, windows_home_fallback: bool = False) -> Path:
    if is_linux():
        return xdg_directory("XDG_DATA_HOME", Path.home() / ".local/share") / "lighttable"
    if sys.platform == "win32":
        fallback = Path.home() if windows_home_fallback else Path.home() / "AppData/Local"
        return Path(os.environ.get("LOCALAPPDATA", str(fallback))) / "LightTable"
    return Path.home() / "Library/Application Support/LightTable"


def config_directory() -> Path:
    if is_linux():
        return xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "lighttable"
    return data_directory()


def state_directory() -> Path:
    if is_linux():
        return xdg_directory("XDG_STATE_HOME", Path.home() / ".local/state") / "lighttable"
    return data_directory()


def catalog_file() -> Path:
    return _override("LIGHTTABLE_CATALOG_FILE") or data_directory() / "Catalog/library.sqlite3"


def cache_directory(app: Path) -> Path:
    default = (xdg_directory("XDG_CACHE_HOME", Path.home() / ".cache") / "lighttable"
               if is_linux() else app / "cache")
    return _override("LIGHTTABLE_CACHE_DIR") or default


def preferences_file(app: Path) -> Path:
    return _override("LIGHTTABLE_PREFS_FILE") or (config_directory() if is_linux() else app) / "prefs.json"


def presets_file(app: Path) -> Path:
    return _override("LIGHTTABLE_PRESETS_FILE") or (data_directory() if is_linux() else app) / "presets.json"


def generated_data_directory(preferences: Path) -> Path:
    # An explicit prefs file is also the existing per-profile isolation anchor.
    if is_linux() and not os.environ.get("LIGHTTABLE_PREFS_FILE"):
        return data_directory()
    return preferences.parent


def ai_directory(preferences: Path) -> Path:
    default = (generated_data_directory(preferences)
               if is_linux() or "LIGHTTABLE_PREFS_FILE" in os.environ
               else Path.home() / "Library/Application Support/LightTable")
    return _override("LIGHTTABLE_AI_DIR") or default / "AI Index"


def model_directory() -> Path:
    return _override("LIGHTTABLE_MODEL_DIR") or data_directory() / "Models"


def instance_directory() -> Path:
    configured = _override("LIGHTTABLE_INSTANCE_DIR")
    if configured:
        return configured
    if is_linux():
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and Path(runtime).is_absolute():
            return Path(runtime) / "lighttable/instances"
        return state_directory() / "instances"
    return data_directory(windows_home_fallback=True) / "instances"


def profile_root() -> Path:
    return _override("LIGHTTABLE_PROFILE_ROOT") or data_directory(windows_home_fallback=True) / "Profiles"


def server_log_file() -> Path:
    return (_override("LIGHTTABLE_SERVER_LOG") or _override("LIGHTTABLE_LOG_FILE")
            or state_directory() / "logs/server.log")
