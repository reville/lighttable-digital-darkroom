#!/usr/bin/env python3
"""Register an extracted bundle; remove only integrations this installer owns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


APP_ID = "app.lighttable.LightTable"
LEGACY_APP_ID = "org.lighttable.LightTable"


def xdg(name: str, fallback: Path) -> Path:
    candidate = Path(os.environ.get(name, ""))
    return candidate if candidate.is_absolute() else fallback


def desktop_exec(path: Path) -> str:
    value = str(path)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Bundle paths cannot contain control characters")
    # Desktop entries have two escaping layers: the Exec argument, then the
    # key's string value. Percent signs must survive field-code expansion.
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value.replace("\\", "\\\\").replace("%", "%%") + '"'


def desktop_entry(bundle: Path) -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=LightTable\n"
        "Comment=Photo editing and film simulation\n"
        # GIO checks the executable before expanding %% in field codes. Use
        # the POSIX shell as executable and the launcher path as an argument,
        # so a literal percent in the bundle directory works as well.
        f"Exec=/bin/sh {desktop_exec(bundle / 'bin/lighttable-desktop')} %u\n"
        f"Icon={APP_ID}\nTerminal=false\nCategories=Graphics;Photography;\n"
        f"StartupWMClass={APP_ID}\n"
        "MimeType=x-scheme-handler/lighttable;\nStartupNotify=true\n"
    )


def signature(path: Path) -> dict | None:
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_file():
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if path.exists():
        return {"directory": True}
    return None


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(value)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lighttable-link-", dir=path.parent) as temporary:
        link = Path(temporary) / "link"
        link.symlink_to(target)
        os.replace(link, path)


def integrate(action: str, bundle: Path, bin_directory: Path) -> None:
    bundle = bundle.resolve()
    if not bin_directory.is_absolute():
        raise ValueError("--bin-dir must be absolute")
    data = xdg("XDG_DATA_HOME", Path.home() / ".local/share")
    state = xdg("XDG_STATE_HOME", Path.home() / ".local/state")
    record = state / "lighttable/desktop-integration.json"
    desktop = data / "applications" / (APP_ID + ".desktop")
    links = {
        bin_directory / "lighttable": bundle / "bin/lighttable",
        bin_directory / "lighttable-desktop": bundle / "bin/lighttable-desktop",
        data / "icons/hicolor/1024x1024/apps" / (APP_ID + ".png"): bundle / "share/icons/lighttable.png",
    }
    expected_paths = {str(path) for path in (*links, desktop)}
    legacy_paths = {
        str(bin_directory / "lighttable"), str(bin_directory / "lighttable-desktop"),
        str(data / "applications" / (LEGACY_APP_ID + ".desktop")),
        str(data / "icons/hicolor/1024x1024/apps" / (LEGACY_APP_ID + ".png")),
    }
    if record.is_symlink():
        raise ValueError(f"Refusing symbolic link installer record: {record}")
    previous = json.loads(record.read_text()) if record.exists() else {}
    previous_paths = set(previous.get("files", {}))
    if previous and previous_paths not in (expected_paths, legacy_paths):
        raise ValueError("Installation directories changed; uninstall using the previous XDG and --bin-dir settings first")
    if action == "uninstall":
        if not previous:
            print("No registered LightTable installation in these directories")
            return
        if previous.get("bundle") != str(bundle):
            raise ValueError("A different LightTable bundle owns the current launchers; run its uninstall.sh")
        preserved = []
        for path in map(Path, sorted(previous_paths)):
            current = signature(path)
            if current == previous["files"][str(path)]:
                path.unlink()
            elif current is not None:
                preserved.append(str(path))
        record.unlink()
        print("Removed LightTable desktop and command launchers. Bundle, photos, catalogs, and settings were preserved.")
        for path in preserved:
            print(f"Preserved changed integration: {path}")
    else:
        for path in (*links.values(), bundle / "bin/lighttable-desktop-shell", bundle / "Python/bin/python3"):
            if not path.is_file():
                raise ValueError(f"Incomplete bundle: {path}")
        entry = desktop_entry(bundle)
        # Preflight all collisions before replacing any existing integration.
        for path in (*links, desktop):
            current = signature(path)
            if current is not None and current != previous.get("files", {}).get(str(path)):
                raise ValueError(f"Refusing to overwrite an unowned or modified file: {path}")
        # The old ID may belong to another application or a hand-edited entry.
        # Only the exact paths and signatures in our installer record authorize
        # migration; keep every unrelated file and all XDG application data.
        retired = [Path(path) for path in sorted(previous_paths - expected_paths)]
        for path in retired:
            current = signature(path)
            if current is not None and current != previous["files"][str(path)]:
                raise ValueError(f"Refusing to overwrite an unowned or modified file: {path}")
        for path, target in links.items():
            atomic_link(path, target)
        atomic_write(desktop, entry)
        for path in retired:
            path.unlink(missing_ok=True)
        atomic_write(record, json.dumps({
            "bundle": str(bundle),
            "files": {str(path): signature(path) for path in (*links, desktop)},
        }, indent=2) + "\n")
        print(f"Registered LightTable from {bundle}")
        if str(bin_directory) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f"Add {bin_directory} to PATH to run lighttable from your terminal.")
    refresh = shutil.which("update-desktop-database")
    if refresh:
        subprocess.run([refresh, str(desktop.parent)], check=False, timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    try:
        integrate(args.action, args.bundle, args.bin_dir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"LightTable: {error}\n")


if __name__ == "__main__":
    main()
