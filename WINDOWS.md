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

`.github/workflows/windows-build.yml` checks pull requests and produces packages
on `main`, manual dispatch, or reusable-workflow calls. The reusable workflow
accepts a required `version` string and uploads both files in the
`LightTable-windows-x64` artifact for the unified release workflow. `source_ref`
selects the exact release commit. `require_signing` defaults to `false` for CI;
the public release workflow sets it to `true` and passes signing secrets to the
reusable workflow. Runtime and installer smoke tests do not establish Windows
GUI or RAW-rendering proof.

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
