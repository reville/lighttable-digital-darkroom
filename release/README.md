# Public releases

The canonical public repository is `reville/lighttable-digital-darkroom`.
The existing private repository remains the historical backup. Copy approved
code changes onto the new history; never merge or push the old ancestry into it.

Release builds bundle the native shell, Python runtime, render engines, web UI,
profiles, and updater. End users do not need Python, Rust, or a source checkout.
The build targets are macOS Apple silicon, Windows x64, and experimental Linux
x86_64. Each platform needs its own native release validation.

## Current readiness

Installer generation, the bundled CLI, npm installer code, and release workflows
are implemented. There is no published desktop version yet. A public Mac release
requires a Developer ID Application certificate and notarization credentials.
Windows code signing and npm authentication must also be configured before their
signed/npm distribution paths can be used. A failed preflight publishes nothing.
Windows and Linux updater integration is present in source. A signed native
upgrade, configured signing keys, and published feeds are still required before
automatic updates can be delivered.

## Validate candidates before publication

Windows and Linux package validation can run independently while Apple signing
is being prepared. Dispatch their build workflows on the candidate branch:

```sh
gh workflow run windows-build.yml --ref CANDIDATE_BRANCH -f require_signing=false
gh workflow run linux-build.yml --ref CANDIDATE_BRANCH
```

These workflows upload CI artifacts; they do not create a public release or
publish update feeds. Record the resolved commit from each run, and compare it
with the downloaded package manifest. An unsigned Windows CI package cannot
establish Authenticode or public update trust. Do not push a release tag simply
to test packaging: the separate Release workflow publishes successful builds.

Portable Windows and Linux apps read their source revision from the bundled
manifest. Extracting an app inside a Git checkout must not run that checkout's
Git commands or substitute its revision during startup or health checks.

Windows retains a completed package before native acceptance runs. A native-only
recheck can reuse those exact bytes with a newer test script:

```sh
gh workflow run windows-build.yml --ref CANDIDATE_BRANCH -f build_run_id=WINDOWS_BUILD_RUN_ID
```

The recheck verifies the selected build and embedded source revision, and records
the package and tester commits separately. It does not rebuild or publish assets.

Linux full-package validation also runs `scripts/linux/updater-smoke.py` with
the bundled Python under Xvfb and a private D-Bus session. It copies the real
bundle, changes only temporary version/key metadata, signs a local test archive,
checks signature/checksum rejection, retargets owned launchers, and requires the
new GTK app and bundled server to render a photo. A deliberately failed GTK
startup must restore the old launchers. The original package and user data are
outside this test's writable paths. This verifies the installed updater path;
public HTTPS delivery, the update dialog, migrations between source revisions,
and hardware/display behavior still need their own acceptance pass.

Before publishing the first version, retain evidence from the exact candidate
for clean installation, representative RAW/JPEG/16-bit TIFF input, editing and
export, saved edits after restart, recoverable trash, and package ownership.
Run Windows client/scaling tests and Ubuntu/Arch desktop/GPU tests on the systems
listed as supported. Package-index submissions are separate from direct downloads.

## Build and release a version

1. Complete unit/installer tests on the exact source to release.
2. Configure the platform credentials below.
3. Create and push a new stable tag such as `v0.1.0`. The tag workflow uses the
   selected release platforms. Manual dispatch accepts an existing tag version
   and can select one platform. See [Linux distribution](../LINUX-DISTRIBUTION.md)
   for platform selection. Published release files are immutable; use another
   version for changes.
4. The workflow builds and tests the actual bundles, signs/notarizes Mac artifacts,
   generates checksums and package definitions from the final bytes, stages a draft
   release, and makes it public only after all selected builds succeed.
5. Publish the generated Homebrew/Scoop files and submit the WinGet/Chocolatey
   packages after native installation validation. The index repositories can sync
   an existing public release without rebuilding or changing its files.
6. Publish the prepared npm tarball only after its matching desktop assets are
   public. npm's package name and Homebrew's main-catalog acceptance are separate
   from creating a GitHub repository.

The workflow prepares the pinned SCUNet conversion environment as part of the Mac
build, rather than depending on ignored files from a developer machine. Ordinary
photo smoke tests use the small source-attributed JPEGs in `tests/fixtures/photos`.
For the optional RAW compatibility journey, run `python3 scripts/fetch-demo-raws.py`.

## macOS credentials

Set these GitHub repository secrets through GitHub settings or `gh secret set`
using a protected file/stdin, never a literal secret in shell history:

- `APPLE_CERTIFICATE_BASE64`: base64 of a Developer ID Application `.p12` export
- `APPLE_CERTIFICATE_PASSWORD`: password for that export
- `APPLE_KEYCHAIN_PASSWORD`: random password for the temporary CI keychain
- `APPLE_SIGNING_IDENTITY`: the full `Developer ID Application: ...` identity
- `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`: notarization account
- `SPARKLE_PRIVATE_KEY`: existing update-signing key matching `SUPublicEDKey`

Apple Development and Apple Distribution certificates do not substitute for a
Developer ID Application certificate. Keep private keys out of the repository.
The build uses an ephemeral keychain and deletes it after the job.

For a local build after preparing `scripts/models/requirements-convert.txt`:

```sh
LIGHTTABLE_VERSION=0.1.0 LIGHTTABLE_BUILD_NUMBER=1 \
  LIGHTTABLE_SIGN_IDENTITY='Developer ID Application: Your Name (TEAMID)' \
  scripts/build-release.sh
scripts/notarize-app.sh dist/LightTable.app
scripts/package-release.sh dist/LightTable.app 0.1.0
```

The notarization script reads credentials from the environment. Public releases
also sign, notarize, and staple the final DMG before calculating its checksum.
The ZIP contains the stapled app and is signed through Sparkle's update feed.
The initial pipeline publishes complete update archives rather than deltas.
Windows-only releases do not replace the latest Mac feed.

The canonical updater URL is
`https://github.com/reville/lighttable-digital-darkroom/releases/latest/download/appcast.xml`.
It becomes live after the first Mac release. The private signing key must match
the public key embedded in `app/Info.plist`.

## Windows release signing

Ordinary CI builds remain unsigned so runtime and installation checks can run
without release credentials. Public release builds set `require_signing: true`
and fail before building if signing is not configured. Set repository secrets
`WINDOWS_CERTIFICATE_BASE64` and `WINDOWS_CERTIFICATE_PASSWORD` for the authorized
Authenticode certificate export. The Windows SDK SignTool signs and timestamps
the app and engine executables before packaging, then signs the final installer.
The npm installer independently rejects an invalid or unsigned Windows installer.

## Windows and Linux update feeds

The dedicated GitHub release tag `desktop-updates` holds mutable feed files:

- `appcast-windows-x64.xml` contains WinSparkle enclosures signed with
  `SPARKLE_PRIVATE_KEY`, matching the Ed25519 public key in
  `windows-shell/src/windows_update.rs`. Authenticode signing is also required.
- `linux-x86_64.json` contains signed release metadata for the portable archive.
  Set the repository variable `LIGHTTABLE_LINUX_UPDATE_PUBLIC_KEY` to the base64
  Ed25519 public key and the secret `LIGHTTABLE_LINUX_UPDATE_PRIVATE_KEY` to its
  matching PEM private key or base64 seed. Release builds embed the public key
  in `update-config.json`; the private key is used only to sign release metadata.

Feed URLs use `/releases/download/desktop-updates/`. Each feed points to assets
under an immutable `vMAJOR.MINOR.PATCH` release. Publishing a feed must follow
successful publication and verification of those exact assets. A Windows-only
or Linux-only release must preserve the other platform's feed and the macOS feed.
Do not use `/releases/latest/` for these two platform channels.

Linux signatures cover the version, architecture, source revision, archive URL,
size, SHA-256, and validity dates. The updater rejects expired or older metadata,
wrong-platform bundles, altered archives, unsafe extraction paths, and a changed
trusted key. Builds without a configured public key keep automatic updates
disabled. Key rotation needs a separately designed transition; replacing the key
in the next archive is rejected.
The Linux feed expires after 365 days by default. Re-sign and refresh it before
expiry even if the current app version has not changed, using the same immutable
archive. `scripts/generate-linux-update.py` verifies the archive's embedded public
key before signing; it does not create credentials or publish its output.

Before enabling either channel, test a signed upgrade on its native desktop with
pending edits, an active export, a network failure, a bad signature, and a busy
server. Confirm preparation saves edits and makes a verified catalog backup,
both app and server exit before installation, the same catalog reopens, and
package-managed installations decline in-app updates. Linux also needs a failed
launch check and launcher recovery verification. Retaining an older bundle does
not make a migrated catalog compatible with it: the updater never automatically
restores a catalog or relaunches an old app after the new app may have migrated it.

The local Linux upgrade gate uses a temporary signing key and two versions of
the same source bundle. Production-key signing, public feed delivery, and an
upgrade between actual release revisions remain separate acceptance steps.

## CLI and package definitions

A Mac release contains `Contents/MacOS/lighttable-cli`. Its launcher resolves
symlinks and uses the bundled runtime from any working directory. For a manual
installation, link it with:

```sh
scripts/install-cli.sh --app /Applications/LightTable.app
```

The default destination is `~/.local/bin`; add that directory to your PATH.
Homebrew creates its own link. Windows installs a dedicated `bin` shim and
registers it in the user's PATH, avoiding a collision with the GUI executable.

See [installer generation](../packaging/INSTALLERS.md) and
[npm preparation](../packaging/npm/README.md). The npm source intentionally has
unpublished metadata until a preparation step hashes real versioned artifacts.
Installing the npm package provides its launcher; `lighttable install` installs
the desktop app. Automatic npm postinstall downloads are disabled.

## Personal development builds

`scripts/update-personal-app.sh --check` is a read-only preflight. The personal
updater reuses a verified installed runtime when compatible, stages changed code,
rebuilds changed helpers, signs/verifies the bundle, and keeps a recovery copy.
Changed native sources compile directly with the Xcode Release Swift settings
and the verified base's pinned Sparkle framework. The compiler recipe is part of
the native cache key; the signed package must pass the real-photo native journey
before installation.
When packaging inputs change, establish a new base with `scripts/build-release.sh`.
Personal builds do not constitute signed public releases.

## npm publication credentials

The npm package remains unpublished until its owner authenticates with npm.
After the first publication, configure npm trusted publishing for this GitHub
repository and `npm-publish.yml`, or store an appropriately scoped `NPM_TOKEN`
repository secret. Set the repository variable `NPM_PUBLISH_ENABLED=true` to
publish automatically after future desktop releases. Manual workflow dispatch
accepts a version already published on GitHub. The workflow downloads the exact
prepared package, verifies its release checksum, checks its metadata, and then
publishes with provenance. It never publishes placeholder release metadata.
