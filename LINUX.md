# LightTable on Linux

The Linux port uses a GTK 3/WebKitGTK desktop window, the shared Python catalog
and editing server, and the Rust film engines. The initial package target is
x86-64 Ubuntu 24.04 or newer and current Arch Linux, including Omarchy. Linux
support is experimental until the native import → edit → export → reopen
journey has passed on the hardware and desktop session being used.

## Install a portable bundle

Install the desktop libraries using your distribution's package manager. The
bundle includes Python and its pinned dependencies; system Python, Rust, and
development headers are unnecessary for running it.

On Ubuntu 24.04:

```sh
sudo apt-get install libgtk-3-0t64 libwebkit2gtk-4.1-0 libxdo3 \
  libopenblas0 libgomp1 libssl3t64 libgl1 libegl1 libvulkan1 liblcms2-2 \
  xdg-utils libglib2.0-bin gvfs zenity desktop-file-utils xdg-desktop-portal xdg-desktop-portal-gtk
```

On Arch Linux or Omarchy:

```sh
sudo pacman -S --needed gtk3 webkit2gtk-4.1 xdotool openblas gcc-libs openssl \
  libglvnd vulkan-icd-loader lcms2 xdg-utils glib2 gvfs zenity desktop-file-utils xdg-desktop-portal
```

Wayland file dialogs require a working desktop portal backend. Keep the backend
provided by your desktop: for example, Omarchy/Hyprland uses
`xdg-desktop-portal-hyprland`, commonly alongside `xdg-desktop-portal-gtk` for
file picking. GNOME and KDE use their respective portal backends. Restart the
desktop session if portal installation or configuration changes require it.

GPU rendering requires the Vulkan driver for your GPU. Ubuntu's Mesa drivers
are in `mesa-vulkan-drivers`; Arch uses `vulkan-radeon` for AMD or
`vulkan-intel` for Intel. NVIDIA needs the matching distribution driver and
userspace Vulkan libraries. A CPU fallback remains available when a GPU backend
cannot initialize. Software Vulkan in CI does not establish hardware performance.
The resident engine adapts its compute workgroups to the GPU. Linux one-shot
fallback previews and exports use the upstream CLI's CPU backend, because that
separate binary does not yet carry the portable GPU workgroups.

Check the downloaded archive against its accompanying checksum:

```sh
sha256sum -c LightTable-VERSION-linux-x86_64.tar.gz.sha256
mkdir -p "$HOME/Applications/LightTable-VERSION"
tar -xzf LightTable-VERSION-linux-x86_64.tar.gz -C "$HOME/Applications/LightTable-VERSION"
"$HOME/Applications/LightTable-VERSION/LightTable/install.sh"
```

Replace `VERSION` with the downloaded version. Keep the extracted bundle in
this permanent location: `install.sh` creates a desktop menu entry and symlinks
in `~/.local/bin`, without copying the bundle or asking for root privileges.
Launch **LightTable** from the desktop menu, or run
`LightTable/bin/lighttable-desktop` directly. `LightTable/bin/lighttable` is the
automation CLI. The desktop entry advertises support for `lighttable:` preset
links. Add `~/.local/bin` to your shell's `PATH` if necessary.

To upgrade, close LightTable, extract the new archive into another permanent
directory, and run its `install.sh`. This retargets the launchers owned by the
previous installation and preserves your data. If you move the bundle, run
`install.sh` again from its new location. The installer refuses to replace
unrelated or manually modified launchers.

Run the active bundle's `uninstall.sh` to remove desktop and CLI integration.
It leaves the extracted bundle, photographs, catalog, preferences, and caches
in place. An old bundle cannot uninstall the newer bundle's launchers. You may
delete the extracted application folder separately after closing the app.
Both scripts accept `--bin-dir /absolute/path` for a custom command directory;
use the same option and XDG settings during uninstall.

## Install with pacman on Arch or Omarchy

An existing Linux bundle can be packaged for system-wide installation without
rebuilding its pinned Python and native engines. From the source checkout:

```sh
python3 scripts/linux/make-arch-package.py \
  dist/LightTable-VERSION-linux-x86_64.tar.gz --output-dir .build/linux/arch-package
cd .build/linux/arch-package
makepkg -s
sudo pacman -U lighttable-bin-*.pkg.tar.*
```

Install Arch's `base-devel` first if `makepkg` is unavailable. The generator
checks the bundle's Linux architecture against its native binaries and writes
a local `PKGBUILD` with SHA-256 checksums. No AUR upload or remote package
repository is involved. The generated recipe, desktop file, and matching archive
must stay together. CI provides the recipe separately from the matching archive.

Pacman owns the application in `/opt/lighttable`, commands in `/usr/bin`, and
the system desktop entry and icon. Upgrade by generating and installing a package
from the newer bundle. Remove it with `sudo pacman -R lighttable-bin`; your photos,
catalog, settings, and caches remain in their user directories. When switching
from a portable installation, run that bundle's `uninstall.sh` first so its
per-user launcher does not take precedence over the system launcher.

The Omarchy package target is x86-64. `--experimental-aarch64` can create a
separately labelled ARM64 recipe for native ARM validation; it does not make
an ARM bundle usable on x86-64. See [the packaging notes](packaging/linux/arch/README.md)
for the package layout and validation boundary.

## Data and display behavior

### Omarchy and Hyprland

LightTable uses GTK's native Wayland backend when available and keeps the same
`org.lighttable.LightTable` identity on Wayland and X11. On Hyprland it leaves
borders and window actions to the compositor, without restoring a saved
maximized state over the tiling layout. No global Omarchy configuration is edited.
The Linux minimum window size is 800 × 480 logical pixels. Below 1100 pixels
wide, the library opens as a drawer and the editor keeps room for the photo.

The app reads Omarchy 4's active `colors.toml` from
`~/.local/state/omarchy/current/theme/`, with support for Omarchy 3's
`~/.config/omarchy/current/theme/` and absolute XDG relocations. Toolbars,
sidebars, menus and dialogs follow the palette; visible windows check for
changes every five seconds. The viewer background preference, photo pixels
and color scopes stay neutral. Outside Omarchy, controls follow the system's
light/dark preference. See the [upstream theme format](https://github.com/omacom/omarchy/blob/v4.0.2/docs/theming.md).

Settings → Performance reports the last active Rust backend and adapter.
`lighttable --port PORT status --json` includes `renderers` diagnostics, and
the export benchmark includes adapter information, fallback reasons and host
timings for submission/readback. Those timings are not GPU hardware timestamps.
A per-image GPU limit uses CPU for that image; a worker transport failure retries
once on CPU and keeps that client on CPU until the app is reopened. Software
Vulkan adapters use the threaded CPU backend by default. Explicit
`SPEKTRAFILM_BACKEND=wgpu` remains available for software Vulkan validation.

From a source checkout, measure the actual adapter with:

```sh
python3 rust-engine/bench_resident_cache.py \
  --binary /path/to/lighttable-engine --data /path/to/engine/data \
  --width 2200 --require-hardware --output linux-gpu-report.json
```

The benchmark records adapter identity, timings, cache behavior and pixel
parity. `--require-hardware` rejects software/unknown adapters. Without a
separate baseline binary it compares the current engine with caching disabled
and enabled; it does not measure a before/after patch speedup.

### Storage

Linux uses the XDG directories, with `lighttable` beneath each root:

| Content | Default directory | Override |
| --- | --- | --- |
| Catalog and presets | `~/.local/share/lighttable` | `XDG_DATA_HOME` |
| Preferences | `~/.config/lighttable` | `XDG_CONFIG_HOME` |
| Render and Python caches | `~/.cache/lighttable` | `XDG_CACHE_HOME` |
| Logs and installer record | `~/.local/state/lighttable` | `XDG_STATE_HOME` |

Only absolute XDG overrides are used. Photos remain in the folders you choose.
Existing LightTable-specific path overrides remain available for isolated
review libraries and automation.

The webview uses the desktop's display scaling and rendering support. Both
Wayland and X11 require native runtime testing, especially fractional scaling,
multiple monitors, keyboard shortcuts, removable drives, dialogs, and trash.
ICC-tagged export uses portable color profiles, but wide-gamut preview accuracy
must be checked on the actual compositor, browser engine, and calibrated
display. Do not infer display accuracy from an embedded export profile.
Linux TIFF import uses LittleCMS to convert embedded ICC profiles while retaining
16-bit and floating-point input precision, without reducing it to eight bits
first. Orientation and transparency are handled during conversion. Processed
TIFF caches are uncompressed so the bundled decoder can read them without
optional compression plugins.

Apple Vision/CoreML features and macOS HEIF export have no bundled Linux
replacement in this first port. Use the capabilities exposed by the Linux
runtime; this package does not claim feature parity with the Apple services.

## Build on Ubuntu 24.04 x86-64

Build on the oldest supported distribution to avoid linking the desktop host
against newer glibc or WebKitGTK symbols. Compiling on Arch produces a local
Arch build and does not establish compatibility with Ubuntu 24.04.

```sh
sudo apt-get install build-essential git pkg-config cmake gfortran libssl-dev \
  libopenblas-dev libgtk-3-dev libwebkit2gtk-4.1-dev libxdo-dev libdbus-1-dev \
  libvulkan1 mesa-vulkan-drivers libgl1 libegl1 libgomp1 liblcms2-2 desktop-file-utils python3-venv
rustup toolchain install 1.88.0 --profile minimal --target x86_64-unknown-linux-gnu
python3 -m venv .build/linux/build-tools
.build/linux/build-tools/bin/pip install uv==0.11.28
export PATH="$PWD/.build/linux/build-tools/bin:$PATH"
./scripts/linux/build-release.sh --version 0.1.0
```

Install Rust/rustup first if unavailable. On Arch, the equivalent
additional build packages include `base-devel`, `git`, `pkgconf`, `cmake`,
`gcc-fortran`, and `rustup`; GTK/WebKit headers ship with the runtime packages.

`packaging/linux/runtime.json` pins CPython 3.13.12, the uv release that selects
and verifies its standalone distribution, Rust 1.88.0, and both upstream film
source revisions. Python dependencies reuse `requirements-runtime.lock`; only
binary wheels are accepted so missing Linux wheels fail the build visibly.
ICC downloads have fixed revisions and SHA-256 checksums. Build source revision,
dirty state, platform, pins, and requirements checksum are recorded in
`build-manifest.json`. Dependencies still require network access at build time.

The builder copies all shared top-level Python modules, film data and web
resources; builds the desktop, resident, and export binaries with Cargo's
locked dependencies; moves the completed bundle to a path containing spaces;
and runs the packaged runtime smoke there. Only then does it produce
`dist/LightTable-VERSION-linux-x86_64.tar.gz` and its `.sha256` file.
The archive needs the distribution libraries listed above and is not an
AppImage, Flatpak, DEB, or RPM.

Native ARM64 Linux builders may pass `--experimental-aarch64` for a validation
bundle. It uses the same pinned versions with ARM64 Python and Rust targets,
records its experimental architecture in the manifest, and produces an
`aarch64` archive. An ARM64 VM check does not establish x86-64 package or GPU
hardware support. `--cargo-target-dir`, `--shell-target-dir`, and
`--engine-target-dir` allow reuse of existing native compilation caches.

## Verification

```sh
python3 -m unittest discover -s tests -p test_linux_packaging.py -v
cargo +1.88.0 test --locked --manifest-path windows-shell/Cargo.toml
cargo +1.88.0 test --locked --manifest-path rust-engine/Cargo.toml
LightTable/Python/bin/python3 -B LightTable/runtime-smoke.py LightTable
```

The Linux workflow builds on Ubuntu 24.04, runs Rust and packaging tests, and
tests XDG storage, shared-memory fallback, RAW decode reuse, portable color,
CLI portability, and first-run setup. Node.js 24 runs the shared interface unit
tests using its built-in runner; no npm packages are required. Python checks run
with pinned Python dependencies and film data. Pull requests run those source
checks; main-branch and manual runs also build a distributable archive and
upload a relocated bundle only after dependency imports, ICC conversion, CPU
film rendering through both engines, an isolated HTTP health check, and the
packaged CLI pass. The smoke creates temporary data and always stops its server.
Before uploading that archive, CI also extracts it to a path containing spaces,
opens the actual GTK/WebKit desktop under Xvfb and a private D-Bus session, and
requires its UI bridge to report a rendered test photo. This native startup
check has a two-minute bound and stops its desktop and server processes.
The same check also runs with GTK forced to Wayland on a private headless
Weston compositor, with no X server available to the app. These software-rendered
startup checks cover both display protocols; they do not establish Omarchy,
physical GPU, fractional-scale, or color-managed display behavior. Packaging
tests exercise desktop URI delivery and safe removal, including paths containing
spaces and shell metacharacters, and verify the pacman package's staging layout.

Before declaring a Linux release ready, complete the desktop photo journey on
Ubuntu and Arch/Omarchy, both Wayland and X11 where supported. Verify a real RAW
import, responsive film edits, 16-bit TIFF export, restart persistence, folders
on a removable drive, and recoverable trash. Measure GPU response on AMD, Intel,
and NVIDIA separately. CI's CPU render and backend initialization do not verify
those hardware, native UI, or photographic display requirements.

References: [Wry Linux requirements](https://docs.rs/wry/0.56.0/wry/#linux),
[uv Python distributions](https://docs.astral.sh/uv/concepts/python-versions/#managed-python-distributions),
[XDG directories](https://specifications.freedesktop.org/basedir/latest/),
[desktop entries](https://specifications.freedesktop.org/desktop-entry/latest/).
