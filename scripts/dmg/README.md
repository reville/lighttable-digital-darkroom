# macOS installer artwork

`../package-dmg.sh /path/to/LightTable.app /path/to/output.dmg` packages an
existing app without changing its contents or generating an update feed.
It requires macOS and `uv`. The output must not already exist.
`package-release.sh` uses the same builder for its DMG.

The 760 × 520 background is the approved orange/blue design, with a black
arrow over the yellow ball. The app and Applications shortcut are real Finder
items, using a common 112-point icon size. The installer has no README item or
unsigned-app launch warning.

`assets/background.svg` is the editable original; `assets/background.png` is
its opaque PNG export. Render the SVG with Sharp, flatten onto `#072250`,
remove alpha, and export PNG when updating it. Keep the 760 × 520 dimensions.
Use the signed, notarized distribution app when preparing a public installer.

The pinned [dmgbuild](https://dmgbuild.readthedocs.io/en/latest/settings.html)
tool writes the background alias, icon positions, and window preferences
directly into `.DS_Store`, without launching or scripting Finder.
