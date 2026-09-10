# Snap development candidate

This prepares a complete strict-confinement Snap from the verified Ubuntu 24.04
x86_64 bundle. It does not register a Snap Store name or upload a release.
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

## Required before a store release

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
export hashes and reports are retained as workflow artifacts. Run 34424616546
passed these checks for application source
`be537f2f3e2e431ae6b42af716c2a8b365f57bab` and packaging revision
`c8ef478e12404c4a4a873d1eb603ff579526e2b4`.
It also passed initial native folder selection through the real desktop portal,
photo display and close/reopen using the retained document-portal path, denial
of direct `/media` access before connection, and access after explicitly
connecting `removable-media`. Proxy handling remained enabled throughout.

The web onboarding flow, backup restoration and real GPU coverage remain
unverified. The candidate therefore remains `grade: devel`.
Snap manages its own user data and removal/snapshots. Before uninstalling or
switching package formats, preserve a catalog backup; do not describe Snap removal
as equivalent to the portable installer's launcher-only uninstall.

References: [GNOME extension](https://ubuntu.com/docs/snapcraft/latest/reference/extensions/gnome-extension/),
[private shared memory](https://snapcraft.io/docs/reference/interfaces/shared-memory-interface/),
[desktop portals](https://snapcraft.io/docs/explanation/snap-development/xdg-desktop-portals/).
