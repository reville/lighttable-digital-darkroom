# Windows architecture

## Installation and command line

Public releases are published at
[reville/lighttable-digital-darkroom](https://github.com/reville/lighttable-digital-darkroom/releases).
The Windows x64 artifacts are `LightTable-VERSION-windows-x64-setup.exe` and
`LightTable-VERSION-windows-x64.zip`. Availability depends on a successful
release build; installer manifests do not create downloadable binaries.

The installer runs without administrator rights and defaults to
`%LOCALAPPDATA%\Programs\LightTable`. It adds a dedicated `bin` directory to
the current user's PATH. Open a new terminal after installation, then run
`lighttable --help`. The CLI uses bundled Python and does not require a source
checkout, Python installation, or Node.js. The dedicated directory prevents
Windows from resolving the desktop `LightTable.exe` before the CLI.

Before copying or replacing LightTable, setup checks for the Microsoft Edge
WebView2 Runtime in both the per-machine and per-user registry locations. If
it is missing, setup downloads Microsoft's Evergreen bootstrapper, requires a
valid Microsoft Authenticode signature, and installs the Runtime without
elevation. Downloads and installation have time limits; failure stops setup
before changing an existing LightTable installation. Internet access is only
needed for this prerequisite when the Runtime is missing. For offline setup,
install Microsoft's [Evergreen Standalone Installer](https://developer.microsoft.com/microsoft-edge/webview2)
first. LightTable uninstall leaves this shared Microsoft runtime installed.

For unattended installation:

```powershell
Start-Process -Wait .\LightTable-VERSION-windows-x64-setup.exe -ArgumentList '/S'
```

An optional `/D=C:\path with spaces\LightTable` must be the last installer
argument and its value must not be quoted separately. The per-user uninstall
registry key is `LightTable`, publisher is `Nicholas Reville`, and the
`QuietUninstallString` supports `/S` for WinGet and other package managers.
Uninstall removes shipped files, shortcuts, and the CLI's PATH entry. Catalogs,
preferences, caches, photos, and unrelated files in the install directory remain.
Nothing LightTable writes at runtime lands in the install directory: the
catalog, preferences, desktop settings, server log, render caches, compiled
Python bytecode, compiled numba kernels, and the WebView2 profile all live
under `%LOCALAPPDATA%\LightTable`.

For the portable ZIP, extract its `LightTable` folder, open `LightTable.exe`, or
run `LightTable\lighttable.cmd --help`. A package manager can shim
`LightTable\lighttable.cmd` explicitly. Merely adding the ZIP root to PATH
would choose the GUI executable instead of the command.
Portable users must install the WebView2 Runtime separately if it is missing.
Both package formats include Microsoft-signed Visual C++ x64 runtime DLLs
beside the executables. The build requires runtime 14.44.35211.0 or newer,
records its version, and verifies that Python loads the packaged copies.
Users do not need administrator rights to update the machine's C++ runtime.

## Updates

Signed direct installations use WinSparkle for signed update checks, release
notes, downloads, and installation. **Settings → General → Check for Updates…**
opens the native updater. Automatic checks can be disabled in General; when
enabled, they run at most daily after the editor opens. Downloads do not close
the app. Installing an update saves pending edits, verifies a catalog backup,
and requires imports, exports, and other active work to finish first. The update
helper waits for both the window and server to exit before running the installer,
then reopens LightTable with the same catalog. A failed preparation leaves the
app open.

The installer writes `install-channel.txt`. Direct installations use `direct`;
Scoop, WinGet, and Chocolatey packages record their manager and disable in-app
installation. Update those copies through the same package manager. The portable
ZIP uses `portable` and requires downloading and extracting a newer ZIP.
Unsigned development builds cannot install updates automatically.

The Windows feed is `appcast-windows-x64.xml` on the dedicated `desktop-updates`
GitHub release. Its signed enclosures point to immutable versioned installers.
WinSparkle verifies the Ed25519 signature before handing off the download.
The release build signs and verifies the installer's Authenticode signature
before publication. The helper waits for shutdown and runs that installer. See
[release setup](release/README.md) for signing and feed publication.
This source integration still requires a signed upgrade on an actual Windows
desktop before release.

## Build and validation

On Windows x64 with Rust 1.88.0, Git, uv 0.11.28, and NSIS installed:

```powershell
.\scripts\windows\build-release.ps1 -Version 0.1.0
```

The build downloads a hash-verified embedded Python runtime and pinned render
sources, runs Rust tests and a packaged runtime smoke test, creates the ZIP and
installer, then tests silent installation, reinstallation, CLI discovery from
another directory, exit-code forwarding, and uninstall with user-data sentinels.
The installer smoke uses a disposable directory and refuses to run when that
Windows account already has a registered LightTable installation. A `build-manifest.json`
records the exact source commit and version. Use `-PortableOnly` explicitly to
build a ZIP without NSIS or installer testing.

`scripts/windows/installer-fixture-smoke.ps1` runs the same install, repair,
package-ownership, CLI-registration, and data-preserving uninstall checks with
a tiny fixture payload. It compiles the real NSIS source and uses the real PATH
and uninstall helpers; its temporary WebView2 prerequisite and runtime files
are stubs. CI requires this fast installer check before a full package build.
It does not establish application or prerequisite-runtime behavior.

`-RuntimeSmokeOnly` stages the same embedded Python runtime and application
files, then checks imports, high-precision processed-image conversion, and a
real server startup with HTTP health, editor, and options requests. It does
not compile Rust or create an installer. Pull requests run this check in
addition to the Windows Rust compile checks. Root Python modules are staged
together, so adding an indirect or optional feature import cannot silently
leave its local dependency out of the Windows package.

`.github/workflows/windows-build.yml` checks pull requests and produces packages
on `main`, manual dispatch, or reusable-workflow calls. The reusable workflow
accepts a required `version` string and uploads both files in the
`LightTable-windows-x64` artifact for the unified release workflow. `source_ref`
selects the exact release commit. `require_signing` defaults to `false` for CI;
the public release workflow sets it to `true` and passes signing secrets to the
reusable workflow. Runtime and installer smoke tests do not establish Windows
GUI or RAW-rendering proof.

Full package workflows additionally run `scripts/windows/desktop-smoke.py`
against the extracted portable ZIP. It opens the real native shell in an
isolated catalog, requires a rendered precision TIFF, changes exposure and
rating through the interface, closes and reopens the app, and verifies retained
edits and an RGB16 TIFF export with an embedded ICC profile. A private Windows
Job Object owns and cleans up only the test's processes. The runner checks
`--runtime-paths` before opening a window; the Windows-only absolute
`LIGHTTABLE_SUPPORT_DIR` override isolates native settings and WebView2 data.
The JSON evidence records the source revision and renderer. Use `--photo`
with a real RAW file for an additional hardware acceptance run. This gate
provides native runtime evidence, not screenshot or monitor-color proof.

For Authenticode signing, configure repository secrets
`WINDOWS_CERTIFICATE_BASE64` (a base64-encoded PFX containing a valid code-signing
certificate and private key) and `WINDOWS_CERTIFICATE_PASSWORD`. The installed
Windows SDK must provide `signtool.exe`. A reusable-workflow caller must pass
these secrets explicitly or use `secrets: inherit`.

`build-release.ps1 -RequireSigning` checks the signing configuration before any
downloads or compilation and refuses missing or partial credentials. Without
credentials, ordinary CI builds remain unsigned. With credentials, the build
signs the desktop executable, both render-engine executables, and the final
NSIS installer; verification precedes smoke testing and final archiving. The
temporary PFX is deleted in a `finally` block and no certificate is installed
in the Windows certificate store. `build-manifest.json` records whether the
package was signed.

The signing helper uses SHA-256 file and RFC 3161 timestamp digests with the
DigiCert timestamp service, then requires `signtool verify /pa /all /tw` to
pass. The flags follow [Microsoft's SignTool documentation](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool).
Configuring this workflow does not itself obtain a certificate or prove a
successful signed release.

Windows support is an additional host around the shared render core, not a
replacement for the macOS implementation.

## Boundaries

- `app/main.swift` remains the macOS host. It still launches the same server,
  uses the macOS image and colour-management tools, and reaches WGPU through
  Metal.
- `windows-shell/` owns Windows windowing, native file dialogs, source-folder
  persistence, and the system webview. It launches the same local server and
  reaches WGPU through DirectX 12.
- `platform_image.py` selects native macOS image services on macOS and the
  bundled portable decoder, metadata, and ICC path on Windows.
- The web UI talks to either host through the small `postMessage` bridge. Film,
  grade, cache identity, and export math remain shared.

This separation is intentional: platform work should not add a conditional to
the measured render loop unless the operating system genuinely requires one.

Full-resolution portable processed-image conversion decodes through OpenImageIO
and applies ICC transforms through LittleCMS via the pinned `imagecodecs`
runtime. 16-bit and floating-point intermediates preserve source detail instead of
passing it through Pillow's 8-bit RGB conversion. These dependencies are loaded
only for portable conversion; macOS retains its native image/color path, and
the bounded preview path is unchanged.
The portable processed-input cache has its own version so an existing Windows
installation rebuilds its old 8-bit intermediates. Mac and RAW input cache
identities remain unchanged.

## Runtime layout

The embedded Python runtime ships a `python313._pth` file, and CPython treats
that file as a request for isolated mode: `PYTHONPATH`, `PYTHONUNBUFFERED`,
`PYTHONPYCACHEPREFIX`, and every other `PYTHON*` variable are ignored. The
desktop shell and `lighttable.cmd` therefore pass what matters as interpreter
options: `-u` keeps `server.log` current, and `-X pycache_prefix` caches
bytecode under `%LOCALAPPDATA%\LightTable\python-bytecode` so the second
launch skips recompiling the application and its scientific dependencies.
`NUMBA_CACHE_DIR` (honoured, because it is not a `PYTHON*` variable) keeps
compiled kernels under `%LOCALAPPDATA%\LightTable\compiled-runtime`, and the
WebView2 profile lives under `%LOCALAPPDATA%\LightTable\WebView2` rather than
beside the executable. The install directory stays read-only in practice.

The shell opens its window immediately with a dark loading page and starts the
render server on a background thread, so the first frame no longer waits for
Python imports and catalog opening. Choosing another folder stops the running
server before starting its replacement, because one catalog holds one process
lease; a choice made while a start is in flight waits its turn, and a folder
whose server cannot start returns to the previous one with a toast. The window
remembers its size and maximized state, fits the current display, and uses the
dark title bar and WebView2 colour scheme.

Every helper the server starts (the resident engine, the one-shot exporter, the
export worker, git) runs with `CREATE_NO_WINDOW`; under a console-less parent a
console-subsystem child would otherwise open a visible command window. The
OpenMP, numba, and BLAS pools use half the logical processors, between four and
eight, instead of the fixed four the macOS host uses.

## Performance path

On Windows, checking whether originals have changed reads each local file in
full. Rescanning, opening photos, and export checks can take longer with large
originals. These checks do not download files stored only in the cloud.

The resident renderer remains a separate long-lived process on both platforms,
so GPU device creation, pipeline compilation, profile loading, and decoded
inputs stay warm. Adding Windows therefore does not force the Mac through a
portable renderer or a cross-platform desktop framework.

Decoded RGB16 pixels reach the resident engine through memory on Windows as
they do on macOS. `multiprocessing.shared_memory` creates a named file mapping,
the engine opens it with `OpenFileMappingW`, maps exactly the protocol length,
and copies the pixels into its resident input cache before replying; the Python
side releases the mapping afterwards. A full-resolution RAW export therefore no
longer writes and re-reads a six-byte-per-pixel TIFF, and first previews at a
new size skip the disk as well. The TIFF route remains the fallback, and an
engine that reports the exchange unavailable is remembered so later renders go
straight to TIFF.

If future measurement shows that webview texture upload and paint dominate at
large preview sizes, each native host can add a child WGPU viewport while
retaining the webview for controls. The resident render protocol and shared
editing/export math are already outside the host, so that experiment does not
require another application rewrite or a forked Windows pipeline.

## Required proof before release

1. Run the full shared Python and Rust suites.
2. Compare `bench/benchmark.py` before and after on the Mac benchmark machine,
   including browser decode/upload/paint timing at 1100, 2200, and 5000 px.
3. Build on Windows x64 and pass `scripts/windows/runtime-smoke.py` from the
   packaged runtime.
4. Open the installed app on Windows and verify a RAW preview, a processed-file
   preview, folder operations, preset save, and an RGB16 TIFF export.
5. Confirm a RAW export reports `input_transport: shared-memory-rgb16`, that a
   second launch starts faster than the first, that no command window appears
   while rendering, and that switching folders shows the loading page rather
   than a frozen window.
6. Check an existing WebView2 runtime and a clean machine without one. Confirm
   setup handles the missing prerequisite and can be retried after an offline
   failure without damaging an existing install.
7. Compare 16-bit TIFF ramps and real RAW/JPEG/TIFF exports against the Mac
   reference. Check ICC profiles, orientation, and smooth gradients using the
   exported files, not only the remote desktop stream.

## First Windows GPU session

Use a Windows x64 desktop with a graphics-capable GPU driver. Record the OS,
driver, GPU, package source revision, and reported WGPU adapter/backend before
benchmarking. A Windows Server cloud desktop can establish installation,
rendering, and GPU behavior; a Windows 11 client still needs a separate pass.

Start with a small reproducible set: two JPEGs, one 16-bit TIFF gradient, and
RAWs from the cameras used for the existing Mac benchmarks. Test install,
first and second launch, import, film changes, continuous slider movement,
Compare, Fit/1:1 zoom, export, folder changes, quit/reopen, and edit recovery.
Repeat at 100%, 150%, and 200% display scaling. Use local render timings to
separate application delays from remote desktop latency, and download exported
files for pixel/color inspection. Cloud streaming is not proof of calibrated
monitor color or local display latency.

Windows currently presents the photo through the webview; the Mac's native
preview surface is not enabled in the Windows shell. Measure decode, GPU work,
webview upload, and presentation separately before choosing whether a native
DirectX viewport is needed. Apple-only AI providers and HEIF export also remain
separate feature-porting work.
