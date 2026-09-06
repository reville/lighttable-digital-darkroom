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
  a full-frame fallback while the next tile arrives. Browser previews, diffusion,
  and unsupported edit combinations retain full-frame behavior.
- Scan-time source thumbnails use one bounded, cancellable background worker,
  yielding to interaction. Measured RAW thumbnails retain rawpy: ImageIO was
  slower on the tested NEF and CR2 files.
- macOS packaging adds a reproducible, relocatable OpenMP decoder for Bayer
  while preserving the stock decoder for X-Trans. See
  [RAW runtime details](packaging/README-rawpy.md) for the source pins, macOS 13
  dependency checks, and the unresolved upstream X-Trans limitation.

## Measured verification

Measurements below are local runs on the same Apple-silicon Mac. Engine tests
use synthetic textured input to make exact comparisons repeatable; cold input
and application journeys use real-photo fixtures. They are not input-to-photon
latency claims.

| Measurement | Before | After |
| --- | ---: | ---: |
| Cold NEF input preparation plus Match, 1100px | 174ms | 72ms |
| Cold 24MP JPEG preview preparation | 573ms | 47ms |
| 24MP Bayer DNG decode, stock versus 8-thread decoder | 741ms | 437ms |
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

The real macOS photo journey verifies native presentation, navigation, edit
recovery, film controls, completed actual-size tiles, panning, return to Fit,
and export in a disposable catalog. Export integration also verifies a second
export uses resident input and that a preview completes during another export.
The final native journey's uncached viewport film adjustment completed in 41ms
including the request and Metal presentation acknowledgement. The separate RAW
integration run completed a preview in 27ms during a 507ms export; repeated
export avoided decoding and completed in 460ms versus 1363ms on the first run.

Local JSON evidence and the actual macOS screenshot are retained in
`bench/results/remaining-performance/` (ignored build evidence). The native
journey used a separate ad-hoc signed review app with the new decoder runtime;
the personal app was not replaced, and a full distributable release was not
built or published.

The final Python suite ran 1044 tests successfully with four skips: the optional
Git LFS RAW scan fixture suite and three Windows signing checks requiring
PowerShell. All 18 Rust tests passed. Separate real RAW comparisons and native
Metal journeys provide the RAW and macOS runtime evidence described above.

## Repeatable checks

```sh
python -m unittest discover -s tests -q
cargo test --locked --manifest-path rust-engine/Cargo.toml
python rust-engine/bench_resident_cache.py --data engine/data --width 2200
python rust-engine/bench_resident_cache.py --data engine/data --width 128 --fresh-pipeline-check
python rust-engine/bench_viewport.py --data engine/data --width 2200
python rust-engine/bench_native_surface.py --data engine/data --width 2200
python scripts/native-app-smoke.py --app /path/to/LightTable.app --layer pr
```

The native checks need real Metal/shared-memory access and a connected macOS
window session. A headless or sandbox-denied run cannot establish native proof.
