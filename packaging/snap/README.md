# Snap development candidate

This prepares a complete strict-confinement Snap from the verified Ubuntu 24.04
x86_64 bundle. The generator only prepares the package; Store registration and publication are separate steps.
The generated package stays `grade: devel` until the native checklist below
passes. Version **0.5.0** is displayed as **LightTable 0.5** in release titles.

```sh
python3 scripts/linux/make-snap-package.py dist/LightTable-0.5.0-linux-x86_64.tar.gz \
  --version 0.5.0 --source-revision FULL_SOURCE_SHA --output-dir .build/snap
cd .build/snap
snapcraft pack
```

Use a clean bundle from the exact expected source revision. The generator checks
the manifest, architecture, native ELF headers and checksum before extracting;
it preserves existing output directories. The manual **Linux sandbox candidates**
workflow can build and exercise a candidate without creating a GitHub release.

The GNOME extension supplies GTK3, WebKitGTK 4.1 and the `gpu-2404` provider. The
package adds its numerical/runtime libraries. It does not ship a host graphics
driver or disable WebKit sandboxing. A private `shared-memory` plug lets the
Python server and Rust engines exchange data without exposing host shared memory.
`network-bind` is needed by the local HTTP server; `network` supports the
embedded client and optional verified model downloads. File dialogs use portals.
`network-status` lets the desktop portal report network and proxy settings to
WebKit; omitting it prevents the embedded client from loading when portals run.
`home` supports existing photo libraries and CLI file arguments; `removable-media`
is optional and does not normally auto-connect.
The session D-Bus slot permits GTK application registration only under
`app.lighttable.LightTable`; the desktop cannot claim arbitrary service names.

After installing a local development candidate with `snap install --dangerous`,
the desktop command is `snap run lighttable` and the CLI is `snap run lighttable.cli`.
Both use `$SNAP_USER_COMMON/{data,config,cache,state}` for stable locations across
Snap revision upgrades. Existing portable/AUR catalogs are not copied implicitly.
Photo originals and exported images stay in their selected folders.

## Acceptance before stable publication

1. Build successfully on native Ubuntu 24.04 amd64 and retain Snapcraft's lint output.
2. Run with strict confinement, without `--devmode` or `--classic`.
3. Test the first-run folder picker, import, editing, film rendering, export and
   reopening. Exercise both portal-selected removable folders and optional
   `snap connect lighttable:removable-media` access.
4. Test the CLI, local server shutdown, shared-memory input and all packaged codecs.
5. Upgrade an installed revision and reopen the same catalog and edits. Check
   a restored backup independently before recommending downgrade across a schema change.
6. Test Vulkan on real AMD, Intel and NVIDIA hardware, plus CPU fallback.
7. Verify store-name ownership, review licenses/metadata, change `grade` to `stable`
   only after the above checks, then publish through the maintainer's Snap account.

The **Snap release candidate** workflow checks the installed CLI, isolated CPU
renders, codec imports and the local server inside strict confinement. It also
runs the actual GTK/WebKit desktop under Xvfb: Canon CR2 and Fuji RAF imports,
film rendering, full-size 16-bit ICC exports, normal close/reopen, and a local
Snap revision update that preserves the catalog and saved edits. Native screens,
export hashes and reports are retained as workflow artifacts. Run [34429708691](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34429708691)
passed these checks for application source
`be537f2f3e2e431ae6b42af716c2a8b365f57bab` and packaging revision
`6f7af43aa65af57b878cf9fc9e82ae4d19931c97` (artifact build), with acceptance harness
`24bd66b`. The exact Snap SHA-256 is
`a399aabdc3f21a119fb12cb39984893145ea896c159fcb5e266445b7ab3ee797`.
It also passed initial native folder selection through the real desktop portal,
photo display and close/reopen using the retained document-portal path, denial
of direct `/media` access before connection, and access after explicitly
connecting `removable-media`. Proxy handling remained enabled throughout.

The same run passed fresh web onboarding through the actual native WebKit UI,
folder-source creation, initial editing, an independent catalog backup, a
post-backup rating change, and backup restoration in a new desktop process.
The restored photo retained rating 4 and exposure +0.50, onboarding stayed
complete, and the original file hash was unchanged. Native screenshots and all
five acceptance reports were reviewed. Physical AMD, Intel and NVIDIA GPU
coverage remains unverified.
A follow-up [stress acceptance run 34430155776](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34430155776)
passed all five reports for the identical Snap, using harness revision `099d694`.
It completed eight normal reopens per RAW sample after the local revision update
(16 total), preserving exposure, rating, film settings and clean shutdown. All
22 direct Python/native fault logs were empty. One earlier run, 34428856279,
recorded a native libc segmentation fault while reopening the Canon sample after
refresh. It did not recur in the complete repeat or stress run; its cause is not
established and remains a beta stability observation, not a claimed fix.

The package therefore stays `grade: devel`; an initial Store release must use
`latest/beta`, never `candidate` or `stable`. Stable promotion requires all the
acceptance checks above, including real GPU evidence.
Snap manages its own user data and removal/snapshots. Before uninstalling or
switching package formats, preserve a catalog backup; do not describe Snap removal
as equivalent to the portable installer's launcher-only uninstall.

References: [GNOME extension](https://ubuntu.com/docs/snapcraft/latest/reference/extensions/gnome-extension/),
[private shared memory](https://snapcraft.io/docs/reference/interfaces/shared-memory-interface/),
[desktop portals](https://snapcraft.io/docs/explanation/snap-development/xdg-desktop-portals/).

## Native dependency notices

The immutable 0.5.0 application bundle did not retain all native Rust dependency
notices. Snap packaging supplements it with verbatim upstream license, copyright
and notice files under `Resources/LightTable/licenses/native-rust`. This does not
change the application binaries or its source manifest.

`python3 scripts/linux/snap-native-licenses.py` verifies 441 crates and 825 retained
files from the Linux normal/build dependency graphs for the desktop shell,
resident engine and standalone film engine. The inventory retains the exact
release Cargo lockfiles and each crate archive checksum; upstream VCS sources
are recorded when a published crate omitted its notice text. Packaging rejects
corruption, missing files, unresolved coverage and a different release identity.
Python distribution notices and Ubuntu package copyright files remain in their
existing locations. See `native-licenses/provenance.json` for scope and limitations.

Snapcraft run 34427122032 completed the classic, GPU, library and metadata
linters. Its six unused-library warnings concern libpython3.13, libpython3,
libcolordprivate, libdconf, libssl and libxdo. These libraries remain available
for runtime loading; no lint errors were reported.

## CPython stack flag

Store review of revision 1 identified an unnecessary executable-stack flag on
`Python/lib/libpython3.13.so.1.0`. This matches the upstream
[CPython standalone-build issue](https://github.com/astral-sh/python-build-standalone/issues/1072).
`snap-python-noexecstack.py` clears only `PF_X` in that library's existing
`PT_GNU_STACK` header. The helper checks the exact input and output SHA-256,
source revision and ELF layout, and records the one-byte change in
`snap-runtime-adjustments.json` and candidate metadata. The application source
manifest and public release archive stay unchanged. CI verifies the installed
library and receipt before exercising the packaged runtime and native desktop.

## Store publication

The public `lighttable` name is approved for publisher `LightTable`
(`lighttable-app`), Snap ID `aR4yGHHrroWcBo0quMe2wgaE0BzJFLis`.
`store-metadata.json` contains the saved beta listing text. The Store also has
the branded icon, Photo and Video category, and five reviewed native Linux
screenshots, verified by their uploaded hashes. Registration,
upload acceptance, Store interface review, channel release and public listing
visibility are separate checks. The session D-Bus slot may require Canonical
review; an upload must not be reported as a published release before acceptance
and channel verification.

On 2026-09-10, the verified artifact above was uploaded as Store **revision 2**
with `latest/beta` requested. Revision 1 was withdrawn from the review queue
because it was superseded by the corrected stack-flag package. The replacement
[review page](https://dashboard.snapcraft.io/snaps/lighttable/revisions/2/)
has the D-Bus explanation and both passing acceptance runs. At submission,
release and public listing visibility remain pending Store review; this is not
a stable release or a claim that the public channel is live.
