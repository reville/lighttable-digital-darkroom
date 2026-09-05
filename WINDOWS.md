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
`LightTable-windows-x64` artifact for the unified release workflow. Code signing
is not currently configured for Windows artifacts; runtime and installer smoke
tests do not establish Windows GUI or RAW-rendering proof.

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

## Performance path

The resident renderer remains a separate long-lived process on both platforms,
so GPU device creation, pipeline compilation, profile loading, and decoded
inputs stay warm. Adding Windows therefore does not force the Mac through a
portable renderer or a cross-platform desktop framework.

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
