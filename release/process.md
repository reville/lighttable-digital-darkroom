# Build once, promote each platform independently

A release has one immutable source cutoff and independently verified platform
artifacts. A blocked Windows or store channel does not prevent an accepted Linux
or macOS channel from progressing. No recurring release scheduler is required.

The manual build, preparation and promotion workflows have separate jobs:

1. **Build:** select platforms and the existing immutable release tag. Run the
   credential/source/environment preflight before expensive native builds. Keep
   the successful original build artifacts, even when another platform fails.
2. **Prepare:** dispatch `release-prepare.yml` with the original build run,
   expected source revision, platform and embedded version. It downloads the
   original bytes, generates installer definitions and a platform manifest, and
   retains `LightTable-PLATFORM-promotion`. It never rebuilds an app or publishes
   a release. Optional `native_run_id` identifies separate acceptance evidence;
   preparation does not declare that evidence passed.
3. **Verify and plan:** promotion resolves the original build and native evidence,
   verifies the exact candidate, and prints missing versus already matching
   release assets. The default is read-only. A manifest's own `ready` label is
   not independent signature or native acceptance proof.
4. **Promote:** explicitly apply the verified plan, independently download and
   verify the public assets, then advance only that platform's signed update
   feed. Synchronize the reviewed aggregate manifest to the website and package
   channels. Verify live versions, links and checksums before reporting success.

The original build's `head_sha` identifies the workflow revision; a manual build
may check out a different pinned release tag. The manifest's `source_revision`
must match the immutable release tag and bundled build identity. Record
`build_workflow_revision` separately. Preparation accepts a completed build with
an unrelated failed platform only when the selected package job and its required
native/proof step passed. Fork and pull-request runs are rejected.

For an ad-hoc Mac beta, explicitly select the beta channel and manual update
owner, and create its `macos-vVERSION-beta.N` tag at the verified source before
preparation. Stable Mac publication always requires Developer ID, notarization,
native acceptance and Sparkle signing; no missing credential selects a weaker
channel automatically.

## One versioned data contract

[`release-manifest.schema.json`](../scripts/release/release-manifest.schema.json)
defines schema version 1. `release_process.py validate_manifest()` additionally
checks version/channel agreement, exact GitHub repository/tag/asset ownership,
safe unique filenames and platform policy. This is public metadata: no keys,
tokens, passwords or secret values belong in it.

```json
{
  "schema_version": 1,
  "repository": "reville/lighttable-digital-darkroom",
  "tag": "v1.2.3",
  "version": "1.2.3",
  "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "platforms": {
    "linux-x86_64": {
      "version": "1.2.3",
      "channel": "stable",
      "minimum_os": "Ubuntu 24.04+; current Arch Linux/Omarchy",
      "update_owner": "app",
      "state": "candidate",
      "signing": "ed25519",
      "build_run_id": "123456",
      "gates": ["Exact-artifact native acceptance is pending"],
      "validation": {"status": "pending", "receipts": []},
      "artifacts": []
    }
  }
}
```

Platform keys are `linux-x86_64`, `windows-x64` and `macos-arm64`. Platform states
are `candidate`, `blocked`, `ready` and `published`. Artifact records contain
`name`, canonical public `url`, positive `bytes` and lowercase `sha256`. Optional
artifact `update_owner` can distinguish a package manager archive from a portable
app. Empty artifacts are valid only for planning a candidate or blocked channel.

Each published platform keeps its own immutable manifest:
`LightTable-VERSION-PLATFORM-release.json`. A manifest cannot hash itself. The
website's aggregate uses the same schema, includes all three platform entries,
and records independently verified publication state. An aggregate may contain
different platform versions, so advancing Linux to 0.7 does not remove a still
supported Mac 0.6 beta. Entries outside the top-level release version must provide
their own `source_revision` and `tag`; same-base beta/stable entries may share the
top-level source but still need their own tag. A historical same-tree build can record
`build_source_revision` plus `source_tree`; new promotion requires the exact
source revision and does not use that historical exception.

Do not modify an immutable platform manifest merely to update a receipt link or
to add another platform. Update the reviewed aggregate instead. The website
publishes download links only for accepted, published entries; a blocked entry
does not need a guessed future URL.

## Read-only preflight

Create a candidate manifest without `--artifact` before building. Pass the
following **presence metadata**, collected from source/tag checks, configured
runner/environment access and secret names. Credential values are never read or
printed by this CLI.

```json
{
  "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source_clean": true,
  "tag_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "platforms": {
    "linux-x86_64": {
      "release_environment_allowed": true,
      "build_runner_ready": true,
      "native_acceptance_ready": true,
      "credentials": {"linux_update_signing": true}
    }
  }
}
```

```sh
python3 scripts/release/release_process.py preflight \
  --manifest candidate.json --capabilities capabilities.json \
  --platform linux-x86_64 --phase build
```

Credential capability keys are `linux_update_signing` for Linux;
`windows_authenticode` and `sparkle_signing` for Windows; and
`macos_developer_id`, `macos_notarization`, `sparkle_signing` for stable Mac.
Only an explicitly ad-hoc, manual beta omits stable Mac credentials.

The matrix separates `build_ready` from `promotion_ready`. `--phase publish`
also requires passed acceptance receipts, no outstanding gates, correct signing
policy and native acceptance availability. A blocked selection exits 2; malformed
input or failed verification exits 1. Unselected platforms remain in the matrix
but do not block the selected ones. Presence alone never proves signing validity.

## Additive publication and retries

The reusable CLI verifies local artifacts and, for Linux tarballs and Windows
portable ZIPs, the embedded version/source/clean/platform metadata. It reads
metadata directly from archives without extracting them. Windows ZIPs also need
the declared Authenticode build marker; independent signature verification remains
the promotion workflow's responsibility. Legacy bundles missing clean/platform
metadata are not silently exempted.

```sh
python3 scripts/release/release_process.py verify \
  --manifest prepared/LightTable-1.2.3-linux-x86_64-release.json \
  --artifacts-dir prepared

python3 scripts/release/release_process.py publish \
  --manifest prepared/LightTable-1.2.3-linux-x86_64-release.json \
  --artifacts-dir prepared --platform linux-x86_64
```

`publish` without `--apply` only reads GitHub and prints the plan. The release/tag
must already exist. Use the promotion workflow to create a missing draft, verify
native/signature receipts, and apply only after those gates pass.

For each remote asset: matching SHA-256 **and** size means skip; absent means
add; any mismatch aborts. Old GitHub assets without digest metadata are downloaded
and hashed. Every upload omits `--clobber`, including on drafts. Concurrent uploads
fail safely and can be retried. Already uploaded bytes and the platform manifest
are checked again after an apply. Feed advancement is a separate explicit step
after independent public download verification.

Installer archives and checksum files are platform-specific:

- `LightTable-VERSION-PLATFORM-installers.tar.gz`
- `LightTable-VERSION-PLATFORM-SHA256SUMS`

The installer tar archive has normalized ordering, timestamps and ownership, so
identical definitions produce identical bytes. Existing generic `SHA256SUMS`
and combined `LightTable-VERSION-installers.tar.gz` files remain immutable.
Canonical Arch names, `lighttable-bin-VERSION-PKGREL-x86_64.pkg.tar.zst`, remain
supported and retain package-manager update ownership.

Retry promotion from the **same completed preparation run**. Preparation reuses
an existing public same-version Linux feed after verifying its GitHub digest,
size and downloaded bytes. Generating a missing Linux feed uses the configured production key and current
validity dates; a second preparation can therefore produce different signed
metadata despite an identical app archive. Retaining and reusing the preparation
artifact prevents accidental feed/manifest collisions. If assets have already
been published, conflicting metadata must not be overwritten to force a retry.

## Evidence and build timing

Keep original build, preparation, signature verification, native acceptance,
public download and live website verification distinct. Windows Server and
Windows 11 ARM emulation do not establish native Windows 10/11 x64 offline
acceptance. The client matrix requires Windows 10 with WebView2 genuinely absent
before offline install and Windows 11 with its preinstalled runtime preserved;
both require actual offline, unelevated native acceptance. See
[the client test contract](../docs/windows-client-acceptance.md). Store enrollment/review and direct download readiness are separate.

Windows build timing records separate compilation, runtime/native tests,
installer construction/signing and compression. Optimize the measured expensive
stage; never reuse a candidate with a changed source or dependency identity merely
to make a build appear faster.

Focused offline regression coverage:

```sh
python3 -m unittest discover -s tests -p test_release_process.py
```

These tests exercise tampering, source/version/tag ownership, duplicate metadata,
partial release resume, immutable manifests, blocked platform isolation,
credential-presence policy, independent successful jobs, byte-preserving candidate
preparation and deterministic archive generation. They do not claim a real public
promotion or native updater run.
