# Processing correctness

Run from the repository with the normal Python runtime, reference profiles,
Rust/Cargo, Node, and Playwright installed:

```sh
.venv/bin/python scripts/check-processing.py --film-gpu
```

This builds the current Rust source in `build/processing-cargo`, generates
synthetic fixtures, executes independent references, and checks the production
engines, exports, browser application, and actual Metal display frames. It uses
an isolated photo library and runtime; it does not edit a personal catalog or
replace the installed application.

The command exits nonzero for a numerical mismatch, missing runtime, unavailable
GPU/display, timeout, or incomplete suite. It never converts a missing native
display into a passing skip. All requested suites must finish before the report
can say PASS. On macOS, run outside restrictive process sandboxes in an unlocked
graphical login with Metal available.

## Evidence

`build/processing-correctness/` contains:

- `report.md`: suite outcomes and failures.
- `report.json`: source hashes, revision, dirty-tree state, individual metrics,
  tolerances, runtime identity, and artifact paths.
- `junit.xml`: CI results; blocked suites are failures.
- Per-case reference, actual, and amplified difference PNGs for RGB comparisons.
- Floating-point stage arrays, requests, logs, grain statistics, and submitted
  native drawable records. Film arrays retain their physical units.
- Full browser-app screenshots and separate pixel-scored canvas captures.

Never bless a failing output by updating a screenshot golden or increasing a
tolerance until it passes. Locate the first divergent stage. If a reference
contract changes intentionally, explain the model change and derive a new
tolerance before comparing results.

## What each suite establishes

| Suite | Implementation under test | Independent reference and coverage |
| --- | --- | --- |
| `film` | Freshly built Rust resident engine, CPU diagnostic taps | Python spectral runtime at film exposure, developed film density, print exposure, developed print density, and scanned RGB; separate NumPy exposure and measured characteristic-curve checks. Isolates exposure, development gamma, dye couplers, halation, diffusion, print exposure/preflash/filters, scanner blur/sharpen/glare, reversal, and B&W development times. |
| `film --film-gpu` | Actual WGPU film output | Verified CPU output for deterministic recipes; backend identity must say WGPU. A CPU fallback fails. Intermediate GPU buffers are not exposed, so GPU stage-by-stage equivalence is not claimed. |
| `export` | Actual `render_cli.py` subprocesses and current Rust resident export | Independent NumPy sRGB, linear-light exposure, rotation/crop and Lanczos equations; RGB16 precision and ICC profiles. Python grading/masks independently check Rust postprocessing from the same float film base. |
| `webgl` | Production `web/gl.js` in Chromium | Python CPU grading for individual positive/negative controls, detail/noise/sharpening, all curve channels and HSL bands, Point Color, tonal color grading and combined operation order. Scores synchronous framebuffer output and composited canvas screenshots, with navigation, portrait geometry, and compare restoration. |
| `app` | Complete server and browser application | Real UI commands and slider/save/render paths, Film on/off, print exposure, and portrait/landscape navigation. Requires the intended saved recipe and matching ready photo. Scores the actual frame against the CLI render endpoint with an explicit reference recipe, and the visible scaled canvas against its rendered pixels. |
| `native` | Production `NativePreview.swift` and `NativePreview.metal` in a real AppKit window | Python grading against the actual MTKView drawable submitted for display, including image edges. Exercises PNG/FLRA input, all shared grade cases, cached A/B/A navigation, compare sweeps/restoration, and portrait/landscape sizing. Requires GPU completion and drawable presentation. |

The native helper compiles the production renderer and shader; it is not a
replacement for the full native product journey. Run the existing
`scripts/run-product-journey.sh pr` as well when changing native bridge wiring,
window layout, or packaging. The complete browser application is exercised by
the `app` suite; the Metal helper isolates native rendering correctness.

## Reference contracts and tolerances

Film stage errors use log10 exposure or optical density. Scanned RGB and
preview tolerances are measured as 8-bit code values, even when input/output
arrays are float. The exact limits are stored alongside every result; RGB
preview defaults are mean <= 0.75, p95 <= 2, maximum <= 5. Maximum error matters:
a few badly rendered border pixels must not disappear into an image-wide mean.

RGB8 encoding has a half-code quantization bound. Lossless RGB16 exports use
limits near one 16-bit code. GPU and curve texture rounding need separate
documented limits from float CPU stages. Comparisons reject NaN, infinity,
empty output, wrong dimensions, wrong channels, and missing artifacts.

Grain is stochastic. Different samplers cannot be expected to generate the same
individual particles. The suite instead requires exact repeatability within an
engine and tests flat-field means and variance against analytical
Poisson/binomial expectations. With GPU enabled it also checks CPU/GPU scan
noise moments. Statistical regions exclude the known blur support between
flat fields; ordinary preview image comparisons retain image edges.

Browser element screenshots include fractional CSS boundary coverage. The app
check independently scores the framebuffer against the CLI, then compares the
visible app canvas with a separate reference canvas containing those readback
bytes at identical DOM bounds. Both use the browser's compositor, including its
fractional scaling, rather than approximating it with an integer-sized resize.
The reference page is labeled as a reference artifact, never an app screenshot.
No image registration or border trimming is used.

These are software-equivalence tests for the declared model and fixtures, not
proof that a simulation matches a particular physical negative, chemical bath,
scanner, printer, or monitor. The two spectral implementations share measured
profile data, so agreement cannot establish that the profile data are correct.
Real-camera RAW demosaicing/WB, all stock combinations, operating-system display
calibration, and stochastic perceptual quality need additional fixtures or
real-device comparisons.

Regression coverage includes Vision3 200T/500T with automatic metering on/off,
flat-field Texture/Clarity after global exposure or an earlier mask, real-photo
full and partial masks, Heal/Remove, sequential Clone, sharpening after retouch,
and manual optical boundaries. Native edit cases exercise the server's bake
decision before submitting its surface and edit payload to Metal. Browser app
cases additionally cover an exposure slider while a detail mask is active and
clearing the last detail mask. Ordinary grade-only controls retain their live
GPU path.

Retouching uses the exact ordered CPU base correction before GPU grading.
Masks with Texture or Clarity bake the complete global-grade/local-mask stack,
because a single fragment pass cannot sample the image produced by earlier
adjustments. These edits can increase preview latency; the expensive film base
is reused. Corrected surfaces are RGBA8 for Metal and lossless PNG for browser
presentation. Soft proof, compare, and reference overlays remain display steps.

Active optical diffusion now uses the exact CPU PSF convolution on both render
backends. Other supported stages remain on the selected GPU backend, but the
diffusion recipe renders the full frame before viewport trimming. This removes
the previous preview/export halo mismatch and can increase latency when the
effect is enabled. The default diffusion strength is zero.

## CI and focused runs

Pull requests and main run the film CPU, export, WebGL, and complete browser
app gates in `.github/workflows/python-tests.yml`, and upload failure artifacts.
Hosted macOS runners do not stand in for a verified physical Metal/display
session. The full local command additionally requires native and film GPU proof.

```sh
# Hosted/CPU-compatible subset (explicitly omits native and film GPU proof).
python scripts/check-processing.py --suites film,export,webgl,app

# Focus on a native shader change.
python scripts/check-processing.py --suites native --output build/native-pixels

# Scorer and reference self-tests; real engine comparisons are separate gates.
python -m unittest discover -s tests -p 'test_processing_*.py' -v
```

Application GPU finishing and merge stages have additional device tests. Build
the current resident engine into `engine/lighttable-engine`, then run these in
an unlocked graphical session with GPU access:

```sh
LIGHTTABLE_TEST_GPU=1 python -m unittest discover -s tests -p 'test_gpu_grade.py' -v
LIGHTTABLE_TEST_GPU=1 python -m unittest discover -s tests -p 'test_merge_acceleration.py' -v
```

These compare float32 output against the existing CPU algorithms, including
odd dimensions, image boundaries, tiled filter halos, local-mask composition,
and direct resident export ordering. Eligible device cases fail if they silently
fall back. Ordinary runs still exercise the explicit unavailable-device fallback.
Export finishing retains CPU chromatic-aberration geometry and precision-sensitive
Point Color luminance-uniformity recipes; basic-only grades use the existing
Numba path when available because worker transport can cost more than it saves.

`bench/gpu_finish_benchmark.py` measures full-resolution finishing including
worker transport, and can compare encoded 16-bit TIFFs with `--export-check`.
`bench/merge_benchmark.py` measures complete merges and their phases with fixed
registration randomness. Its `--baseline-source` option accepts the original
`merge_workflow.py` for a baseline/new-CPU/GPU comparison. Use fresh processes
to measure cold startup separately, and report source dimensions, recipe,
repetition count, output error and device identity alongside speed.

Install a reproducible browser test runtime if one is not already configured:

```sh
npm install --prefix build/processing-browser --no-save playwright@1.62.1
build/processing-browser/node_modules/.bin/playwright install chromium
export LIGHTTABLE_PLAYWRIGHT_MODULE="$PWD/build/processing-browser/node_modules/playwright/index.mjs"
```

`LIGHTTABLE_BROWSER_EXECUTABLE` optionally selects the test Chromium executable;
otherwise Playwright's installed browser is used. Pinned film runtime/data
preparation is shared with the existing Python CI setup. The untracked legacy
`engine/spektrafilm-rs` binary is deliberately not used as the film gate's
implementation under test.
