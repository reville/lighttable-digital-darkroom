#!/usr/bin/env python3
"""Install the validated archive inside flatpak-builder's private /app prefix."""
import os
from pathlib import Path
import shutil


def install(source: Path, prefix: Path) -> None:
    bundle = prefix / "LightTable"
    shutil.copytree(source / "LightTable", bundle, symlinks=True)
    app_id = "app.lighttable.LightTable"
    targets = {
        "lighttable-desktop": prefix / "bin/lighttable-desktop",
        "lighttable-cli": prefix / "bin/lighttable-cli",
        app_id + ".desktop": prefix / f"share/applications/{app_id}.desktop",
        app_id + ".metainfo.xml": prefix / f"share/metainfo/{app_id}.metainfo.xml",
        "desktop-smoke.py": prefix / "share/lighttable/flatpak/desktop-smoke.py",
        "sandbox-smoke.py": prefix / "share/lighttable/flatpak/sandbox-smoke.py",
        "candidate-provenance.json": prefix / "share/lighttable/flatpak/candidate-provenance.json",
    }
    for name, destination in targets.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    for name in ("lighttable-desktop", "lighttable-cli"):
        (prefix / "bin" / name).chmod(0o755)
    # AppStream composes catalog icons from standard theme sizes; a lone
    # 1024px macOS source icon is not sufficient for the catalog composer.
    # Flatpak rejects exported icons larger than 512px. Keep the original
    # 1024px resource inside the application, outside the exported icon theme.
    for size in (64, 128, 256):
        icon = prefix / f"share/icons/hicolor/{size}x{size}/apps/{app_id}.png"
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / f"icon-{size}.png", icon)
    licenses = prefix / f"share/licenses/{app_id}/lighttable"
    licenses.mkdir(parents=True)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(bundle / name, licenses)
    shutil.copytree(bundle / "Resources/LightTable/licenses", licenses / "dependencies")
    # The private Flatpak installation is managed by Flatpak; do not offer the
    # portable installer's host desktop-registration commands inside it.
    for name in ("install.sh", "uninstall.sh", "desktop-integration.py"):
        (bundle / name).unlink(missing_ok=True)


if __name__ == "__main__":
    install(Path.cwd(), Path(os.environ["FLATPAK_DEST"]))
