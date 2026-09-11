#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the Finder installer layout without opening or automating Finder."""

import argparse
from pathlib import Path
import subprocess

import dmgbuild


def verify_staged_app(mount_point, _settings):
    """Reject Finder metadata or copying changes that break the app's seal."""
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict",
         str(Path(mount_point) / "LightTable.app")],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    if not (app / "Contents/Info.plist").is_file():
        parser.error(f"Not an application bundle: {app}")
    output = args.output.resolve()
    if output.exists():
        parser.error(f"Output already exists: {output}")
    assets = Path(__file__).resolve().parent / "assets"
    output.parent.mkdir(parents=True, exist_ok=True)
    dmgbuild.build_dmg(
        str(output),
        "LightTable",
        settings={
            "format": "UDZO",
            "create_hook": verify_staged_app,
            "filesystem": "HFS+",
            "files": [
                (str(app), "LightTable.app"),
                str(assets / "READ ME FIRST.txt"),
            ],
            "symlinks": {"Applications": "/Applications"},
            "background": str(assets / "background.png"),
            "window_rect": ((100, 100), (760, 520)),
            "default_view": "icon-view",
            "show_status_bar": False,
            "show_tab_view": False,
            "show_toolbar": False,
            "show_pathbar": False,
            "show_sidebar": False,
            "include_icon_view_settings": True,
            "include_list_view_settings": False,
            "arrange_by": None,
            "icon_size": 112,
            "text_size": 12,
            "label_pos": "bottom",
            "show_icon_preview": False,
            # Setting FinderInfo on the app invalidates its strict code seal.
            # Finder already hides the standard .app extension.
            "hide_extensions": ["READ ME FIRST.txt"],
            "icon_locations": {
                "LightTable.app": (176, 169),
                "Applications": (584, 169),
                "READ ME FIRST.txt": (380, 444),
            },
        },
    )


if __name__ == "__main__":
    main()
