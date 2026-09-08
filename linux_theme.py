"""Read the active Omarchy palette without running or modifying theme files.

Omarchy 4 stages colors.toml under ~/.local/state/omarchy/current/theme;
Omarchy 3 used ~/.config/omarchy/current/theme. Reopening the palette on each
request also handles Omarchy's directory swaps and older symlink-based themes.
See https://github.com/omacom/omarchy/blob/v4.0.2/docs/theming.md.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys
import tomllib

from platform_paths import xdg_directory

_MAX_PALETTE_BYTES = 64 * 1024
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
_COLORS = {
    "background": ("background", "bg"),
    "foreground": ("foreground", "fg"),
    "accent": ("accent", "color4"),
    "selection": ("selection", "selection_background"),
    "muted": ("muted", "color8"),
    "dark_background": ("dark_background", "dark_bg"),
    "darker_background": ("darker_background", "darker_bg"),
    "lighter_background": ("lighter_background", "lighter_bg"),
    "dark_foreground": ("dark_foreground", "dark_fg"),
    "light_foreground": ("light_foreground", "light_fg"),
    "bright_foreground": ("bright_foreground", "bright_fg"),
}


def _theme_directories() -> list[Path]:
    home = Path.home()
    # Upstream uses these literal home paths. Also accept XDG relocations,
    # checking every v4 location before the legacy v3 locations.
    state = home / ".local/state"
    config = home / ".config"
    bases = [xdg_directory("XDG_STATE_HOME", state), state,
             xdg_directory("XDG_CONFIG_HOME", config), config]
    return list(dict.fromkeys(base / "omarchy/current/theme" for base in bases))


def _palette_file(path: Path) -> dict:
    # O_NONBLOCK plus the fstat check avoids hanging on a FIFO or reading a
    # device. Symlinks to regular palette files are valid in older Omarchy.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PALETTE_BYTES:
            return {}
        data = stream.read(_MAX_PALETTE_BYTES + 1)
    if len(data) > _MAX_PALETTE_BYTES:
        return {}
    return tomllib.loads(data.decode("utf-8"))


def desktop_theme() -> dict:
    """Return only validated color data; absence leaves system mode to the UI."""
    fallback = {"source": "system", "mode": None, "colors": {}}
    if not sys.platform.startswith("linux"):
        return fallback
    for directory in _theme_directories():
        try:
            raw = _palette_file(directory / "colors.toml")
            colors = {}
            for role, aliases in _COLORS.items():
                for name in aliases:
                    value = raw.get(name)
                    if isinstance(value, str) and _HEX.fullmatch(value):
                        colors[role] = value.lower()
                        break
            if "background" not in colors or "foreground" not in colors:
                continue
            mode = raw.get("mode")
            if mode not in ("light", "dark"):
                mode = "light" if (directory / "light.mode").is_file() else None
            return {"source": "omarchy", "mode": mode, "colors": colors}
        except (OSError, UnicodeError, ValueError):
            # In-flight activation, unreadable files and invalid TOML must
            # never prevent opening the photo library.
            continue
    return fallback
