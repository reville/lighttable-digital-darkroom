# LightTable for npm

This package installs the LightTable desktop digital darkroom and exposes its
bundled CLI. It requires Node.js 20+ and supports macOS on Apple silicon and
Windows x64. It has no npm dependencies.

The registry package is
[`lighttable-digital-darkroom`](https://www.npmjs.com/package/lighttable-digital-darkroom).
The unscoped name `lighttable` is unavailable on npm. The installed command is
still `lighttable`.

```sh
npm install --global lighttable-digital-darkroom
lighttable install
lighttable --help
```

For a one-off installation without a global npm command:

```sh
npx lighttable-digital-darkroom install
```

The source package in this repository deliberately contains no desktop release
URL or checksum claims until maintainers prepare it from actual assets.

Installing the npm package itself does not download the desktop app. The
explicit `lighttable install` command downloads the exact release recorded in
the npm package, checks its SHA-256, verifies the operating system signature,
and installs it. It never requires a source checkout, Python installation,
compiler, administrator password, or disabling Gatekeeper.

macOS installs to `~/Applications/LightTable.app`. An existing app in
`/Applications` is used automatically. Use `--install-dir /absolute/directory`
to select another directory. Windows reuses the per-user installation registered
by the NSIS installer, including a custom directory. With no existing install,
it uses `%LOCALAPPDATA%\Programs\LightTable`. The `--install-dir` option is
supported only on macOS.

An existing app is reused unless `lighttable install --update` is explicit.
Updates keep the previous app beside it in a `.backup-…` location; a failed
replacement restores that backup. Backups can be removed after verifying the
new app. Quit LightTable before updating. The installer only replaces app
files and does not remove photo libraries, catalogs, or preferences.

After installing a newer npm package, use `lighttable install --update` to
install its desktop version. Other arguments go directly to the installed
app's bundled CLI. On Windows this uses bundled Python directly so shell
metacharacters in photo names are passed as arguments without cmd.exe
interpretation.

`npm uninstall --global lighttable` removes the npm launcher, leaving the app
and user data. Remove the Mac app or use Windows Installed Apps to remove the
desktop app separately.

## Maintainer release preparation

Build, sign, and verify the actual versioned assets. The supported names are
`LightTable-VERSION-macos-arm64.zip` and
`LightTable-VERSION-windows-x64-setup.exe`. A Mac app must have the production
identifier `com.reville.lighttable`, a valid Developer ID signature and
Gatekeeper approval. The Windows installer must pass Authenticode validation.

From the repository root:

```sh
node packaging/npm/scripts/prepare-release.mjs 1.0.0 /path/to/real/release-assets
cd packaging/npm
npm test
npm pack --dry-run
```

Preparation computes hashes from existing asset bytes and records only the
platforms actually present. It updates `release.json` and the npm package
version. It never invents checksums. Publish those exact assets under the
matching `vVERSION` tag in `reville/lighttable-digital-darkroom` before npm
publication. Downloads cannot be redirected to a custom repository or host.
Preparation also copies the project's GPLv3 license into the package. The
`prepublishOnly` check blocks unprepared metadata or missing license text.

Automatic downloading is available as an explicit maintainer choice: add
`"postinstall": "node scripts/postinstall.mjs"` to `scripts` in package.json
and update this README. The hook honors `LIGHTTABLE_SKIP_DOWNLOAD=1` and reuses
an existing app. It is currently disabled. npm users with lifecycle scripts
disabled can always run `lighttable install` themselves.

Tests use temporary files and injected transport/process execution. They
never download a production release or install an app on the test machine.
