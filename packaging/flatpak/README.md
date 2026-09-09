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

## Source-only build work remaining

`python-source-audit.json` inventories every exact pin in `requirements-runtime.lock`.
It contains SHA-256-pinned PyPI source distributions or immutable upstream Git pins;
it contains no wheels. `audit-python-sources.py` checks lock drift offline, and its
explicit `--refresh` mode queries PyPI. This inventory is not a transitive build graph.

The current unsolved work is an **offline build and verification of the scientific
runtime**, not missing upstream source. `rawpy 0.27.1` now provides a PyPI source
distribution. Dependencies without a source distribution retain immutable upstream
Git source mappings in the audit. A completed manifest must build and pin:

- CPython 3.13 and the packaging backends, Cython, Meson-Python, scikit-build-core,
  pybind11, Pythran and their build dependencies;
- the matching LLVM toolchain for llvmlite/Numba, and Fortran/LAPACK for SciPy;
- FFTW, LibRaw, Lensfun and its database, Exiv2 and its Python binding;
- OpenImageIO, OpenEXR/Imath and the required codecs, OpenCV's headless bindings,
  and imagecodecs' native codec libraries;
- source-built NumPy/SciPy/scikit-image and the remaining Python runtime packages,
  with all source licenses, before replaying the existing precision/parity tests.

Native code builds must disable implicit CMake/Meson/Python dependency downloads
and provide those sources beforehand. Optional codecs cannot simply be disabled
without checking LightTable's advertised image formats. The source-only manifest
and installed source-built application are **NOT DONE**; this candidate does not
claim otherwise.

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
