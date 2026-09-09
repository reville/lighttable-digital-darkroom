# macOS installer artwork

`../package-dmg.sh /path/to/LightTable.app /path/to/output.dmg` packages an
existing app without changing its contents or generating an update feed.
It requires macOS and `uv`. The output must not already exist.
`package-release.sh` uses the same builder for its DMG.

The 760 × 520 background is the approved orange/blue design, with a black
arrow over the yellow ball. The app, Applications shortcut, and README are
real Finder items. The underlined background text points to the README below;
the text itself is part of the image. Finder uses a common 112-point icon size
so the README and its label fit beneath the beta notice.

`assets/background.svg` is the editable original; `assets/background.png` is
its opaque PNG export. Render the SVG with Sharp, flatten onto `#072250`,
remove alpha, and export PNG when updating it. Keep the 760 × 520 dimensions.
The current artwork and README describe the unsigned beta; revise that copy
before distributing an Apple Developer ID signed release.

The pinned [dmgbuild](https://dmgbuild.readthedocs.io/en/latest/settings.html)
tool writes the background alias, icon positions, and window preferences
directly into `.DS_Store`, without launching or scripting Finder.
