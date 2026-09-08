# Arch/Omarchy packages

## Local package

`scripts/linux/make-arch-package.py` turns an existing LightTable Linux archive
into a local binary-package recipe. It uses the same pinned runtime that passed
bundle validation, checks the manifest and ELF architecture, and records SHA-256
checksums for both local sources. It does not publish to the AUR or download a
release implicitly.

```sh
python3 scripts/linux/make-arch-package.py \
  dist/LightTable-VERSION-linux-x86_64.tar.gz --output-dir .build/linux/arch-package
cd .build/linux/arch-package
makepkg -s
sudo pacman -U lighttable-bin-*.pkg.tar.*
```

Run `makepkg` as your regular user on matching-architecture Arch Linux, with
`base-devel` installed. Keep `PKGBUILD`, `.SRCINFO`, `app.lighttable.LightTable.desktop`, and
the matching archive together. The generated package is named `lighttable-bin`;
the desktop menu name and commands remain LightTable, `lighttable-desktop`,
and `lighttable`.

| Installed content | Pacman-owned location |
| --- | --- |
| Native shell, Python, engines and resources | `/opt/lighttable` |
| Command symlinks | `/usr/bin/lighttable`, `/usr/bin/lighttable-desktop` |
| Desktop entry | `/usr/share/applications/app.lighttable.LightTable.desktop` |
| Icon | `/usr/share/icons/hicolor/1024x1024/apps/app.lighttable.LightTable.png` |
| License notices | `/usr/share/licenses/lighttable-bin` |

The recipe leaves user data and Omarchy configuration alone. Pacman handles
system file ownership and standard desktop/icon hooks. The per-user portable
installer is excluded from the installed package. When switching from a portable
bundle, run its `uninstall.sh` before installing the package; otherwise its
per-user desktop entry and commands may shadow the system installation.

To upgrade, generate and install a package from the new archive. To uninstall,
run `sudo pacman -R lighttable-bin`. User catalog, photos, presets, preferences,
and caches are preserved.

Omarchy's target is `x86_64`. An ARM64 archive requires
`--experimental-aarch64` and produces an explicitly `aarch64` recipe; its
architecture cannot be relabelled. Tests run the recipe's staging step and
verify ownership boundaries. Ubuntu/ARM package construction does not validate
Arch dependency resolution or establish a tested Omarchy desktop release.

## Release-backed AUR recipe

LightTable's display version is **0.5**; its canonical package version is
`0.5.0`, with release tag `v0.5.0`. After building and validating the clean
x86-64 release archive, generate the three files suitable for an AUR submission:

```sh
python3 scripts/linux/make-aur-package.py \
  dist/LightTable-0.5.0-linux-x86_64.tar.gz \
  --version 0.5.0 --source-revision FULL_RELEASE_COMMIT_SHA \
  --output-dir .build/linux/aur-lighttable-bin
```

Replace `FULL_RELEASE_COMMIT_SHA` with the full 40-character lowercase Git SHA
pinned for the release. The generator verifies archive paths, native ELF
architecture, platform, version, clean source state, and exact source revision.
It also checks the archive's `.sha256` sidecar when present. CI snapshots and
experimental ARM64 bundles remain available through the local generator above;
they cannot become official AUR release recipes.

The output contains `PKGBUILD`, `.SRCINFO`, and
`app.lighttable.LightTable.desktop`. Its source is the fixed official URL:

```text
https://github.com/reville/lighttable-digital-darkroom/releases/download/v0.5.0/LightTable-0.5.0-linux-x86_64.tar.gz
```

The recipe pins SHA-256 hashes for the archive and desktop file, so replacing
the asset's bytes causes verification to fail. It does not use a mutable
`latest` URL or skip checksums. The generator does not download or copy the
archive into the AUR recipe directory and does not publish anything. Publish
the matching official release asset before submitting the recipe to the AUR.

On Arch, run `makepkg -s` in the generated directory to download, verify, and
stage the release, then inspect the resulting package before installing it with
`pacman -U`. `makepkg --printsrcinfo` regenerates the AUR metadata if a maintainer
edits the recipe. For a new application release, regenerate with its new version
and source commit; for a recipe-only correction, increase `--pkgrel` while
keeping the exact application version and checksum. The stable `lighttable-bin`
package name lets pacman and AUR helpers apply normal upgrades.

Both recipes install the canonical `app.lighttable.LightTable` desktop and icon
names. Pacman removes files from an older version of the same package during
upgrade; no package script edits per-user launchers, data, or Omarchy settings.
Portable users should run the active bundle's `uninstall.sh` before switching
to pacman. A portable-to-portable upgrade automatically migrates only legacy
`org.lighttable.LightTable` entries whose installer ownership records still
match, refusing modified files before making any changes.

Recipe format: [Arch PKGBUILD manual](https://man.archlinux.org/man/PKGBUILD.5.en).
