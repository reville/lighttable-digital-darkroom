# Preview and export performance

The September 2026 changes target first open, film controls, actual-size viewing,
export, and initial thumbnail fill. Export pixels and user state remain durable;
only reproducible caches use atomic publication without filesystem synchronization.
Malformed generated TIFF/JPEG files are discarded and rebuilt.

## Implemented paths

- Embedded RAW camera JPEGs use DCT draft decoding and a source-versioned,
  byte-bounded cache shared by provisional film input and Before/Match.
  JPEG/HEIC previews decode and color-convert at preview size; full-resolution
  TIFF conversion remains available for exports.
- Startup executes a small native render for the default and last-used stock/
  paper pairs, compiling the actual film and native packing shaders. Ordinary
  native responses obtain camera/lens identity from the catalog; enabled lens
  corrections still retrieve focal length/aperture from full metadata.
- Resident spectral state and developed-film checkpoints survive print-stage
  changes, including enlarger filtration, preflash, paper, glare, scanner blur,
  sharpening, and output recipes. Direct-positive scanner reference adjustments
  that physically affect capture correctly invalidate the film checkpoint.
- Film requests adapt their interval to measured round trips. HTTP/1.1 reuses
  connections; event streams remain close-delimited and rejected requests close
  their connection so unread bodies cannot become another request.
- Export, prefetch, and edited-thumbnail renders use a separate resident engine
  and admission gate. An input probe skips repeated full RAW decoding. Export
  responses/status expose phase timings for input exchange, engine work, ICC,
  metadata, finishing, and publication.
- The GPU rotates, packs RGBA, and computes the native mean. Immutable POSIX
  shared surfaces feed mmap-backed Metal textures without an HTTP fetch or
  texture upload. A bounded server cache owns/unlinks them; disk and browser
  fallbacks are produced lazily. This is shared-memory transport, not IOSurface.
- Native 1:1 renders request an output-oriented pixel rectangle. The renderer
  retains full-frame metering and pixel pitch, uses absolute grain/glare
  coordinates, and includes the required spatial-kernel margin. Panning retains
  a full-frame fallback while the next tile arrives. Browser previews and
  unsupported edit combinations retain full-frame behavior.
- Scan-time source thumbnails use one bounded, cancellable background worker,
  yielding to interaction. Measured RAW thumbnails retain rawpy: ImageIO was
  slower on the tested NEF and CR2 files.
- Opening a photo (the navigation request) allows the embedded-camera draft
  through the film pipeline and refines it; requests for a photo already on
  screen still ask for accurate pixels only. Neighbour prefetch starts behind
  the first frame instead of after the settled full-width render. A superseded
  decode (a prefetch overtaken by the navigation it anticipated) keeps running
  while a live request waits on it, so the demosaic completes once and serves
  both instead of restarting. Import and rescan queue a standard-preview
  worker after the source thumbnails: it prepares each RAW's accurate 1100 px
  input (the editor's first-frame width) at the lowest admission priority
  without retaining the demosaic in memory, so the first frame after
  navigation costs one film render. The Rust input tier defaults to 2 GiB to
  hold a few hundred of those previews alongside the working set.
- macOS packaging adds a reproducible, relocatable OpenMP decoder for Bayer
  and X-Trans. X-Trans tiles run in dependency order to retain exact serial
pixels; older wheels lacking that schedule retain stock decoding. See
  [RAW runtime details](../packaging/README-rawpy.md) for the source pins, macOS 13
  dependency checks, compiler selection, and deterministic tile scheduling.

## Measured verification

Measurements below are local runs on the same Apple-silicon Mac. Engine tests
use synthetic textured input to make exact comparisons repeatable; cold input
and application journeys use real-photo fixtures. They are not input-to-photon
latency claims.

| Measurement | Before | After |
| --- | ---: | ---: |
| Cold NEF input preparation plus Match, 1100px | 174ms | 72ms |
| Cold 24MP JPEG preview preparation | 573ms | 47ms |
| 24MP Bayer DNG decode, stock versus 8-thread decoder | 772ms | 450ms |
| 40MP X100VI X-Trans decode, stock versus 8-thread decoder | 12908ms | 4291ms |
| 26MP X-T30 III X-Trans decode, stock versus 8-thread decoder | 8080ms | 2641ms |
| 12MP XQ2 X-Trans decode, stock versus 8-thread decoder | 3619ms | 1837ms |
| Downstream film controls, 1100px GPU median | 12.43ms | 4.49ms |
| Downstream film controls, 2200px GPU median | 52.24ms | 18.40ms |
| Eligible 2200px full render versus viewport GPU median | 42.61ms | 3.61ms |
| Native pack/publish, 2200px median | CPU packing replaced | 0.595ms |

Checkpoint tests compare 53 cases at both 1100 and 2200px byte for byte, plus an
independently rebuilt spectral-pipeline reference. Viewport tests compare 48
cases at each size against exact full-frame float32 crops, including rotation,
panning, frame edges, grain, glare, scanner kernels, and full-frame fallbacks.
Native transport tests compare every RGBA byte for disk/shared and full/viewport
outputs, and real Metal tests cover padded rows, mapping lifetime after unlink,
and tile sampling with a retained full-frame texture.

The X-Trans follow-up built the actual macOS 13 wheel from pinned sources,
including the patched diagonal schedule and Clang 17 compiler selection.
On three X-Trans cameras and one Bayer camera, two fresh-process runs each of
stock, candidate 1-thread, candidate 4-thread, and candidate 8-thread decoding
produced identical dimensions and SHA-256 pixel hashes: 32 full RAW decodes.
The table uses medians of the stock and 8-thread runs. The 1-thread candidate
is for parity checks; the performance gain requires multiple threads.
Another 32 half-size decodes, 18 alternate-setting decodes (one-pass,
smooth, daylight white balance/highlight blending), and two 2-/16-thread
stress runs also matched stock: 84 final-wheel decodes in total. The app's
`decode_raw` entry point used `rawpy_openmp` and matched stock with the detail
profile, tungsten white balance, and highlight blending. The wheel, build
metadata, JSON measurements, and suite log are retained in
`bench/results/xtrans-wavefront/` as ignored local evidence.

The real macOS photo journey verifies native presentation, navigation, edit
recovery, film controls, completed actual-size tiles, panning, return to Fit,
and export in a disposable catalog. Export integration also verifies a second
export uses resident input and that a preview completes during another export.
The final native journey's uncached viewport film adjustment completed in 41ms
including the request and Metal presentation acknowledgement. The separate RAW
integration run completed a preview in 27ms during a 507ms export; repeated
export avoided decoding and completed in 460ms versus 1363ms on the first run.

Local JSON evidence and the actual macOS screenshot are retained in
`bench/results/remaining-performance/` (ignored build evidence). That native
journey preceded the X-Trans follow-up and used a separate ad-hoc signed review app;
the personal app was not replaced, and a full distributable release was not
built or published.

The final Python suite ran 1045 tests successfully with four skips: the optional
Git LFS RAW scan fixture suite and three Windows signing checks requiring
PowerShell. All 18 Rust tests passed. Separate real RAW comparisons and native
Metal journeys provide the RAW and macOS runtime evidence described above.

## Capture reuse, cancellation, and edited-photo follow-up

A process-wide RGB16 demosaic cache now serves both resident engines, full-size
viewing, export, and preview sizes that use the same half-size decode. The
512MiB default is a hard retained-byte limit; oversized captures are not retained.
Source identity includes filesystem identity, size, and nanosecond timestamps
captured before opening the file. Concurrent requests share one build. Half-size
pixels remain distinct from full-size pixels rather than substituting a resize.

The cache keys actual LibRaw options. Custom capture temperature/tint, Develop
curves, and learned-denoise strength remain downstream, so those changes reuse
the same demosaic without changing their processing order or pixel values.
The bundled decoder supports cooperative cancellation between X-Trans tiles;
obsolete RAW refinements, waiting decodes, and exports share cancellation through
the transport fallbacks. Older wheels complete normally and discard stale work.

RAW decoding has one priority admission slot, separate from the background GPU
engine. The two existing export workers can prepare the next capture during the
current render without starting two multithreaded demosaics simultaneously.

Camera and print diffusion now support exact viewport rendering. The crop aligns
to each diffusion downsample grid, includes the combined kernel halo, and uses
absolute interpolation coordinates. Optics/healing geometry and unsupported
combinations still retain their established full-frame fallback.

Complex exports use immutable RGB32 shared output before the unchanged Python
finisher, including TIFF, wide-gamut recipes, brush masks, healing, optics, and
watermarks. This preserves existing gamut limitations and finishing semantics.
A bounded film-frame cache reuses mapped pixels, loads existing disk entries,
and writes evicted frames through one bounded background writer. Shutdown
flushes retained frames synchronously; purging invalidates both pending writes
and retained memory. Portable/older workers retain the float TIFF path.

| Follow-up measurement | Before | After |
| --- | ---: | ---: |
| Repeated full 40MP X100VI capture decode | 3958ms | 0.8ms |
| Subsequent custom capture WB on that image | 4256ms | 464ms |
| Four RAW JPEG exports, two workers, median of three runs | 4255ms | 3085ms |
| 24MP camera diffusion, 1200x800 viewport, GPU stage | 929ms full frame | 295ms viewport |
| 24MP camera plus print diffusion, same viewport | 787ms full frame | 172ms viewport |
| 24MP float export transfer plus normalization | 75ms TIFF | 51ms shared |

RAW reuse measurements use the actual app decoder with cache-disabled controls,
two repetitions, and exact hashes. The first uncached demosaic still takes about
four seconds. A real X100VI 1:1-to-export integration kept the cache miss count
at one; preparing the export input took 16.5ms, including shared-memory exchange.
The batch benchmark alternated serial/pipelined runs and verified every delivered
JPEG hash. Diffusion timings use synthetic linear RGB fixtures and measure GPU
work only: all 320 viewport crops matched full-frame float32 pixels bit for bit.
The export transfer measurement excludes GPU rendering, final encoding, and
deferred cache writeback; delivered pixels and ICC profiles matched for seven
finishing recipes, with float-bit identity across all four rotations.
A real RAW export combining brush edits, healing, optics, a watermark, and
Display P3 TIFF output also matched delivered pixels and ICC bytes exactly.
After flushing its film frame to disk and clearing memory, another export
reused the persisted frame without invoking the renderer.

The final source-built cancellation wheel passed 84 real RAW decode comparisons
against stock, across three X-Trans cameras and one Bayer camera, full/half sizes,
alternate settings, and 1/2/4/8/16-thread coverage. Mid-decode X100VI cancellation
stopped 42–70ms after signalling and left no monitor threads. An alternative
OpenMP task-dependency scheduler matched pixels but changed performance by less
than 1% on the two large X-Trans fixtures; it was not adopted. TIFF mmap adoption
was also slower in the measured transport experiment and was not adopted.

A newly built isolated native app passed the complete photo journey: edit
recovery, navigation, sampling, Film on/off, film controls, comparison, actual
size, viewport panning, return to Fit, and completed export. The personal app
was not replaced. Evidence is retained under `bench/results/performance-followup/`
(ignored), including the actual native screenshot, RAW hashes, batch reports,
diffusion and export parity reports, and the rejected scheduler experiment.

The follow-up Python suite passed 1116 tests with the rebuilt decoder and four
platform/optional-fixture skips. All 22 Rust tests passed, and all 54 help
articles passed their source and review checks.

## Export throughput, scan hashing, warm-up, and denoise preload (2026-09-13)

Four background paths changed; the interactive render path is untouched.

- Direct JPEG exports no longer encode inside the engine gate. The resident
  engine quantises the finished frame to the same 8-bit codes its own encoder
  received and publishes them as a packed RGB8 shared-memory surface
  (`export_rgb8`); the Python export worker adopts and unlinks it and encodes
  with libjpeg-turbo while the engine is already free for the next photo or a
  preview. The encoder settings match the engine's `image` crate encoder:
  libjpeg quality scaling of the standard tables, 4:4:4 chroma (the crate's
  doc comment says 4:2:2, its component table says 1×1), standard Huffman
  tables. Delivered bytes differ because the DCT implementations differ, so
  the batch benchmark's cross-run hash check compares runs of the same build
  only. On a 4128×2752 film render both encoders sit at the same distance
  from the 8-bit codes (mean 4.15, p99 15, PSNR 33.5 dB each) and 43.6 dB from
  each other with 0.8% file-size difference; the engine encoder took 290 ms
  per frame inside the gate, libjpeg-turbo 65 ms outside it. Windows, older
  workers, and shared-memory failures keep the in-engine encoder
  (`LIGHTTABLE_DEFERRED_ENCODE=0` forces it). Python-finished recipes (TIFF,
  wide gamut, brushes, heals, optics, watermarks) are unchanged.
- The export pool is sized from the machine: `min(4, max(2, cores // 4))`
  workers (`LIGHTTABLE_EXPORT_WORKERS` overrides). RAW decode keeps its
  single admission slot and GPU work its gate, so extra workers overlap
  decode, encode, ICC, and metadata rather than multiplying demosaics.
- Startup warms the background engine after the preview engine
  (`warm_resident_engines`), including one export-shaped request so the grade
  shader and shared RGB8 path are compiled before the first export
  (`LIGHTTABLE_WARM_BACKGROUND_ENGINE=0` restores the old cold start).
- Scan hashing runs each batch's header and complete BLAKE2b digests on a
  bounded pool (`max(2, min(4, cores // 2))`, serial on Windows where the
  strong signature already reads every byte through a single-entry cache;
  `LIGHTTABLE_SCAN_HASH_WORKERS` overrides). Only reads and digests leave the
  scanning thread: failures are reported in walk order and every catalog
  decision, relink, and write still happens on that thread in the original
  sequence, so the identity invariants in `docs/recovery.md` hold unchanged.
- After the first RAW render completes, macOS schedules a one-time learned
  denoise preload (`schedule_denoise_preload`). It waits, outside the denoise
  pool, until the preview and background gates, the RAW decoder, refinements,
  exports, and any real denoise are idle (bounded by
  `LIGHTTABLE_DENOISE_PRELOAD_WAIT`, 300 s), then runs one blank tile through
  the Core ML helper inside `DENOISE_POOL` so a user denoise started meanwhile
  queues behind the shared compile instead of compiling twice. It is skipped
  in Safe Mode, off-platform, without a model, during updates, or with
  `LIGHTTABLE_DENOISE_PRELOAD=0`; a real denoise cancels a preload still
  waiting, and shutdown cancels a running one. This Mac has the helper but no
  bundled model, so the compile-time saving is not measured here; the state
  machine is covered by `tests/test_denoise_preload.py`.

Measurements: same Apple-silicon Mac (18 cores), four CC0 RAW originals
(X-T30 III RAF, EOS M200 CR3, G100D RW2, Mavic 3 Pro DNG) in an isolated
catalog and cache, Film on, JPEG quality 92, `bench/export_pipeline_benchmark.py`
with `--repeats 3`; the first batch pays the cold RAW decodes and the next two
reuse the demosaic cache. "Before" runs the same build with the three switches
set to the previous behaviour (in-engine encode, two workers, cold background
engine). Scan numbers use `bench/scan_benchmark.py` on 300 distinct noise JPEGs
(676 MB, warm page cache), median of three fresh catalogs.

| Measurement | Before | After |
| --- | ---: | ---: |
| First export after startup (X-Trans, cold decode) | 12262 ms | 9694 ms |
| First export, engine phase | 3105 ms | 1528 ms |
| Four-RAW batch, warm demosaic cache, two workers | 6065 / 5790 ms | 4125 / 4316 ms |
| Four-RAW batch, warm demosaic cache, sized pool (4 workers) | — | 4007 / 4789 ms |
| Four-RAW batch, cold decodes | 7621 ms | 4313 ms |
| Interactive preview during the cold batch | 627 ms | 192 ms |
| Scan, new files, with metadata read | 491 photos/s | 1403 photos/s |
| Scan, new files, hashing only | 543 photos/s | 1717 photos/s |
| Four-RAW batch, Film off (Python worker), warm, two vs four workers | 8525 / 7020 ms | 5454 / 6263 ms |

With Film on the four-photo batch is now bound by the serialised GPU render,
so the sized pool matches two workers on it; the extra workers pay off on the
Python worker path, where each export is its own process. Every batch left the
preview probe complete and every delivered JPEG hash identical across repeats
and across the two-worker and sized-pool runs. JSON evidence is retained under
`bench/results/export-perf-2026-09-13/` (ignored).

## Windows/Linux interactive preview transport (2026-09-13)

Windows and Linux (and a plain browser against the review server: neither
has the macOS WKWebView bridge `NATIVE_PREVIEW` requires) presented every
film-rendered preview frame as a JPEG: the server encoded it, the browser's
`<img>` decoded it, and `GradeRenderer.setImage` uploaded the decoded RGB
into a WebGL texture. The resident engine already produces a packed RGBA8
surface for the Metal path (`server.py`'s `native_surface`/`FLRA` header);
this change lets a non-Metal client ask for that same surface instead of a
JPEG and upload it straight into WebGL with no image codec at all.

- `render_preview`/`_render_preview` in `server.py` take a new `raw` request
  flag, independent of `native` (which still means "bake for the Metal
  presenter"). `want_surface = native or raw` now gates whether the resident
  RGBA8 surface is produced; `need_jpg = not want_surface` skips the eager
  JPEG encode for a raw request the same way it already did for a native
  one. The viewport-render gate (`viewport is not None and (engine != "rs"
  or not want_surface)`) now accepts a raw-transport request too, so the
  server can already serve the same 1:1 viewport tiles the Metal path uses
  to a non-Metal client (see "Not done" below for why the client does not
  ask for them yet). JPEG stays the transport for the non-film source-image
  passthrough, thumbnails, and edited/baked renditions (`/api/render/png`
  already serves those losslessly when optics/heals are baked).
- `web/gl.js` adds `GradeRenderer.setImageFromRaw(pixels, width, height, …)`,
  a `texImage2D` upload from a typed array, alongside the existing
  `setImage(img, …)` that takes an `<img>` element.
- `web/app.js`'s `doRender`/`prefetchImage` send `raw: !nativePreviewActive()`.
  `setBaseImage` fetches `render.native.url` with `fetch()` +
  `arrayBuffer()`, validates the `FLRA` header, and calls
  `setImageFromRaw`; a fetch failure or malformed header falls back to
  `render.img` (JPEG) if the server also produced one, so a raw request
  never turns a working preview into a broken one.
- Adaptive/auto preview width (`requestedPreviewWidth`/`automaticPreviewWidth`
  in `web/view-performance.js`) was already platform-agnostic — it does not
  gate on `NATIVE_PREVIEW` — so no change was needed there.

Measurements: `bench/preview_transport_benchmark.py` starts an isolated
server against a demo asset and issues distinct-per-iteration film renders
(to defeat the render cache, matching a real slider drag) through the
legacy JPEG path and the new raw path, 8 iterations each. This Mac is
darwin, so the server process still takes the shared-memory branch for the
resident engine's own IPC with Python (`native_shared` in `render_rust`);
that is orthogonal to this measurement, which is the HTTP transport from
server to client either way, and would be exercised identically on real
Windows/Linux hardware, which this environment does not have (see "Not
done"). Raw numbers: `bench/results/preview-transport-2026-09-13.json`.

| Width | Path | Server render+encode | Fetch | Client decode/parse | End to end | Payload |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1100 px | JPEG | 66.0 ms | 0.74 ms | 4.96 ms | 71.6 ms | 518 KB |
| 1100 px | raw | 54.3 ms | 2.86 ms | 0.003 ms | 58.0 ms | 5.64 MB |
| 2200 px | JPEG | 156.3 ms | 2.95 ms | 16.3 ms | 188.6 ms | 1.81 MB |
| 2200 px | raw | 113.9 ms | 5.84 ms | 0.003 ms | 119.2 ms | 18.6 MB |

(medians of 8 iterations; "server render+encode" also drops because the raw
path skips the JPEG encode the legacy path paid for on every render, not
only because of the transport change.) At 2200 px, end-to-end time drops by
roughly 37%; the client-side JPEG-decode cost (16 ms, comparable to a frame
budget at 60 Hz) is eliminated outright. The raw payload is roughly 10-11x
larger in bytes (uncompressed RGBA8 vs. quality-88 JPEG); over the loopback
connection the actual app uses (the server and the WebView2/WebKitGTK host
are on the same machine, exactly as tested here) that costs low single-digit
milliseconds, not the bandwidth-bound cost it would be over a real network.

Not done, and why:
- **Client-side viewport tiling for the raw/WebGL path.** The server accepts
  a viewport request from a raw-transport client now, but presenting a
  partial-frame tile also needs the Metal presenter's virtual-canvas
  compositing (an oversized canvas, an offset texture write, panning without
  re-uploading the whole frame — see `S.nativeViewport`/
  `nativeViewportPayload()`/`scheduleNativeViewportLayout()`). Enabling
  `viewportRegionEnabled()` for the raw path without that compositing would
  request a tile and then draw only that tile stretched to fill the canvas,
  which is a visible regression at 100% zoom pan — so it stays gated to
  `nativePreviewActive()` until that compositing exists. This is a
  self-contained follow-up independent of the transport change above.
- **A GPU-resident presenter (no WebGL texture upload at all) for Windows
  and Linux.** Design and estimate in
  [`gpu-preview-design.md`](gpu-preview-design.md); no spike is committed
  because validating its biggest risk (WebView2/WebKitGTK child-window
  compositing) needs real Windows/Linux hardware this environment does not
  have.
- **Raw transport for the non-film source-image passthrough** (viewing an
  unrendered JPEG/HEIC/RAW-neutral preview before Film is turned on).
  Left as JPEG: it is not the hot path this change targets (no per-frame
  film render or JPEG encode happens there; the JPEG is already a cached
  file), and building a raw variant would mean decoding that cached JPEG
  into a native surface on the server for no per-request encode saving.
- **Real Windows/Linux hardware verification.** Every measurement and test
  above ran on macOS with the macOS-only Metal path forced off by simply
  not setting `native: true`/`NATIVE_PREVIEW`, which is exactly the
  condition `windows-shell` runs under; the actual WebView2/WebKitGTK
  `fetch()`/`texImage2D` behavior, and real-network (not loopback) HTTP
  timing, are unverified on real hardware.

## Repeatable checks

```sh
python -m unittest discover -s tests -q
cargo test --locked --manifest-path rust-engine/Cargo.toml
python rust-engine/bench_resident_cache.py --data engine/data --width 2200
python rust-engine/bench_resident_cache.py --data engine/data --width 128 --fresh-pipeline-check
python rust-engine/bench_viewport.py --data engine/data --width 2200
python rust-engine/bench_native_surface.py --data engine/data --width 2200
python scripts/benchmark-raw-reuse.py /path/to/capture.RAF --repeats 2
python rust-engine/bench_export_surface.py --data engine/data --width 2200
python bench/export_pipeline_benchmark.py --photos /path/to/raw-folder --count 4 --repeats 3 --output bench/results/export.json
python bench/scan_benchmark.py --count 300 --repeats 3 --fixtures /path/to/fixtures --output bench/results/scan.json
python scripts/native-app-smoke.py --app /path/to/LightTable.app --layer pr
python bench/preview_transport_benchmark.py --iterations 8 --widths 1100,2200
```

The native checks need real Metal/shared-memory access and a connected macOS
window session. A headless or sandbox-denied run cannot establish native proof.
