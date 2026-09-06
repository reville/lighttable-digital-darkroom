# LightTable

**Highly performant digital darkroom with realistic film stock effects.**

A free, open-source photo browser and RAW editor with a native macOS app, a
Rust and GPU-driven render core, and physical film simulation built on
[spektrafilm](https://github.com/andreavolpato/agx-emulsion).

[Website](https://lighttable.app/) ·
[Screenshots](https://lighttable.app/screenshots.html) ·
[Features](https://lighttable.app/features.html) ·
[Get started](#get-started) ·
[Contribute](#contributing)

[![LightTable's native macOS Film workspace, showing film stock and physical print controls](https://lighttable.app/screenshots/film-controls.webp)](https://lighttable.app/screenshots.html)

## Maintainers wanted

We're looking for Windows, macOS, and Linux maintainers, including a maintainer
focused specifically on Omarchy. If you'd like to help,
[open an issue](https://github.com/reville/lighttable-digital-darkroom/issues/new)
and tell us which platform you'd like to maintain.

## What makes LightTable great?

### The photograph comes first

The photograph stays large while the library, film controls, and editor share
one quiet workspace. Catalog originals in place, keep edits separate, and use
ratings, flags, collections, stacks, and virtual copies to organize your work.
Film can be switched off for conventional RAW development.

### Film is a complete image pipeline

Choose from **23 film stocks**, each with its own spectral response, density
curves, and grain. Spektrafilm models the photograph from exposure through
scan, using published sensitivity and density data.

- **Capture:** stock, film format, exposure, white balance, and diffusion.
- **Development:** development controls, halation, and physical grain scaled
  to the stock and film format.
- **Print:** paper, exposure, preflash, and yellow/magenta filtration.
- **Scan:** glare, softness, and sharpening. Reversal film goes straight to scan.

The print and scan stages remain editable. Profile sources and the distinction
between measured data and modeled assumptions are documented in
[Film profiles](FILM-PROFILES.md) and the [calibration guide](calibration/README.md).

### Bring your Lightroom library

LightTable reads a **copy** of a Lightroom Classic catalog. Ratings, flags,
labels, keywords, collections, stacks, virtual copies, metadata, and supported
develop settings can come across without changing the original catalog.

It also imports Lightroom / Camera Raw XMP, legacy `.lrtemplate` presets, and
Capture One styles. A conversion report names what was mapped and what was
skipped, and labels converted settings as approximate:
different render engines produce different results. See
[preset interchange](PRESET-INTERCHANGE.md) for compatibility details.

### A full editing workflow

RGB curves, Point Color, four-way Color Grading, local masks, heal and clone,
lens correction, crop and perspective, versions, and reusable export recipes
sit alongside the film controls. HDR, panorama, and focus merge create new
masters. Soft proofing covers sRGB, Display P3, matte paper, and gloss paper.

RAW decoding uses rawpy and LibRaw. Capture white balance and demosaicing happen
before film; processing stays floating point until the final encoder. Export
JPEG, PNG, macOS HEIF, or true RGB16 TIFF with an embedded ICC profile for sRGB,
Display P3, or ProPhoto RGB.

Export recipes can use one destination, a subfolder beside each original, or
the original folder hierarchy beneath a destination. The dialog previews paths
across source folders before starting. Optional capture-time file timestamps
use the embedded timezone; missing timezones are reported unless you explicitly
choose this computer's local timezone. Metadata and recipe-sidecar policies are
saved with the recipe. Cancel stops queued work, waits for active cleanup, and
retains completed files. Export details list skipped files, errors and warnings.

### Local by design

Photos, the SQLite catalog, edit history, and render caches stay on your machine.
Optional Apple Vision analysis runs on your Mac and adds searchable scene and
object tags, visible text, face counts, and subject masks. It does not identify
people or upload the analysis.

## One RAW, three paths

The **same Nikon Z6 RAW** feeds all three renders, with no manual matching
after the render.

| Neutral conversion | Imported preset | LightTable film process |
| --- | --- | --- |
| [![Neutral RAW conversion](https://lighttable.app/assets/film-comparison/neutral-frame.jpg)](https://lighttable.app/assets/film-comparison/neutral-frame.jpg) | [![Lightroom-compatible XMP rendered by LightTable](https://lighttable.app/assets/film-comparison/imported-xmp-frame.jpg)](https://lighttable.app/assets/film-comparison/imported-xmp-frame.jpg) | [![Kodak Gold 200 simulation printed to Portra Endura paper and scanned](https://lighttable.app/assets/film-comparison/lighttable-film-frame.jpg)](https://lighttable.app/assets/film-comparison/lighttable-film-frame.jpg) |
| Film off, standard base curve | 16 controls mapped, Film off | Gold 200 → Portra Endura paper → modeled neutral scan |

The middle image is a Lightroom-compatible XMP imported and rendered by
LightTable, **not an Adobe Lightroom render**. The
[homepage comparison](https://lighttable.app/#film-proof-title) includes matched
100% crops and print/scan variations. Its
[build manifest](https://lighttable.app/assets/film-comparison/manifest.json)
records the source, parameters, crop coordinates, hashes, and renderer revision.

## Get started

**LightTable is in active development. The first public desktop release is still
being prepared.** Check [Releases](https://github.com/reville/lighttable-digital-darkroom/releases)
for published downloads; package-manager installers are not available yet.

| Platform | Current status |
| --- | --- |
| macOS 13+, Apple silicon | Native AppKit/WebKit app with a Metal preview. Source builds are available; signed public packages are being prepared. |
| Windows 10/11, x64 | Windows shell and installer build tooling are present, using WGPU/DirectX 12. Public packages and broader platform validation are still needed. |
| Linux, including Omarchy | Maintainers wanted to establish and validate the desktop build, packaging, and platform support. No supported Linux desktop release yet. |

For development, follow the [source setup guide](CONTRIBUTING.md#source-setup-macos).
A fresh clone needs the Python runtime dependencies and pinned film data before
it can run. See [release setup](release/README.md) for bundled builds and
[Windows](WINDOWS.md) for its build instructions and platform boundaries.

## How it's made

| Layer | Implementation |
| --- | --- |
| Desktop hosts | Swift, AppKit, WebKit, and Metal on macOS; Rust with Tao/Wry and the system webview on Windows. |
| Interface | HTML, CSS, and native JavaScript modules in [`web/`](web/). No frontend bundler or build step. |
| Local application | Python handles the HTTP API, catalog, imports, edit state, render scheduling, and exports. SQLite stores the library. |
| Film renderer | A resident Rust/WGPU process in [`rust-engine/`](rust-engine/), adapted from [spektrafilm-rs](https://github.com/turbasvin/spektrafilm-rs). The Python spektrafilm implementation provides a reference and fallback. |
| Image processing | rawpy/LibRaw decode RAW; NumPy and the native GPU paths implement grading and finishing. Platform image services handle decoding and color management where available. |

The render order is **RAW decode → film (optional) → optical/healing corrections
→ global grade → local masks → crop and resize → encoded output**.
The resident engine keeps its GPU device, image buffers, and pipelines warm;
ordinary grade changes run in the live Metal preview or WebGL fallback.

On the [documented benchmark Mac](bench/results/responsiveness-2026-09-01.md),
a warmed 1100 px film render had a median of **50.5 ms across 30 runs**.
That measures the film stage for that workload, not total application latency.

### Engineering choices

- **One edit contract across renderers.** Metal, WebGL, and export must agree on
  operation order and grading behavior. Pixel changes need parity checks.
- **Originals stay separate from edits.** Edits live in the catalog; exports
  produce new files. Catalog recovery and backups have an explicit
  [recovery workflow](RECOVERY.md).
- **No silent approximation.** Imports report unsupported controls. Automatic
  lens correction requires a confident profile match; manual controls remain
  available when profile data is missing.
- **Reproducible output.** Cache identity includes the source, recipe, profile
  catalog, and renderer. Saved edits and export sidecars record provenance.
  Modeled film parameters are labeled as modeled.
- **Native platform strengths.** macOS keeps its Metal and image-services paths;
  other hosts share the film and edit model while providing their own integration.

### Command line and MCP

The app exposes a local API, a command-line client, and an MCP server. With the
source environment prepared and an app running:

```sh
./lighttable status --json
./lighttable photos list --limit 5 --json
./lighttable schema
```

Use `./lighttable mcp` for the stdio MCP server. Programmatic edits appear in
History and support Undo. See the [CLI reference](CLI.md) for commands,
installation on your PATH, and isolated review profiles.

## Contributing

Platform maintenance, RAW compatibility reports, renderer work, documentation,
and reproducible bug reports are welcome. For a substantial change,
[open an issue](https://github.com/reville/lighttable-digital-darkroom/issues/new)
to discuss the approach before building it. For bugs, include your platform,
app revision, reproduction steps, and a sample image you have permission to share
when the problem depends on a particular file.

The [contributor guide](CONTRIBUTING.md) covers source setup, local builds,
tests, and what to include in a pull request. The repository has Python unit
and contract tests, Rust tests, renderer parity checks, and real macOS
[product journeys](JOURNEY-TESTING.md). Run the checks that exercise your change
and report what you verified.

## Project documentation

- [Film profiles and provenance](FILM-PROFILES.md)
- [Preset import and export](PRESET-INTERCHANGE.md)
- [CLI and automation](CLI.md)
- [Responsiveness and benchmarking](RESPONSIVENESS.md)
- [Catalog recovery](RECOVERY.md)
- [Windows architecture](WINDOWS.md)
- [Release and installer setup](release/README.md)
- Roadmap notes: [RAW development](DEVELOP-ROADMAP.md),
  [catalog and workflow](WORKFLOW-ROADMAP.md), [remaining gaps](GAPS-ROADMAP.md).
  Implementation notes are not release guarantees; check Releases for shipped versions.

## License and credits

LightTable is free software under the [GNU GPL v3](LICENSE), distributed as
**GPL-3.0-only**. You can inspect, modify, and redistribute it under that license.

The film pipeline builds on Andrea Volpato's
[agx-emulsion / spektrafilm](https://github.com/andreavolpato/agx-emulsion)
and the [spektrafilm-rs](https://github.com/turbasvin/spektrafilm-rs) Rust port.
See [third-party notices](THIRD_PARTY_NOTICES.md) for pinned upstream revisions,
component licenses, and test-image attribution. The
[website source](https://github.com/reville/lighttable-site) is maintained separately.


Cloud placeholders and lens matching: LightTable catalogs macOS dataless photos as
`cloud-only` without reading their content. Existing metadata, edits and identities
survive eviction. Download the file in Finder using Download Now or Keep Downloaded,
then rescan the source; it becomes available even if its size and timestamp did not
change. Other providers that do not expose macOS's dataless flag may still block on
filesystem I/O; this check is not a general network timeout or download manager.

Lens correction requires one compatible automatic match. Missing or ambiguous
metadata leaves automatic correction off and explains the reason. The Lens profile
selector offers compatible bundled profiles and stores the explicit choice with the
photo's optics, so preview and export use the same choice. A saved profile that is
no longer compatible stays unavailable rather than silently choosing another lens.

Run `.venv/bin/python bench/storage_readiness.py --output /tmp/storage.json` for a
reproducible 10,000-record cold/mixed catalog benchmark. It uses distinct synthetic
file headers, 20% injected cloud flags and a configurable delay per fingerprint and
metadata operation. Results include first-page latency, concurrent query median/p95,
scan time and content-read counts, followed by a warm scan and hydration rescan.
This measures the actual scanner and SQLite query path with simulated slow reads;
it does not establish physical HDD, network-drive, 8 GB RAM, cloud-provider, RAW
render or UI performance. “Cold” means an empty catalog, not a flushed OS disk cache.
