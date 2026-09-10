# Prepare once, promote one platform at a time

`release.yml` builds selected platforms. `release-prepare.yml` consumes the
completed build artifacts and emits `LightTable-PLATFORM-promotion`, containing
the immutable binaries, installer definitions, checksums, proof files, and a
candidate manifest. The release source is the version tag's commit; the workflow
revision may be newer and is recorded separately.

Run **Promote verified release candidate** (`release-promote.yml`) manually with
the exact version, source revision, original build run, preparation run, and
platform. Its default is a dry run. It does not rebuild or sign anything. A failed
platform in the original build does not block another platform whose required
package and native checks passed.

The promotion artifact must contain a flat, complete set of files named in
`LightTable-VERSION-PLATFORM-release.json`. Preparation records `candidate` and
`pending`; promotion changes these to `ready` and `passed` only after independently
fetching and verifying the original binaries and platform evidence. The version
tag and embedded source/version/clean-worktree identity must agree. Legacy bundles
without the required identity fields require a separately reviewed migration;
this workflow does not invent the missing evidence.

Keep **dry_run** enabled for the first pass. If the target GitHub release is
missing, the plan includes creating its draft. Disabling **dry_run** creates that
draft only after all evidence passes, then adds missing assets. Existing drafts
and public releases resume without replacing assets. Authentication or network
failures never count as a missing release. **make_public** publishes the verified
draft, including one created by this run.
**advance_feed** also requires **make_public**, and changes only the selected
platform's app-owned updater after anonymous public downloads match every
published hash. Linux and Windows use their respective `desktop-updates` assets;
macOS uses the latest release's appcast, matching the existing installed client.

Existing assets and platform manifests are immutable. Identical retries skip
those files; a name with different bytes fails. Separate
`LightTable-VERSION-PLATFORM-installers.tar.gz` and
`LightTable-VERSION-PLATFORM-SHA256SUMS` names allow another platform to join later.
The only replaceable files are the selected platform's signed updater pointer.
Older feed versions and different feed bytes for the same version are rejected.
A feed repair needs a separate review.

## Required proof

- **Linux:** original portable archive, its production-key signed feed, and the
  original build's `Linux-X11-native-acceptance/report.json` with the matching
  clean source, rendered/exported photo, persistence, and normal reopen/close.
- **Windows:** original archive and installer, matching timestamped Authenticode
  receipt, signed appcast, and a completed `windows-client-vm.yml` run containing
  both `Windows-10-x64-client-acceptance` and
  `Windows-11-x64-client-acceptance`. Each requires actual native x64 client host
  details, exact installer hash, unelevated/offline installation with WebView2
  initially absent, a complete installed PE signature audit including the
  uninstaller, and `native/report.json` proving edit/export/reopen. Windows Server
  or Windows 11 ARM emulation results do not satisfy this gate.
- **macOS:** the original `macos-release-proof.json` binds the final artifact
  hashes to clean Apple silicon source, strict signature verification, and the
  packaged real-photo acceptance receipt. Stable releases additionally require
  the producer's notarization/Gatekeeper proof. Ad-hoc beta builds are manual
  update channels.

Windows native and client VM acceptance accept an optional `source_revision`
when the build workflow ran from a different revision. Signed candidates must
match the immutable version tag before installer execution. The workflow source
and package source remain separate in the evidence.

Client/native evidence and promotion results are retained for 90 days. Keep the
preparation artifact and original build artifacts available through publication;
expired evidence blocks promotion rather than triggering a new build. VM disks,
account configuration, and Windows installation media are excluded from evidence.

The promotion result includes the platform manifest, source, tag, release URL and
publication time, and independent public-download verification state. Website
synchronization must consume a successful public verification result, then deploy
and verify the live affected pages before reporting website publication complete.
The promotion workflow itself makes no website-publication claim.
