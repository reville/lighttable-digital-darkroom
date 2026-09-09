# Contributing to LightTable

We welcome bug reports, photo and camera compatibility testing, documentation,
and code contributions. We are looking for Windows, macOS, and Linux maintainers,
including a maintainer focused on Omarchy. [Open an issue](https://github.com/reville/lighttable-digital-darkroom/issues/new)
with the platform or area you would like to work on.

For a bug report, include your operating system, LightTable version or commit,
steps to reproduce, and what you expected to happen. For image problems, include
the camera model and file format; a sample you can share and a screenshot or
export showing the problem help us reproduce it.

For user-facing changes, follow the **update-text** workflow: update the
documentation, in-app Help, and website feature content, then refresh affected
localizations. The [content maintenance guide](docs/help/README.md#update-text)
describes the order and checks.

## Source setup (macOS)

The native development app targets Apple silicon and macOS 13 or later. You need
Git, `uv`, Rust/Cargo 1.88.0 or later, and Xcode or Xcode Command Line Tools with
`swiftc` and a macOS SDK. Node.js 20 or later is needed for JavaScript tests;
there is no frontend dependency installation or bundling step.

A fresh clone needs the Python environment, upstream Python film runtime, and
spectral profile data. The upstream revisions below match the release scripts
and Python CI workflow. Run this once in a fresh checkout:

```sh
git clone https://github.com/reville/lighttable-digital-darkroom.git
cd lighttable-digital-darkroom

uv venv --python 3.13.12 .venv
uv pip install --python .venv/bin/python -r requirements-runtime.lock

mkdir -p vendor .build/dependencies engine
git clone --filter=blob:none --no-checkout \
  https://github.com/andreavolpato/agx-emulsion.git vendor/spektrafilm
git -C vendor/spektrafilm fetch --depth=1 origin \
  3bb2c2d2801ff68b92019cf1dbcbb133d60832bc
git -C vendor/spektrafilm checkout --detach \
  3bb2c2d2801ff68b92019cf1dbcbb133d60832bc

git clone --filter=blob:none --no-checkout \
  https://github.com/turbasvin/spektrafilm-rs.git .build/dependencies/spektrafilm-rust
git -C .build/dependencies/spektrafilm-rust fetch --depth=1 origin \
  9dd59b0380194b93686aaa230a8bb9680aa270a4
git -C .build/dependencies/spektrafilm-rust checkout --detach \
  9dd59b0380194b93686aaa230a8bb9680aa270a4
cp -R .build/dependencies/spektrafilm-rust/data engine/data

cargo build --release --locked \
  --manifest-path .build/dependencies/spektrafilm-rust/Cargo.toml \
  -p spektrafilm-cli --bin spektrafilm
cp .build/dependencies/spektrafilm-rust/target/release/spektrafilm \
  engine/spektrafilm-rs
```

The last two commands prepare the one-shot render engine used by the export
fallback and the engine parity test. The resident render engine is built by the
native app command below. `.venv/`, `vendor/`, `engine/`, and `.build/` are local
dependencies and are not committed. The LightTable-specific Rust sources under
`rust-engine/vendor/` are already tracked.

Build and open the development app:

```sh
bash build-app.sh
open build/LightTable.app
```

This app uses the checkout's `.venv` and source files, so keep the checkout in
place. It is a local development bundle. Self-contained packaging, signing,
notarization, and model preparation are covered in the [release guide](release/README.md).
The optional Enhance denoiser reports unavailable until its converted Core ML
model has been prepared; normal browsing and editing remain usable.

To run the server and browser interface directly, set the Python source path in
the same terminal:

```sh
export PYTHONPATH="$PWD:$PWD/vendor/spektrafilm/src${PYTHONPATH:+:$PYTHONPATH}"
bash run.sh "/path/to/photo-folder" 8321
```

`run.sh` uses the macOS `open` command. It is not a Linux launcher.

For local appearance experiments on macOS, Command-D opens a hidden, movable
appearance tester window. It stays open while the editor remains
usable; on macOS it floats above the main window. Repeating the shortcut brings
the existing tester forward. Its color picker and hex field update selections and
active controls immediately; switching borders off leaves active button text
and icons colored, without their fill or border. Photo selection outlines,
semantic color swatches, and keyboard focus outlines remain available. Test
settings stay in the current webview/browser origin's local storage; Reset
restores the normal palette and borders. Escape or Close closes the tester;
closing the editor also closes it. The testing controls remain English-only and
absent from product menus and Help.
Browser sessions use a separate popup; Windows and Linux desktop shells retain
the existing inline tester under Ctrl-D.

## Tests

From the prepared checkout, run the Python unit and contract suite and the
resident Rust tests:

```sh
PYTHONPATH=".:vendor/spektrafilm/src" .venv/bin/python \
  -m unittest discover -s tests -p 'test_*.py' -t tests --verbose
cargo test --locked --manifest-path rust-engine/Cargo.toml
```

The Python suite runs the JavaScript regression tests when Node.js is available;
those tests are skipped without it. Installer work also has a standalone npm
suite:

```sh
npm test --prefix packaging/npm
```

For rendering changes, check the Python/Rust film comparison after building the
one-shot engine above:

```sh
PYTHONPATH=".:vendor/spektrafilm/src" MPLCONFIGDIR=/tmp/lighttable-mpl \
  .venv/bin/python tests/engine_parity.py
```

The real macOS app journey builds the development app, opens the checked-in
JPEG fixtures, exercises editing and export, and saves a screenshot and timing
results:

```sh
scripts/run-product-journey.sh pr
```

For occasional layout and visual bug hunts, use the [on-demand UI audit](scripts/ui-audit/README.md).
It provides headless snapshots, DOM invariants, and seeded exploration; it is not
part of commit checks, CI triggers, or `mnb`.

See [product journey testing](JOURNEY-TESTING.md) for package and RAW-image
journeys. The optional RAW fixtures require a separate download; routine unit
tests and the JPEG journey do not require the full RAW collection. Passing unit
tests does not establish that a native window or a rendered photo looks right.
For changes to image math, keep the relevant CPU, WebGL, Metal, and Rust paths
consistent and check both preview and export.

## Windows and Linux

Windows has a Rust desktop shell and packaging workflow. Follow
[WINDOWS.md](WINDOWS.md) for the Windows x64 prerequisites, build command,
runtime checks, and native GUI checks. The macOS setup commands above are not a
Windows packaging recipe.

Linux has an experimental GTK/WebKitGTK desktop port and portable-bundle build
tooling. Follow [LINUX.md](LINUX.md) for Ubuntu and Arch/Omarchy dependencies,
build instructions, XDG storage locations, and the platform validation checklist.
The initial target is x86-64; public packages and physical GPU validation are
still needed. An ARM64 virtual-machine build does not establish x86-64 support
or hardware rendering performance.

Linux contributions should check both the shared photo workflow and native
integration: import, edit, export, reopen, file dialogs, display scaling,
removable drives, and recoverable Trash. Report the distribution, desktop,
Wayland or X11 session, GPU, and driver with results. Apple Vision/CoreML
features and HEIF export remain unavailable on Linux. There is no supported
Linux desktop release yet.

## Send a contribution

Keep pull requests focused, explain the behavior being changed, and report the
tests and platforms you checked. Screenshots or exported samples are useful for
interface and image-quality changes.

The [README](README.md) describes the architecture.
[CLI.md](CLI.md) documents the local control interface, and
[FILM-PROFILES.md](FILM-PROFILES.md) and [calibration](calibration/README.md)
explain profile provenance and image comparisons. The
[development roadmap](DEVELOP-ROADMAP.md) and
[workflow roadmap](WORKFLOW-ROADMAP.md) record implementation plans and status; check Releases for shipped versions.
