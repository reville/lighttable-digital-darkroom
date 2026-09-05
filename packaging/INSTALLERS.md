# Installer manifests

`scripts/generate-installers.py` creates release-specific package files using the
SHA-256 of completed local release artifacts. It never substitutes unchecked or
placeholder hashes, publishes a package, or chooses an application license.
Run it after signing, notarization, and all other changes to release files.

```sh
python3 scripts/generate-installers.py \
  --version 0.1.0 \
  --artifacts-dir dist \
  --output-dir dist/installers \
  --channels homebrew
```

The output directory must be new or empty. Versions are stable `MAJOR.MINOR.PATCH`
without a `v` prefix; each component must fit a Windows version resource. The
download tag is `vMAJOR.MINOR.PATCH` in `reville/lighttable-digital-darkroom`.

| Channel | Required artifact | Generated location |
| --- | --- | --- |
| Homebrew | `LightTable-VERSION-macos-arm64.dmg` | `homebrew/Casks/lighttable.rb` |
| Scoop | `LightTable-VERSION-windows-x64.zip` | `scoop/bucket/lighttable.json` |
| WinGet | `LightTable-VERSION-windows-x64-setup.exe` | `winget/manifests/n/NicholasReville/LightTable/VERSION/` |
| Chocolatey | `LightTable-VERSION-windows-x64-setup.exe` | `chocolatey/lighttable/` |

Omit `--channels` to generate all channels whose artifact is present. Explicitly
requested channels fail when their artifact is missing. Windows channels require
`--license` with the chosen license name or SPDX identifier. Chocolatey also
requires `--license-url` pointing to the published license. Supply these only
after the owner has selected the license. `--require-license-acceptance` sets
Chocolatey's corresponding metadata when the selected license needs it.

`SHA256SUMS` covers all recognized installer files present, plus the optional
`LightTable-VERSION-macos-arm64.zip` Sparkle update archive and `appcast.xml`
when placed at the top level of the artifact directory. Publish this file beside
the artifacts. The generator verifies the portable ZIP has its GUI and CLI at
`LightTable/LightTable.exe` and `LightTable/lighttable.cmd`; platform release
validation must establish that these binaries actually run.

Homebrew installs `LightTable.app`, links its bundled `lighttable-cli` as
`lighttable`, and requires Apple silicon and macOS 13 or newer. The cask marks
the app as updating itself through Sparkle. Publish its generated `Casks` folder
to `reville/homebrew-lighttable`. Once the tap contains a real released cask:

```sh
brew tap reville/lighttable
brew install --cask lighttable
```

Homebrew's main catalog requires a separate submission and acceptance. These
generated files alone do not make an untapped `brew install lighttable` available.

Scoop extracts the portable ZIP's `LightTable` directory and explicitly shims
`lighttable.cmd`. A dedicated bucket repository with `bucket/lighttable.json`
at its root avoids cloning the entire app source. The generated `scoop` directory
is ready to be that repository's contents. A `packaging/scoop` subdirectory in
the app repository cannot itself be registered as a Scoop bucket; before a
bucket is published, the generated JSON may be installed by its release URL.

Submit WinGet's three generated YAML files to `microsoft/winget-pkgs` after a
Windows machine validates them with `winget validate --manifest PATH` and checks
local-manifest installation. The package ID is `NicholasReville.LightTable`.
The manifest describes the same per-user NSIS installer used by Chocolatey.
That installer owns the user PATH entry for the `lighttable` command.

On Windows, run `choco pack lighttable.nuspec` in the generated
`chocolatey/lighttable` folder, then install, upgrade, and uninstall the local
package under the intended user's account before publishing it. The supplied
uninstall script invokes the registered per-user NSIS uninstaller. Homebrew,
Scoop, and Chocolatey manifests do not delete catalogs, preferences, or photos;
the native installer's corresponding preservation behavior needs its own test.

Focused generator validation:
`python3 -m unittest discover -s tests -p test_installer_manifests.py`.
For every channel, fresh installation, CLI access, upgrade, and uninstall on the
target platform remain release checks. Manifest generation is not installation
proof or public package registration.

Reference formats: [Homebrew cask cookbook](https://docs.brew.sh/Cask-Cookbook),
[Scoop app manifests](https://github.com/ScoopInstaller/Scoop/wiki/App-Manifests),
[WinGet manifests](https://learn.microsoft.com/en-us/windows/package-manager/package/manifest),
[Chocolatey packages](https://docs.chocolatey.org/en-us/create/create-packages/).
