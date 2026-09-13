# LightTable Debian / Ubuntu Packaging

This directory contains definitions and scripts for packaging LightTable as a Debian package (`.deb`) for Debian 12+, Ubuntu 24.04+, Linux Mint, and Pop!_OS.

## Architecture

The `.deb` package installs the verified standalone bundle to `/opt/lighttable`, creates symlinks `/usr/bin/lighttable` and `/usr/bin/lighttable-desktop`, installs the desktop launcher to `/usr/share/applications/app.lighttable.LightTable.desktop`, and installs the application icon.

In `/opt/lighttable/installation-owner.json`, the installation owner is set to `{"owner": "deb"}` so that APT manages file lifecycle and updates.

### Runtime Dependencies
- `gtk3`
- `webkit2gtk-4.1 (>= 2.40)`
- `libopenblas0`
- `openssl`
- `libgl1`
- `libvulkan1`
- `liblcms2-2`
- `xdotool`
- `xdg-utils`
- `hicolor-icon-theme`
- `desktop-file-utils`

## Generating a Debian Package

```bash
python3 scripts/linux/make-deb-package.py \
  dist/LightTable-0.6.2-linux-x86_64.tar.gz \
  --output-dir dist/deb
```

This outputs:
`dist/deb/lighttable_0.6.2-1_amd64.deb`

## Installing on Ubuntu / Debian

```bash
sudo apt install ./lighttable_0.6.2-1_amd64.deb
```

APT will resolve and install all necessary system libraries automatically.

## Uninstalling

```bash
sudo apt remove lighttable
```

User libraries, edits, and preferences in `~/.local/share/LightTable` and `~/.config/LightTable` are preserved upon package removal.
