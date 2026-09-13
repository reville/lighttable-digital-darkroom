# LightTable Fedora / RPM Packaging

This directory contains the RPM specification template and runbook for packaging LightTable for Fedora, Red Hat Enterprise Linux (RHEL), CentOS Stream, AlmaLinux, Rocky Linux, and openSUSE.

## Architecture

The RPM wraps the exact verified, portable Linux bundle (`LightTable-<version>-linux-x86_64.tar.gz`) into `/opt/lighttable`, symlinks binaries into `/usr/bin/`, installs the desktop entry and application icons, and marks the installation owner as `"rpm"` in `/opt/lighttable/installation-owner.json`.

Because LightTable bundles its tested Python runtime and native render engines, the RPM depends only on standard system-level shared libraries:
- `gtk3`
- `webkit2gtk4.1`
- `openblas`
- `openssl`
- `libglvnd-glx`
- `vulkan-loader`
- `lcms2`
- `xdotool`
- `xdg-utils`
- `hicolor-icon-theme`
- `desktop-file-utils`

## Generating an RPM Package

To generate an RPM from a release archive:

```bash
python3 scripts/linux/make-rpm-package.py \
  dist/LightTable-0.6.2-linux-x86_64.tar.gz \
  --output-dir dist/rpm \
  --build
```

If `rpmbuild` is installed on the host, this outputs:
- `dist/rpm/RPMS/x86_64/lighttable-0.6.2-1.fc40.x86_64.rpm`
- `dist/rpm/SPECS/lighttable.spec`

## Installing on Fedora

Users install the package using DNF, which automatically satisfies all dependencies:

```bash
sudo dnf install ./lighttable-0.6.2-1.fc40.x86_64.rpm
```

To run LightTable:
```bash
lighttable-desktop  # GUI
lighttable --help   # CLI
```

## Uninstalling

```bash
sudo dnf remove lighttable
```

User catalogs in `~/.local/share/LightTable` and configuration in `~/.config/LightTable` are preserved upon package removal.
