#!/usr/bin/env python3
"""Exercise the packaged Windows runtime without opening the desktop UI."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("resources", type=Path)
    parser.add_argument("shell", type=Path)
    args = parser.parse_args()
    resources = args.resources.resolve()
    vendor = (resources / "vendor" / "spektrafilm" / "src").resolve()
    runtime_paths = {Path(entry).resolve() for entry in sys.path if entry}
    assert resources in runtime_paths
    assert vendor in runtime_paths

    with tempfile.TemporaryDirectory(prefix="lighttable-windows-smoke-") as temp:
        temp_path = Path(temp)
        os.environ["LIGHTTABLE_DIR"] = str(temp_path)
        os.environ["LIGHTTABLE_CACHE_DIR"] = str(temp_path / "cache")
        os.environ["LIGHTTABLE_PREFS_FILE"] = str(temp_path / "prefs.json")
        os.environ["LIGHTTABLE_PRESETS_FILE"] = str(temp_path / "presets.json")

        import OpenImageIO  # noqa: F401
        import exiv2  # noqa: F401
        import rawpy  # noqa: F401
        import server

        assert args.shell.is_file()
        assert server.RUST_BIN.name == "spektrafilm-rs.exe"
        assert server.RUST_WORKER_BIN is not None
        assert server.RUST_WORKER_BIN.name == "lighttable-engine.exe"
        assert server.RUST_BIN.is_file()
        assert server.RUST_WORKER_BIN.is_file()
        for profile in server.color_pipeline.ICC_PROFILES.values():
            assert profile.is_file(), profile

        source = temp_path / "source.jpg"
        destination = temp_path / "converted.tif"
        Image.new("RGB", (32, 24), (80, 120, 160)).save(source)
        server.platform_image.convert_processed_to_tiff(
            source,
            destination,
            app_root=resources,
            output_space="display_p3",
            force_portable=True,
        )
        with Image.open(destination) as converted:
            assert converted.size == (32, 24)
            assert converted.info.get("icc_profile")

    print("Packaged Windows runtime smoke passed")


if __name__ == "__main__":
    main()
