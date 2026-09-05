# LightTable

A local photo browser and editor built around the
[spektrafilm](https://github.com/andreavolpato/agx-emulsion) spectral film
pipeline. Film simulation is the *base render*; ordinary editing controls
grade on top of it in real time.

## Installation

Public source lives at [reville/lighttable-digital-darkroom](https://github.com/reville/lighttable-digital-darkroom).
The repository starts with one initial snapshot; subsequent development uses
ordinary commits and pull requests.

**Desktop packages are being prepared.** The installer code is present, but
Homebrew/npm commands become available only after the first verified binary
release and package publication. Supported release targets are macOS 13+ on
Apple silicon and Windows 10/11 x64.

- Homebrew: our tap will provide `brew install --cask reville/lighttable/lighttable`.
  A fresh-machine `brew install lighttable` requires acceptance into Homebrew's main catalog.
- npm: `npm install --global lighttable`, then `lighttable install`, or one command
  `npx lighttable install`. The package name is not reserved by this source repository.
- Windows: WinGet, Scoop, and Chocolatey manifests are generated from the same
  installer/portable ZIP and their final SHA-256 checksums.

See [release setup](release/README.md), [installer definitions](packaging/INSTALLERS.md),
and [npm installer](packaging/npm/README.md) for preparation and publication steps.
All installers preserve original photos and user catalogs when removing the app.

## Run

**As a Mac app:** `build/LightTable.app` (rebuild with `bash build-app.sh`).
It owns the server's lifecycle and uses port 8321 when free.

**On Windows 10/11 (x64):** run `LightTable.exe` from the portable ZIP or use
the per-user installer. The package includes its Python runtime, native GPU
renderers, colour profiles, and desktop shell; it does not require a separate
Python or Rust installation. Windows builds use the system webview and DirectX
12 through WGPU.

**As a server:** `bash run.sh ["/path/to/folder"] [port]`.

**From the command line:** run `scripts/install-cli.sh`, then use
`lighttable status`, `lighttable photos list`, or `lighttable --help`.
The dependency-free client discovers an open app through its per-instance
registry, validates programmatic edits strictly, and can explicitly start an
isolated `--profile review` server. `render`, `analyze`, and `compare` provide
pixel-level proof without driving the interface. `lighttable mcp` exposes the
same manifest as a stdio MCP server. See [CLI.md](CLI.md) and [AGENTS.md](AGENTS.md).

For a self-contained, signed application with the updater enabled, use
`scripts/build-release.sh`. Publishing and signing setup is documented in
[release/README.md](release/README.md).

To refresh the isolated personal installation from the current checkout in
seconds, use `scripts/update-personal-app.sh`. It reuses the verified packaged
runtime and preserves the previous app as a rollback copy.

To make a reproducible Windows package on a Windows x64 build host with Rust,
Git, `uv`, and NSIS (`makensis`) installed:

```powershell
.\scripts\windows\build-release.ps1 -Version 0.1.0
```

The script verifies pinned downloads, compiles both GPU renderers and the
desktop shell, tests the packaged runtime and install/uninstall cycle, and writes
a portable ZIP plus an installer under `dist/`. Pass `-PortableOnly` explicitly
to build only the ZIP without NSIS.
See [WINDOWS.md](WINDOWS.md) for the platform boundary, future native-viewport
path, and release proof gates.

## How it works

The expensive film stage runs in a resident Rust/WGPU process that keeps its
Metal or DirectX 12 device, source buffer, and compiled pipelines warm. On
macOS, the film surface streams directly to an `MTKView`; Metal applies the
common global grade, curves, HSL, detail, Point Color, Color Grading, soft
proofing, zoom, and pan without JPEG decode or WebKit texture upload. Local
masks, manual optics, and up to sixteen heal spots stay in that Metal pass.
Windows and exact lens-profile remapping use the WebGL/cached-edit display path. Pixel-resampling
work (lens geometry and healing) is cached as a separate preview stage. Export
uses the same operation order at full resolution: base render, optical/healing
corrections, global grade, local masks, then crop and resize. Parity-safe sRGB
JPEG recipes stay in the resident Rust process through grade, curves, HSL,
Point Color, Color Grading, radial/linear/bitmap masks, crop, Lanczos resize,
and JPEG encode. Recipes with lens warps, healing, brush masks, watermarks,
wide-gamut output, or high-bit-depth TIFF retain the floating-point Python
finisher. Full-resolution RAW inputs cross into Rust as temporary RGB16 POSIX
shared memory on macOS/Linux, with the cached TIFF path retained as a bounded
fallback when the host cannot allocate a segment.

`grade.py` and `web/gl.js` implement the same operations in the same order and
**must be kept in sync**. `web/paritytest.html` verifies it: it renders a shared
PNG through the shader and prints channel means to compare against numpy.
The current 32-case run covers tone, colour mixer, local effects, detail, noise
reduction, and chromatic-aberration correction. Every case except vignette
agrees to **0.03/255** or better; vignette differs by 0.74/255 because of the
documented pixel-centre convention.

## Features

**Catalog** — one SQLite catalog in Application Support spans every source
folder, so several folders are open at once and "All Photographs" is a real
view rather than whatever tree was chosen at launch. A photo's identity is its
content, not its path: a file moved or renamed in Finder relinks to its edits
and keeps its warm render caches. Filtering, sorting, smart-collection rules,
and search run in SQL, so the browser never holds the whole library to answer a
question. Scanning happens behind the server, never in front of it, so the
window opens on what is already catalogued while new files arrive. Folders
edited by an older build are adopted from their `.lighttable-state.json` on
first sight, and an optional mirror keeps writing that file so a folder can
still carry its own edits; the mirror is best-effort and silent on read-only
volumes. Catalog backups are taken through SQLite's own backup API into a dated
zip, kept on a tiered schedule, and the log is checkpointed by background
maintenance. A damaged catalog is never replaced on its own: the app opens in
folder mode and Library Health (Catalog pane, Settings, or Help ▸ Diagnostics)
shows what a salvage would recover, what each backup holds, and the option to
start fresh, with the previous file kept in `Catalog/Recovery`. The same
dialog verifies and repairs the catalog, and releases a photo that was set
aside after crashing the server twice. The native shell relaunches a crashed
server, offers Safe Mode after repeated crashes, and rotates the server log.
See [RECOVERY.md](RECOVERY.md). `LIGHTTABLE_CATALOG=0` restores the previous
per-folder behaviour.

**Migration** — import a Lightroom Classic `.lrcat` with its ratings, flags,
colour labels, keyword hierarchy, collections, stacks, IPTC, GPS, develop
settings, and optionally edit history. The catalog is copied and opened
read-only, so it can be imported while Lightroom is running, and every table
and column is checked before it is read: a version that stores something
differently degrades to a named skip rather than a failure. Per-image XMP
sidecars can be read separately, which is the cheapest path for anyone whose
work already lives beside their originals. Both report what was mapped and what
was skipped, in the same voice as preset conversion. See
[PRESET-INTERCHANGE.md](PRESET-INTERCHANGE.md).

**Ingest** — copy from a card into dated folders with a rename template, an
optional second backup copy, and verification. Copies are verified by full
content hash before the job reports success, because a card is often erased
straight afterwards; a failed verification removes the incomplete destination
and leaves the source untouched. Duplicates already in the catalog are detected
by content and skipped by default, and a rerun resumes rather than making a
second set of files.

**Watched folders** — monitor a capture or hot folder every two seconds and
catalog settled arrivals in place or copy them through the verified ingest
path. Optional presets can land on arrival, follow mode selects the latest
capture only while the user is idle, and a persistent content ledger prevents
repeat imports across restarts. Sources are never moved or deleted.

**Browse** — desktop-style library rail, regular and rule-based smart
collections, collapsible photo stacks, independent non-destructive virtual
copies, borderless aspect-ratio Photo Grid,
metadata-rich Square Grid, single-photo Detail view, horizontal filmstrip,
adjustable thumbnails, lazy loading, filename/keyword/loaded-metadata search,
filter by flag, minimum rating, file kind, or virtual-copy status, and sort by
name / rating / flag / date. RAW thumbnails come from the camera's embedded
preview. Processed-image thumbnails use in-process ImageIO on macOS (with
orientation and a 240 px decode bound) and Pillow elsewhere; a four-worker
200-image 12 MP sample measured 937 thumbnails/second through ImageIO versus
770/second through the portable path on the benchmark Mac. Add Photos and Add
Folder use native macOS pickers and keep originals in place. Import from Apple
Photos can copy either the asset's current representation (preserving RAW,
HEIF, or embedded depth when available) or a compatible representation into a
durable folder under Pictures before it is catalogued. Folder
sources persist across launches and support Browse/Favorites, recursive scope,
photo counts, create, rename, reveal in Finder, synchronize, non-destructive
bookmark removal, and drag-to-move photos with adjacent XMP sidecars. Multi-
select uses cmd- or shift-click. Tested at 569 images: 23 ms full rebuild,
13 ms incremental update.

**Rate and cull** — 0-5 stars, pick, reject, unflag, and five colour labels,
all filterable and sortable. Survey view lays a selection or a burst out
together and sends the rating, flag, and label keys to whichever cell is
active; A/B narrows the same view to two, with a swap and a "make select" that
approves one frame and rejects the rest. Shift with a digit rates and advances.
Rejected photos in the current view go to the Trash with one shortcut, through
the platform, so the choice is recoverable. Two shortcut schemes are available
for people arriving from another editor.

**Film / Develop workspaces** — switch Film off to edit the neutral source as a
conventional raw editor, or leave it on and use the same grade controls after
the physical film pipeline. Develop mode first displays the camera preview,
then replaces it with the same accurate neutral raw conversion used by export.
The workspace choice is non-destructive and can also be part of a preset. With
Film off, a **Develop profile** chooses between Standard, which applies a base
curve with a toe, mid-tone contrast, and a highlight shoulder while holding 18%
grey exactly where it started, and Linear, which is the bare scene-referred
render and the one to use when matching a scan.

**Film** — a non-destructive profile bypass plus 23 stocks and 9 print-stage
profiles. Authentic mode follows each stock's intended print profile unless
the paper is explicitly locked; Creative mode preserves manual combinations.
The capture stage has camera EV, scene metering, RAW white balance, film format,
and diffusion. Film development has measured B&W time choices plus a clearly
labeled creative contrast control. Print and scan stages expose print exposure,
preflash, Y/M filtration, print glare, modeled output recipes, scan softness,
and scan sharpening. Reversal stocks bypass the print stage entirely.

Stock choice now changes the physical render, not only the color response:
grain uses a stock-specific particle-area baseline (including pushed-stock
variants), the control is a multiplier over that baseline, and film format
sets the physical image size used to convert micrometres to pixels. Judge grain
at the viewer's **1:1** setting. See [FILM-PROFILES.md](FILM-PROFILES.md) for
the stock-by-stock routing, provenance, and calibration limits.

**Grade** — exposure, contrast, highlights, shadows, whites, blacks, temp,
tint, vibrance, saturation, texture, clarity, dehaze, and vignette. Detail adds
sharpening amount, radius, detail, edge masking, luminance noise reduction, and
colour noise reduction. Optics adds manual red/cyan and blue/yellow fringe
correction. Switchable live RGB histogram, luma waveform, RGB parade, and
YCbCr vectorscope views sit beside the optional clipping readout, auto-tone,
and white-balance dropper. Scopes and white-balance sampling use tiny GPU
readbacks rather than copying the full preview canvas back to the CPU.
In Detail view, holding a mapped speed key while scrolling, dragging, or using
the arrow keys adjusts its slider; a tap keeps the key's ordinary action and a
double tap resets the control.

**Point Color and Color Grading** — sample up to eight exact source hues, tune
their affected range, hue shift, saturation, and luminance, or even out hue,
saturation, and luminance variation toward the sampled patch. Then finish with
independent shadow, midtone, highlight, and global grading wheels. Blending
and balance control the transition between tonal ranges. The CPU exporter,
WebGL preview, and native Metal preview use the same ordered math.

**Masking** — up to sixteen named, non-destructive masks using on-device
Subject, Sky, Object, Depth, Person, Face Skin, Eyes, Eyebrows, Lips, Teeth,
and Hair selection or manual Brush, Linear, and Radial tools. Depth preserves
a continuous far-to-near field, uses embedded capture depth when available,
and otherwise estimates from local image cues; its near/far range remains
adjustable after analysis. Add, Subtract, and
Intersect refinements, visibility, overlay, flow, opacity, inversion,
luminance and sampled-colour ranges, and grouped Light, Color, Texture, and
Clarity controls follow the same create-refine-adjust workflow in preview and
export. Fine portrait masks use compressed 1024-edge bitmaps; mask data is
packed into one vertical GPU atlas, so local sliders remain immediate at the
new cap. People analysis stays on the Mac and never infers identity.

**Remove & Heal** — automatic inpainting removal plus deterministic two-point
heal or exact clone spots, selected from a compact mode bar. Size, feather,
opacity, direct target/source dragging, source refresh, per-correction
visibility, and a high-contrast spot visualization stay on the image. Up to 50
spots are stored as edit instructions; originals are never modified.

**Auto level and upright** — the frame's own lines are detected and solved
into the existing parametric geometry model, with the scale needed to avoid
empty corners. On synthetic tilts the recovered angle is within 0.006-0.17° of
truth; keystone within 0.004. Rotation beyond the supported ±15° is clamped and
said so rather than silently half-applied.

**Camera profiles** — `.dcp` files from a folder you choose can be applied as
an explicitly approximate camera look: the hue/saturation map, look table, and
tone curve are applied exactly, while dual-illuminant interpolation and
forward-matrix adaptation are not implemented and are named per file.

**Lens** — exact camera/lens matching against the bundled profile database,
with profile distortion, transverse colour-fringe, and lens-shading correction
when those calibrations exist. Manual distortion, vignette, vertical and
horizontal perspective, horizontal/vertical flip, fine rotation, and scale
remain available without a match. A known fixed-lens successor can use its
optically compatible profile;
otherwise LightTable declines an uncertain automatic match.

**Tone curve** — draggable point curve, RGB plus per-channel. The editor
computes a 256-entry table; the shader samples it and the exporter applies the
*same table*, so there is no second interpolation to drift.

**Colour mixer** — hue / saturation / luminance across eight hue bands.

**Presets** — full styles can replace the edit; tool presets can layer only
their included controls. Presets may include the Film pipeline, add a grade on
top of Film, or switch Film off for a conventional Develop workflow. Import
supports LightTable `.ltpreset`, Lightroom / Camera Raw `.xmp` and legacy
`.lrtemplate` (including ZIP bundles), and Capture One `.costyle` /
`.costylepack`. Export supports LightTable, XMP, and `.costyle`.
Cross-engine conversions are deliberately approximate and show
mapped and skipped operations. See [PRESET-INTERCHANGE.md](PRESET-INTERCHANGE.md).

**Versions** — named, per-photo editing checkpoints that restore film, grade,
crop, masks, healing, and lens geometry without duplicating the source image.

**Keywords** — per-photo tags saved alongside ratings and edits, searchable
from the workspace bar.

**Optional local AI index** — disabled by default. When enabled from the Local
AI inspector, a single background worker uses Apple Vision to add searchable
object/scene tags, visible text, and face counts. Analysis stays on the Mac;
the generated SQLite index lives in LightTable's Application Support directory,
not beside originals or in a synced photo folder. Pausing hides generated
metadata without deleting it, and Delete Index removes it without touching
photos or edits. Face detection does not guess identities.

**Assisted culling** — a first pass over a shoot, built on the same index and
equally optional. Three reasons to keep a frame (subject sharpness, eye
sharpness, eyes open) and three to drop one (exposure issues, misfires,
documents). Each photo gets a plain yes, no, or "unknown" per criterion with
the reason shown beside it, and criteria that cannot be judged honestly — no
face in frame, no subject found, a face too small to read — say so rather than
answering no. Tick the criteria that matter, review the selects or the rejects
as a filtered view, then apply pick or reject flags in one step. Nothing is
flagged until you press the button, and rejected photos stay where they are
until you trash them.

Two of the six need no Vision at all and work from the pixels: exposure looks
at clipping, favouring the subject when one was found, so a blown sky behind a
well-exposed face is not treated as a fault; misfires look for a frame with no
plane of focus anywhere. The other four use Vision's subject mask and face
landmarks. Verdicts are stored with the version of the analysis that produced
them, so improving a measurement re-scores the library on its own.

On macOS 27 or later, Apple Foundation Models can add a natural-language
description when Apple Intelligence and the optional Python bridge are
available. Install that additive bridge with
`uv pip install --python .venv/bin/python -r requirements-ai.txt`. Vision
indexing remains available without it, and all normal browsing, editing, and
export code remains independent of `film_lab_ai/`.

**Video** — clips are catalogued, searched, filtered, and played with a
poster frame and seeking. They are not edited: the film and grade pipelines are
for stills, and offering controls that quietly did nothing would be worse than
leaving them off.

**Enhance** — learned denoising in the Develop pipeline, run through a bundled
Core ML model in macOS release builds. `scripts/fetch-models.py` downloads
SCUNet against pinned hashes and `scripts/convert-models.py` converts it; both
keep the licence text and provenance beside the weights. SCUNet was chosen over the better-known
real-noise denoisers because it is trained on synthesised degradations rather
than the SIDD or DND smartphone pairs, so it generalises to sensor noise from
cameras it has never seen — which matters when the app opens files from more
than a thousand models — and because it is blind, needing no noise-level input.
Its network is Apache-2.0 and its weights MIT, both compatible with this
project's licence.

The fp16 package is 78 MB. On this Apple silicon Mac, a warm standalone
512×512 smoke completes in 0.38 s including process startup; a new model
identity pays a one-time Core ML compile first. The current three-file
evaluation selects gamma encoding and measures +12.65 dB over the noisy FBDD
baseline at middle synthetic noise, mean flat-patch ΔE76 0.341, and 1.08 s/MP
warm end to end. Its high-ISO contact sheet shows progressively stronger
smoothing without invented structure. `bench/denoise_eval.py` records the
scores and the required visual-review sheet locally.

Development builds without the converted model report the feature unavailable
and refuse to run rather than passing pixels through unchanged, because a
silent pass-through is indistinguishable from a model that did nothing.
Super-resolution shares the same plumbing but has no recommended model; see
[WORKFLOW-ROADMAP.md](WORKFLOW-ROADMAP.md).

**Metadata** — camera, lens, focal length, aperture, shutter, ISO, dimensions
and capture date, plus editable IPTC: title, caption, headline, creator,
copyright, credit, city, state, country, and GPS. Keywords are hierarchical:
the catalog interns `Parent > Child` paths and filtering on a parent matches
everything under it. Metadata can be applied across a selection, is written
into exports when the recipe asks for it, and can optionally be written back to
`.xmp` sidecars beside the originals.

**Compare** — drag the divider directly across the photo, use the wipe slider, or hold `B`.

**Reference match** — load a scan of the same frame, align it as an overlay or
split view, and measure mean RGB, luminance, pixel error, and an approximate
Delta E 76. Starting Match adjusts only inspectable physical controls: camera
exposure and RAW capture WB, or print Y/M filtration for processed inputs. It
never creates a hidden HSL transform or opaque LUT. A deterministic calibration
target and a paired-image measurement script live in `calibration/`.

**Geometry** — drag crop with aspect-ratio presets, 90° rotation,
horizontal/vertical flip, pinch/scroll
zoom with pan, fit and 1:1.

**Workflow** — persistent copy/paste controls with disabled and success states,
multi-selection paste, paste to every visible image, and undo/redo. Local masks,
healing, and lens corrections travel with copied settings and native presets.

**Export** — approved / rated / all, reusable recipes, native destination
selection, collision policies, token-based filename templates, a text or image
watermark, a metadata policy per recipe (none, copyright only, everything, or
everything except location), an optional recipe sidecar, JPEG-HEIF-PNG-TIFF,
quality, long-edge resize,
and sRGB, Display P3, or ProPhoto RGB output. JPEG/PNG and macOS HEIF are
8-bit; TIFF is true RGB16. Every export embeds the matching ICC profile and
writes an adjacent
`.lighttable.json` recipe/provenance sidecar. Film and grade calculations stay
floating point until the final encoder. Jobs run two at a time into
the chosen destination, sharing the resident GPU film engine. An original and
its virtual copies export under distinct display names while sharing the same
source decode.
On the benchmark Mac, a cold 10 MP RAW with the standard benchmark grade and a
512 px JPEG delivery fell from 2.02 seconds on the former TIFF/Python path to
0.93 seconds through shared memory and the Rust finisher (2.17x). The former
path wrote 45.6 MB of input TIFF plus 91.2 MB of float film TIFF; the direct
path wrote neither. These figures are measured for that image and recipe, not
a blanket estimate for every camera, output size, or edit combination.

**External editing** — render an adjusted 16-bit ProPhoto TIFF beside its
original (or open a processed original), hand it to a chosen pixel editor, and
register the saved derivative back into the catalog as a stacked photo. Its
rating, flag, label, keywords, and IPTC state carry over; baked develop edits
do not. The session watcher refreshes the same catalog row when the editor
saves again.

**Photo Merge** — selected bracketed frames can be aligned, deghosted, and
exposure-fused into an HDR master. Overlapping frames can be feature-aligned,
projectively stitched, and feathered into a panorama. Focus-bracketed frames
can be aligned for small breathing changes and fused through a sharpness-
weighted Laplacian pyramid, with a 60-frame cap. All three workflows create a
16-bit ProPhoto TIFF plus a source-manifest sidecar in `LightTable Merges/` and
add the result to the local library.

**Soft Proofing** — interactive sRGB and Display P3 display targets plus matte
and gloss print previews. Print targets simulate target gamut, black point,
paper white, and optional magenta out-of-gamut warnings without changing the
photo or export recipe.

## Engines

- **Rust / GPU** ([spektrafilm-rs](https://github.com/turbasvin/spektrafilm-rs))
  resident WGPU process (Metal on macOS, DirectX 12 on Windows), ~50 ms per
  warmed preview on the benchmark Mac. Built from `rust-engine/`; needs
  `engine/data` at runtime.
- **Python** the reference implementation, ~1.4-3 s. Automatic fallback.

## Input

RAW is decoded with rawpy to scene-linear ProPhoto RGB (`gamma=(1,1)`,
`no_auto_bright`). The accepted extension list now covers every format the
bundled LibRaw decodes rather than the original eight, which had been refusing
files the decoder could already read: Pentax, Hasselblad, Phase One, Leica,
Samsung, Minolta, Epson, Kodak, and older Canon among them. The supported-camera
page can be generated into the canonical website checkout from the linked
library — 1275 cameras across 83 manufacturers at LibRaw 0.22. Python, Swift,
Rust, JavaScript, and the benchmark now consume `media-formats.json` rather
than maintaining five
independent extension lists. As-shot, daylight,
tungsten, and custom capture white balance happen before film exposure and are
part of the decode-cache identity. RAW development also exposes camera/detail/
smooth demosaic profiles, blend or reconstruct highlight recovery, and light
or full sensor-stage noise reduction. Those choices can be saved per camera
and are included in every preview/export cache key. Previewing is progressive:
the camera's
embedded preview is converted into a provisional linear draft for immediate
feedback, then an accurate full-quality demosaic replaces it after the first
frame is visible. RAW files without an embedded preview fall back to a
half-size draft decode. The "before" view uses the camera preview (or rawpy's
normal conversion as fallback), since a bare linear file is not a fair
comparison.

With Film disabled, the accurate preview and full-resolution export start from
the same neutral TIFF cache. This avoids a mode where the screen shows an
embedded camera JPEG while export silently uses a materially different raw
render.

Processed files (HIF/JPEG/HEIC) are ColorSync-converted to ProPhoto RGB before
the film model. They work, but remain a compromised input: the camera has
already tone-mapped, clipped, and may have applied a film simulation.

Some files carry `Orientation=1` despite portrait content, so rotation is a
manual control rather than inferred.

## Layout

- `server.py` — HTTP server, state, caching, export queue
- `film_pipeline.py` — UI params to spektrafilm params
- `color_pipeline.py` — float loading, RAW capture WB, output transforms and ICC export
- `calibration_target.py`, `calibration/` — target generation and paired-reference measurement
- `profiles/` — tracked LightTable profile data and reproducible generators
- `FILM-PROFILES.md` — profile provenance, pairing, and render-smoke audit
- `grade.py` — post-film grade (numpy, export)
- `edits.py` — local masks, healing, lens profiles, and perspective geometry
- `semantic_masks.py` — local Subject, Sky, and seeded Object selections
- `export_workflow.py` — export recipes, paths, templates, and collisions
- `library_workflow.py` — collections, stacks, and virtual-copy identities
- `merge_workflow.py` — HDR exposure fusion and panorama alignment/stitching
- `soft_proof.py` — CPU reference for the interactive proof shader
- `preset_io.py` — bounded preset archives plus loss-aware import/export
- `film_lab_ai/` — optional local index worker, SQLite store, providers, and
  standalone Apple Vision helper
- `rust-engine/` — resident WGPU/Metal film renderer and image encoder
- `windows-shell/`, `scripts/windows/` — native Windows host and reproducible
  portable/installer packaging
- `catalog.py` — the SQLite catalog: schema, queries, keywords, history, backup
- `catalog_scan.py` — scanning, content hashing, relink, state-file migration
- `catalog_import.py` — read-only import of another editor's catalog
- `xmp_sidecar.py` — XMP sidecar reading and writing
- `ingest_workflow.py` — card ingest planning, templates, verified copying
- `geometry_auto.py` — line detection and the auto level/upright solve
- `camera_profile.py` — approximate `.dcp` camera profile application
- `enhance_workflow.py` — tiling and output masters for a learned model
- `scripts/camera-list.py` — generates the canonical site's supported-camera
  page from LibRaw (pass `--output` or set `LIGHTTABLE_SITE_ROOT`)
- `render_cli.py` — Python fallback for one full-res export
- `bench/benchmark.py` — repeatable server and real-browser startup, slider,
  decode/upload, 1100/2200/5000 px preview, large-library, burst, thumbnail,
  and export benchmark
- `bench/merge_benchmark.py` — repeatable HDR, panorama, and focus-stack
  algorithm benchmark with the active alignment and memory bounds in its JSON
- `web/` — native ES modules for state, API/native bridge, render scheduling,
  library/search, editor panels, color tools, presets, local AI, and the shared
  WebGL renderer; no bundler or build step
- `tests/` — unit, engine-parity, profile-smoke and performance-contract tests
- `engine/`, `vendor/`, `.venv/` — not in git; see above

## Reproducibility and calibration limits

Preview and export cache keys include source identity, the cleaned recipe,
profile-catalog digest, and renderer identity. Saved edits and export sidecars
record the LightTable revision plus hashes/revisions for the resident engine,
one-shot engine, Python reference, and profile catalog when available.

Stock spectral data and measured B&W development families retain their source
citations in the profile catalog. Film-format geometry is physical. Output
recipes, pushed color-negative grain baselines, diffusion defaults, and scan
MTF/glare starting points are explicitly labeled **modeled** or **visual
calibration** rather than measurements of a named lab. The workflow in
`calibration/README.md` is the path for replacing those assumptions with
paired RAW/film/reference-scan measurements.

The remaining high-value workflow and raw-quality gaps are tracked in
[DEVELOP-ROADMAP.md](DEVELOP-ROADMAP.md).
The catalog, migration, ingest, metadata, and image-quality plan for the
next milestone is in [WORKFLOW-ROADMAP.md](WORKFLOW-ROADMAP.md).
The eight gaps that remain against the rest of the field, with a plan for
each, are in [GAPS-ROADMAP.md](GAPS-ROADMAP.md).

## License

[GNU GPL version 3](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md).
