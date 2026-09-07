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
- macOS packaging adds a reproducible, relocatable OpenMP decoder for Bayer
  and X-Trans. X-Trans tiles run in dependency order to retain exact serial
pixels; older wheels lacking that schedule retain stock decoding. See
  [RAW runtime details](packaging/README-rawpy.md) for the source pins, macOS 13
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
python scripts/native-app-smoke.py --app /path/to/LightTable.app --layer pr
```

The native checks need real Metal/shared-memory access and a connected macOS
window session. A headless or sandbox-denied run cannot establish native proof.
