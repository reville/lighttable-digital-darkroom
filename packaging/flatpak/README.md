# Flatpak preparation

There are two separate deliverables here. Neither has been submitted to Flathub.

## Upstream candidate

`scripts/flatpak/make-candidate.py` creates a complete application manifest from a
verified LightTable Linux archive. It requires the expected version, clean source
commit, x86_64 ELF architecture, and an independently supplied SHA-256. It retains
the exact Python, Python wheels, film engines, profiles and application resources
that passed the portable-bundle tests. OpenBLAS and libxdo are compiled from pinned
source; GTK 3 and WebKitGTK 4.1 come from the supported GNOME 50 runtime.

This is an **upstream/direct-distribution candidate**, not a Flathub source build.
Its binary Python dependencies do not satisfy Flathub's source-only requirement.
The manual `Linux sandbox candidates` workflow builds the base Linux archive once,
then builds this Flatpak without network access during compilation, installs it,
checks private shared memory, denied host-file access, rendering, HTTP, CLI, and an
actual GTK/WebKit photo window. It uploads candidate artifacts and evidence only;
it does not tag, release, upload to a remote Flatpak repository, or submit to a store.
The workflow must pass before this candidate is described as tested or runnable.
It runs manually so ordinary source PRs do not rebuild every distribution format.
For a Flatpak-recipe-only fix, provide `bundle_run_id` and the existing archive's
full `bundle_source_revision` to retest only Flatpak. The archive's manifest and
checksum must still match; this does not test later application-source changes.
Completed dependency stages are cached against the SDK commit and dependency
recipe, including when a later application check fails.

```sh
python3 scripts/flatpak/make-candidate.py \
  --bundle dist/LightTable-0.5.0-linux-x86_64.tar.gz \
  --version 0.5.0 --source-revision FULL_CLEAN_COMMIT --sha256 ARCHIVE_SHA256 \
  --output-dir .build/flatpak/candidate
flatpak-builder --user --download-only .build/flatpak/build \
  .build/flatpak/candidate/app.lighttable.LightTable.json
flatpak-builder --user --force-clean --disable-download --default-branch=candidate \
  --repo=.build/flatpak/repo --install .build/flatpak/build \
  .build/flatpak/candidate/app.lighttable.LightTable.json
flatpak run app.lighttable.LightTable//candidate
```

The generated provenance records the full bundle manifest and archive checksum.
Flatpak evidence records the resolved SDK/runtime commits, since a supported runtime
branch receives security updates. The application ID, desktop entry and icon agree:
`app.lighttable.LightTable`. AppStream uses actual Linux screenshots from the public
[Linux gallery](https://lighttable.app/screenshots.html?platform=linux), whose
[capture manifest](https://lighttable.app/screenshots/linux/manifest.json) records
the Linux app revision, dimensions, CC0 photo sources and software graphics.
Version 0.5.0 is marked as a development release while preparation is underway.

## Sandbox behavior

- Shell, Python server and resident engines are subprocesses inside one sandbox.
  Their private `/dev/shm` suffices for RAW and native frames. No host IPC, host
  `/dev/shm`, host subprocess escape, broad D-Bus, or home/host filesystem grant is
  requested. `--device=dri` permits the runtime's graphics driver access.
- Wayland is preferred and X11 is a fallback. Host shared IPC is omitted, so X11
  may use copies instead of the MIT-SHM optimization. The native-window smoke
  validates functionality, not hardware speed or color-managed display output.
- Network permission supports the loopback server and the application's online
  services. The server continues to bind loopback and enforce its normal local
  request validation. It is not changed to listen on public interfaces.
- File/folder and save dialogs already use rfd's XDG portal backend on Linux.
  Choose the **photo folder** through the native picker to grant the library tree,
  including XMP sidecars and future files. Selecting individual files grants those
  files, not arbitrary siblings. Export destinations on external drives also need
  selection through the folder/save picker. No automatic whole-drive grant exists.
- Portal document paths must remain in the catalog: do not replace them with
  ungranted host paths. FileChooser grants persist across sessions; users can revoke
  them through their desktop's permission settings. Revoked or unplugged folders
  are unavailable until selected/mounted again. A watch runs only while the app runs.
- Catalog, preferences, caches and models use Flatpak's private XDG directories.
  The launcher anchors state and instance-registration files in writable private
  locations. Existing non-Flatpak catalogs are not silently imported.
- The portable directory layout is preserved under `/app/LightTable`, so runtime
  discovery needs no separate system-Python fallback or changes to Windows/macOS.
  The CLI runs with `flatpak run --command=lighttable-cli app.lighttable.LightTable`.
- Host executable paths chosen as external editors cannot execute in this sandbox.
  Use the desktop's default-application portal action; arbitrary host-editor
  selection is not a supported Flatpak capability. Do not add `flatpak-spawn --host`
  to bypass this. Photo import, export, folder grants, reopening after restart,
  external-editor portal behavior, and recoverable trash still need interactive
  portal validation before a public candidate release.

## Experimental source-only build graph

`python-source-audit.json` inventories every exact pin in `requirements-runtime.lock`.
It contains SHA-256-pinned PyPI source distributions or immutable upstream Git pins;
it contains no wheels. `audit-python-sources.py` checks lock drift offline, and its
explicit `--refresh` mode queries PyPI. This inventory is not a transitive build graph.

rawpy 0.27.1 and OpenCV 5.0.0.93 provide PyPI source distributions.
Dependencies without one retain immutable upstream Git source mappings in the audit.

`source-candidate.json` is now a concrete, separate source-build attempt generated
offline by `scripts/flatpak/source-manifest.py`. It includes CPython 3.13.12 without
ensurepip's bundled wheel, source-built Python packaging tools, all 29 runtime
requirements, LLVM 22.1.8 for llvmlite 0.49, OpenBLAS/LAPACK, FFTW, LibRaw, Lensfun,
OpenEXR/Imath, Exiv2, and all three native engines. Cargo archives and checksums
come from the exact application and upstream CLI lockfiles; Cargo runs with
`--offline --locked`. PEP 517 builds use `--no-index --no-build-isolation`, and
wheel files are created locally from the pinned sources. OpenCV and dateutil use
separate build environments to honor their older NumPy/setuptools/SCM bounds.

`source-dependencies.json` records the source pins and the exact maintained GIMP
recipe revision used for Exiv2/OpenEXR modules. It also records build dependencies.
`source-cli-Cargo.lock` is the pinned upstream export CLI's lockfile. The generator
checks the application's runtime lock and upstream CLI identity before emitting
a manifest. The generated source recipe is **not a verified Flathub package**.

`source-native-codecs.json` supplies 38 pinned native codec/build-tool recipes,
including the APNG patch, libjpeg-turbo 3, JPEG XL/Highway, JPEG XR, Blosc/Blosc2,
SZ3, SPERR, Pcodec, and all four AVIF backends (AOM, dav1d, rav1e, SVT-AV1).
Pcodec, rav1e and cargo-c use checked-in upstream Cargo lockfiles and fully
vendored, checksum-pinned crate sources. lzham's compatibility `zlib.h` stays in
its own include directory so it cannot replace the SDK header.

`source-native-licenses.json` appends an install-only module after compilation.
It retains exact upstream notices for all 38 native modules and the three codec
Cargo lockfiles, with source paths, hashes and explicit coverage gaps in the
installed `native/provenance.json`. Lockfile membership includes development and
non-Linux dependencies; the actual linked dependency set still needs review.
The conservative inventory flags five Rust packages without standalone notices
and 31 Vortex packages with a dirty published source identity. Offline, locked
dependency-tree checks with the exact Linux recipe features exclude all of these
from the selected targets except `simd_helpers 0.1.0`, a rav1e procedural macro.
Its generated-code notice coverage remains unresolved. Retaining available
notices does not establish release readiness.

`source-imagecodecs.py` retains the Linux wheel's complete extension set rather
than imagecodecs' reduced default source set. `source-codec-check.py` imports all
60 required compiled modules and fails if any are missing. This is an availability
gate; it does not establish codec numerical parity or completed native acceptance.
`source-status.json` records that native validation is still pending. The SDK
preflight checks compilers (including gfortran and NASM) before expensive builds;
LLVM compilation is limited to two jobs and its linker to one job.

```sh
python3 scripts/flatpak/source-manifest.py --source-revision FULL_APPLICATION_COMMIT
python3 scripts/flatpak/source-check.py
python3 scripts/flatpak/source-preflight.py packaging/flatpak/source-status.json
flatpak-builder --user --install-deps-from=flathub --download-only .build/source-flatpak packaging/flatpak/source-candidate.json
flatpak-builder --user --disable-download --jobs=2 --repo=.build/source-flatpak-repo .build/source-flatpak packaging/flatpak/source-candidate.json
```

Compilation runs without network access. The Rust SDK extension is a build tool
only; LLVM runtime libraries are built from source. Use `--stop-at=openblas` for
a bounded foundation probe, or `--stop-at=python-imagecodecs` to reach the codec
availability gate before the application. Flatpak owns updates for this install.

Native CI has verified the required compiler paths, including gfortran, inside
GNOME 50 and compiled the CPython and initial packaging-tool stages. The complete
source build is still under validation.
All source-built runtime imports, codec/ICC/TIFF precision, film-engine parity,
license installation, and the installed native window still require Linux proof.

## Official requirements checked

- [Flathub requirements](https://docs.flathub.org/docs/for-app-authors/requirements):
  source builds, offline compilation, minimal portal-based permissions, licenses.
- [Flatpak sandbox permissions](https://docs.flatpak.org/en/latest/sandbox-permissions.html).
- [FileChooser portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.FileChooser.html):
  grants survive sessions and selected folders use the directory option.
- [AppStream guidance](https://docs.flathub.org/docs/for-app-authors/metainfo-guidelines).
- [GNOME 50 platform contents](https://github.com/GNOME/gnome-build-meta/blob/gnome-50/elements/sdk-platform.bst):
  GTK 3 and WebKitGTK 4.1 are part of this runtime.

Flathub requires human disclosure of included AI-generated material and forbids AI
agents from opening submission pull requests or generating submission messages,
descriptions, review comments or replies. No such materials or interactions are
part of this work. Source-build and portal validation must be completed before a
human considers a Flathub submission.
