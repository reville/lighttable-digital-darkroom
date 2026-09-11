# Linux distribution

How a Linux release is built, validated, and published. Linux has no public
release yet; macOS and Windows ship from the same tags. This page describes the
process, not a schedule.

Throughout, `X.Y.Z` stands for the version being released and `vX.Y.Z` for its
tag. Packaging preparation neither creates that tag nor publishes the release.
Select the final source commit after the other intended work has finished;
candidate artifacts built before that cutoff must be rebuilt.

## Distribution routes

| Route | Implementation | Publication and proof still required |
| --- | --- | --- |
| Portable archive | Native GTK/WebKitGTK shell, bundled Python and engines, per-user installer and signed updater | Configure update signing, test a native signed upgrade, and publish the tested x86_64 archive, checksum, and feed from the selected release commit |
| Arch / Omarchy | `lighttable-bin` generator, verified source/archive identity, `.SRCINFO`, pacman integration | Build/install on Arch, test actual Omarchy session, publish archive before uploading AUR recipe |
| Direct Flatpak | Full-app sandbox candidate and manual CI | Successful installed sandbox/portal workflow and an explicit update-distribution choice before public delivery |
| Flathub | AppStream metadata, canonical ID and source dependency audit | Finish the source-only dependency closure, validate runtime, establish release/use history and complete human submission |
| Snap | Strict development candidate using GNOME runtime and private shared memory | Native confinement/photo/upgrade/GPU tests, name registration and store review |
| DEB / RPM / AppImage | Deferred | Add when demand justifies another maintained distribution channel |

The public app ID is `app.lighttable.LightTable`, matching `lighttable.app`.
Portable installation migrates only untouched legacy launchers recorded as
LightTable-owned. Catalogs, preferences and photo folders retain their XDG paths.
Sandbox packages have separate data locations; migration between package formats
requires an explicit, tested catalog-and-folder-access workflow.

Portable updates use signed metadata on the dedicated `desktop-updates` GitHub
release, pointing to immutable versioned archives. Configure the public key in
release bundles and keep the matching private signing key in release secrets.
Unsigned or unconfigured builds leave in-app updates disabled. Arch/AUR, Flatpak,
and Snap retain their own update ownership. A direct Flatpak release also needs
a configured repository for continuing updates. See [release setup](../../release/README.md).

## Candidate validation before tagging

Run **Linux sandbox candidates** from the intended preparation branch with the
release version `X.Y.Z`. It builds the native archive once, then independently checks Arch,
Flatpak and Snap candidates. Its permissions are read-only and it uploads Actions
artifacts only. It contains no release, store submission or AUR publication step.
The workflow is manual. When fixing only the Flatpak recipe, its optional
`bundle_run_id` and `bundle_source_revision` inputs reuse a prior run's exact
verified archive and skip the bundle/Arch/Snap rebuilds. A final source cutoff
still requires a fresh full candidate run without those overrides.

The archive build uses Ubuntu 24.04 x86_64, checks the relocated Python/runtime,
both render engines, ICC conversion, HTTP/CLI startup, X11 and Wayland desktop
startup and software-Vulkan correctness. The sandbox workflow adds format-specific
installed-package checks. Failures must be fixed and rerun at the changed commit.

Before calling a channel stable, test a fresh install, folder selection, real RAW
import, film edit, export, close/reopen and upgrade with persistent edits. Include
an external drive, paths with spaces, desktop launch and URI handling. Omarchy
requires actual Hyprland/portal/theme testing; software-rendered VM screenshots
do not certify that desktop or hardware GPU performance. Hardware benchmarks for
AMD, Intel and NVIDIA remain a separate release acceptance item; see `docs/platforms/linux.md`.

## Publishing the chosen revision

The **Release** workflow accepts `all`, `linux`, `macos`, `windows` and legacy
`both` (macOS + Windows). Tag-triggered runs use the repository variable
`LIGHTTABLE_RELEASE_PLATFORMS`, defaulting to `all` when unset. For a Linux-only
release, set that variable to `linux` before pushing the tag; this avoids
starting macOS and Windows signing jobs. Manual platform input takes precedence.
Manual runs require an existing immutable tag; they never create a tag from a
moving branch. The workflow serializes publication and refuses to overwrite an
already-public release.

Linux-only publishing does not require Apple/Windows signing or npm publishing.
It publishes `LightTable-X.Y.Z-linux-x86_64.tar.gz`, its checksum, `SHA256SUMS`, and
the generated installer archive containing the AUR recipe. The recipe pins the
exact release bytes, version and full source revision. AUR submission must wait
until its download URL is publicly available and its checksum matches.

Releases without macOS do not replace the Mac updater's latest release feed.
The website must discover compatible Linux assets across public stable releases,
not only `/releases/latest`, and select the exact architecture. Store installation
links stay pending until a real listing exists and has been verified.

For an all-platform release, separately confirm the existing macOS and
Windows signing prerequisites in their workflows. The Linux-only path is ready
to operate independently; Linux changes do not provision those credentials.

## Store handoff and maintenance

The AUR maintainer owns the package repository and releases new `pkgver`/checksums
after each public app release. Recipe-only corrections increment `pkgrel`.
Keep the generated `.SRCINFO` consistent with `makepkg --printsrcinfo`.

Flathub verification uses the `lighttable.app` domain. All redistribution licenses
and the complete source-build dependency closure must be checked. Under the current
[Flathub policy](https://docs.flathub.org/docs/for-app-authors/requirements#generative-ai-policy),
a human maintainer must write and conduct submission/review interactions and
disclose applicable AI-generated material. This repository does not contain
AI-written Flathub submission messages. A direct Flatpak candidate is not a
Flathub-compliant source submission and must not be labeled as one.

Use package-manager/store updates for installed packages. Keep channels pending
on the website until installed-from-store verification succeeds. Retain the last
known-good binaries and tested catalog backups for release recovery; never replace
the bytes attached to a public version.
The portable updater retains the previous bundle but does not restore a catalog
or automatically reopen the old app after a new version may have migrated it.
