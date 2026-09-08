# Local Arch/Omarchy package

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
`base-devel` installed. Keep `PKGBUILD`, `org.lighttable.LightTable.desktop`, and
the matching archive together. The generated package is named `lighttable-bin`;
the desktop menu name and commands remain LightTable, `lighttable-desktop`,
and `lighttable`.

| Installed content | Pacman-owned location |
| --- | --- |
| Native shell, Python, engines and resources | `/opt/lighttable` |
| Command symlinks | `/usr/bin/lighttable`, `/usr/bin/lighttable-desktop` |
| Desktop entry | `/usr/share/applications/org.lighttable.LightTable.desktop` |
| Icon | `/usr/share/icons/hicolor/1024x1024/apps/org.lighttable.LightTable.png` |
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

Recipe format: [Arch PKGBUILD manual](https://man.archlinux.org/man/PKGBUILD.5.en).
