# Remaining gaps milestone

Status: **implemented locally; not released**, 2026-09-03. Companion to
[docs/roadmap/workflow.md](workflow.md) (migration, catalog, culling: built)
and [docs/roadmap/develop.md](develop.md) (develop-tool parity: built).

All eight numbered phases are now in the working tree. The accepted Windows
denoise/person-part follow-ons and optional local-mask preset conversion remain
out of scope, exactly as recorded below. The denoise package is generated and
ignored rather than committed; both local and release builds bundle it when
present. Current proof is recorded at the end of this document.

With the library and the migration path in place, this milestone covers the
eight things a photographer arriving from a catalog-based editor still finds
missing when they compare LightTable against the field: a denoiser that
actually runs, more and better masks, a way out to a pixel editor and back,
focus stacking, keyboard-driven slider control, portrait masks, a skin-tone
control, and a watched folder for tethered and hot-folder shooting.

Two of the eight are finishing work on plumbing that already exists (the
Enhance model, the mask model). The rest are new. None of them add a hidden
transform: every new operation is a named, inspectable parameter that renders
the same way in the WebGL preview, the Metal preview, and the numpy export.

Line numbers were read on 2026-09-03. Treat them as approximate and search by
function name.

## Scope

| # | Gap | Where it lands | Days |
| --- | --- | --- | --- |
| 1 | A denoise model that ships and runs | Phase 1 | 5–6, plus 2 for Windows |
| 2 | Mask cap of four; no colour range | Phase 2 | 5 |
| 3 | External pixel-editor round trip | Phase 3 | 5 |
| 4 | Focus stacking | Phase 4 | 3 |
| 5 | Hold-a-key slider control | Phase 5 | 2 |
| 6 | People masks | Phase 6 | 6 |
| 7 | Skin-tone uniformity | Phase 7 | 2 |
| 8 | Watched folder | Phase 8 | 4 |

About 32 engineering days for one person who knows the codebase, before the
Windows follow-ons.

## Ordering and tracks

- **Track A (pixels):** 1 → 2 → 6 → 7. Phase 6 needs the bigger mask cap and
  the higher-resolution bitmap from Phase 2; Phase 7 is independent but shares
  the Point Color shader code with nothing else, so it slots in wherever.
- **Track B (workflow):** 5 → 8 → 3 → 4. Phases 3 and 8 share one new
  helper, `catalog_scan.register_file()`, and Phase 3's return leg rides on
  Phase 8's watcher, so 8 comes first. Phase 4 is self-contained.

Phase 1 is the one with an external dependency (a model download and a
conversion toolchain), so it starts first on Track A and its evaluation
harness (1.4) is the gate for shipping it at all.

## Rules that apply to every item

The rules from the workflow roadmap still apply. Restated with what changed:

- **Three renderers, one contract.** A pixel operation now lives in three
  places: `grade.py` / `edits.py` for export and `render_cli.py`,
  `web/gl.js` for the WebGL preview, and `app/NativePreview.metal` for the
  Metal preview. Every new operation lands in all three in the same change,
  with a `web/paritytest.html` case and a numpy test. The existing rule that
  `finish_export()` and `render_cli.py` share a test still holds.
- **Windows allowlist.** Every new module goes into
  `scripts/windows/build-release.ps1` (lines 74–100); the
  `WindowsAllowlistContractTests` already fail the build otherwise.
- **Cache identity.** Anything that changes decoded pixels goes into
  `color_pipeline.raw_decode_fingerprint()` and bumps its versioned token
  (currently `linear-prophoto-v5`). Anything that changes rendered output
  bumps `RENDER_CACHE_VERSION` (`server.py` line 1944).
- **Refuse, never pretend.** A capability that is unavailable on a platform
  reports why and returns an error. There is no identity fallback anywhere in
  this milestone, for the reason recorded in the napkin: a pass-through is
  indistinguishable from a model that did nothing.
- **Worker cleanup.** Every new background worker (the watcher in Phase 8, the
  register step Phases 3 and 8 share, the denoise job in Phase 1) closes its
  thread-local catalog connection when it ends, per the napkin's data-integrity
  rule; nothing relies on thread teardown to release WAL handles.
- **Naming.** Third-party products are named only in the import-compatibility
  notes (2.3), per the napkin directive. This document describes the
  competitors' features generically.
- **Tests.** `unittest`, inline fixtures, `tempfile.TemporaryDirectory()`. The
  Python suite runs in `.github/workflows/python-tests.yml` on macOS; anything
  that needs a Swift helper or a model is skipped there with a named reason and
  exercised by `scripts/build-release.sh`'s smoke step instead.
- **Phase exit.** Tests green, `bench/benchmark.py` and
  `bench/browser_benchmark.mjs` before and after on the Mac benchmark machine,
  Windows runtime smoke, README and this document updated, napkin updated.

## Phase 1 — Denoise that ships (built)

At planning time, everything except the model existed: `enhance_workflow.py` had discovery,
capability reporting, request clamping, 512 px overlapped tiling with
raised-cosine feathering, the 16-bit master, the manifest, and the
`denoise_fingerprint()` token. The model choice was made on 2026-09-03 and
recorded in the workflow roadmap: **SCUNet** (`scunet_color_real_psnr`),
Apache-2.0 code with MIT weights, blind (no noise-level input), trained on
synthetic degradations so it generalises across the 1275 camera models the app
accepts. Super-resolution stays off; a GAN upscaler invents detail and does
not belong in an application that labels every assumption modeled or measured.

**Done in parallel on 2026-09-03 (commit `6e517f3`):** another session
has already done most of 1.1 and 1.2 below. That commit holds
`scripts/fetch-models.py`, `scripts/convert-models.py`,
`scripts/models/scunet_traceable.py`, `scripts/models/requirements-convert.txt`,
and a rewritten `film_lab_ai/enhance_helper.swift` with matching changes in
`enhance_workflow.py`. The checklist below is retained as the implementation
spec; its remaining items are now complete.

### 1.1 Fetch and convert (built)

Exists:

- `scripts/fetch-models.py` pins the SHA-256 of the PSNR weights (and the
  GAN weights behind `--gan`, not recommended), the network source at
  SCUNet revision `52e440a`, and both licence texts, writes `PROVENANCE.txt`,
  and downloads into `scripts/models/download/`. Weights are never committed.
- `scripts/convert-models.py` loads the weights key-for-key, patches the
  network through `scunet_traceable.py` (native reshape for `einops`, a
  precomputed relative-position table, an additive `-inf` window mask),
  **refuses to convert until the patched network is bit-identical to the
  published one**, traces at a fixed `1×3×512×512`, and writes an
  `.mlpackage`. Its own pinned environment is
  `scripts/models/requirements-convert.txt` (torch 2.7.0, coremltools 9.0,
  numpy 1.26.4 because coremltools rejects NumPy 2); nothing from it enters
  the runtime lock.
- The fp16 package is 78 MB. The converter checks the traced network against
  the published PyTorch network before conversion and prints backend-specific
  Core ML error and timing rather than treating an older machine's numbers as
  universal. fp16 is the default; the release smoke validates its real output.

Completed after that commit:

- `convert-models.py` now writes `models.json` beside the package (`version`,
  `license`, `source`, `sha256`, `input: 512`), which `model_info()` reads for
  capability and provenance display.
- Distribution. `model_root()` checks a user override first and the bundled
  `Contents/Resources/models` package second. `build-app.sh` copies it when
  present and `scripts/build-release.sh` requires it, including both licences
  and the package-digest metadata.
- CI: the model is not needed for the Python suite; the release script's
  smoke step runs `--probe` and one batch against the installed package.

### 1.2 Inference (built)

Exists:

- The helper and the runner exchange **bare float32 planar tiles**
  (`FLT0` header, `write_tile_file()` / `read_tile_file()`), not TIFF. Core
  Image colour-manages anything it recognises, so a TIFF written by numpy
  came back through an sRGB-to-linear conversion nobody asked for, measured
  as a 17 dB *loss* against a known-clean reference. The exchange now carries
  no colour information and converts nothing.
- `--denoise-batch <manifest.tsv>` runs every tile of an image in one
  process. Loading the compiled model costs about thirty seconds the first
  time and a second afterwards, against a tenth of a second per tile; one
  process per tile had turned a 24 MP image into an hour of loading.
  `run_model()` uses `helper_batch_runner()` for denoise.
- The compiled `.mlmodelc` is cached in the user's Caches directory under the
  package digest, so inference never mutates a signed application bundle.
- Strength blends against the original inside the helper, in both the
  single-tile and batch paths, so the control means the same thing whether
  or not a model exposes one of its own.

Completed after that commit:

- **Edge tiles.** `tile_image()` clips at the image edge while the converted
  graph is fixed at 512×512, so a short tile previously failed.
  `helper_batch_runner()` and `helper_runner()` reflect-pad a short tile to
  `params["tile"]` before writing it and crop the result back, in Python
  where a stub runner tests it:
  `test_short_edge_tiles_are_padded_and_cropped_back`.
- **Progress and cancel.** Batch mode is silent until it finishes. The helper
  prints `{"progress": n, "total": m}` every eight tiles; the runner moves
  from `subprocess.run` to `Popen` and reads stdout incrementally into a
  status dict, and a cancel kills the helper. 1.3 needs both.
- A macOS-only test, skipped when `--probe` reports no model, runs one flat
  noisy tile through the real helper and asserts the standard deviation
  falls by at least half; and a stub-runner test that a strength of zero is
  byte-identical to the input.
- The single-tile `--denoise` path stays for tests and the upscale path is
  left untouched and off.

### 1.3 Where it sits in the pipeline (built)

The module already anticipates a *develop setting*, not only a separate
Enhance job: `denoise_fingerprint()` reads `learnedDenoise` and
`learnedDenoiseStrength`. A switcher expects denoise inside Develop, so that
is the primary path; the existing Enhance job that writes a new master into
`LightTable Enhanced` stays as the batch form.

- `film_pipeline.RAW_DEVELOP_KEYS` and `DEFAULT_PARAMS` gain
  `learned_denoise` (bool) and `learned_denoise_strength` (0..1, default
  0.6). `color_pipeline.raw_decode_fingerprint()` folds in
  `enhance_workflow.denoise_fingerprint(params)` and moves the token to
  `linear-prophoto-v5`, which invalidates every neutral decode once and goes
  in the release notes.
- The operation runs inside `color_pipeline.decode_raw()` after demosaic and
  highlight recovery and before the linear ProPhoto result is written, so
  `neutral_tiff_for()` and `tiff_for()` cache the denoised decode under the
  new fingerprint and preview and export are the same pixels by construction.
  `decode_raw_draft()` is never denoised: the interactive draft paints first,
  as the napkin requires, and the accurate decode lands when it lands.
- **Encoding was resolved by measurement.** The model was trained on
  gamma-encoded RGB while the pipeline holds linear ProPhoto. The 1.4 gate
  selected an sRGB transfer-function round trip with primaries untouched,
  followed by a low-frequency colour correction in linear light; the module
  names that approximation explicitly.
- The 512-edge evaluation measured 1.08 s/MP warm end to end on the benchmark
  Mac; the first new model identity also pays Core ML compilation. It is
  therefore not a live
  slider. The RAW section of the Develop panel gets a "Denoise" block:
  strength slider, an Apply button, and a progress line fed by a `DENOISE`
  status dict in `server.py` shaped like `MERGE` (running, progress, total,
  error). While the job runs the preview shows the un-denoised render with
  a "Denoising… 40 %" badge on the image; when the fingerprinted TIFF is
  published the normal cache path swaps it in. The worker closes its catalog
  connection when it ends.
- Tests: `test_learned_denoise_changes_the_decode_fingerprint` and, with a
  stub runner injected through `run_model(runner=...)`,
  `test_decode_runs_the_runner_once_per_tile_and_caches_the_result`.

### 1.4 Evaluation harness, the gate (built)

The 17 dB finding in 1.2 came from a measurement that is not in the
repository. It becomes `bench/denoise_eval.py`, writing to
`bench/results/denoise/`:

1. Synthetic: decode the low-ISO files in `raw-test/`, add Poisson–Gaussian
   noise at three levels, run the model at strength 1, and report PSNR and
   SSIM against the clean decode, next to the existing `raw_sensor_denoise`
   full mode as the baseline, for both encodings from 1.3.
2. Real: the high-ISO files in `raw-test/` as a contact sheet of 1:1 crops at
   strengths 0.3, 0.6, and 1.0, for a human judgement.
3. Colour: mean ΔE76 between clean and denoised on flat patches, using the
   measurement code in `calibration/`, to catch an encoding choice shifting
   hue.
4. Timing per megapixel on the benchmark Mac, first run and warm.

Ship criteria: at least 1.5 dB over the FBDD baseline at the middle synthetic
level, mean ΔE76 under 1.0 on the flat patches, and nothing in the contact
sheet that reads as invented texture.

The 2026-09-03 three-file run selected gamma encoding and passed: +12.65 dB at
the middle level, mean flat-patch ΔE76 0.341, 1.08 s/MP warm. The three-row
high-ISO contact sheet showed progressively stronger smoothing without invented
structure. Results stay local under ignored `bench/results/denoise/` because
the source RAWs are local fixtures.

### 1.5 Windows (accepted follow-on)

Core ML does not exist there. Export the same patched network to ONNX at the
same fixed shape from `convert-models.py`, pin `onnxruntime-directml` in a
Windows-only requirements file, and add an `onnx_runner` in
`enhance_workflow.py` chosen when `sys.platform == "win32"`, so
`SUPPORTED_PLATFORMS` grows without a new module. The harness runs once on the
Windows box to confirm the two runtimes agree within 0.1 dB.

## Phase 2 — Sixteen masks and a colour range (built)

### 2.1 Raise the cap to sixteen (3 days)

Four masks is the tightest limit in the field. The constraint is not the
model (`apply_masks()` loops) but the packing: one RGBA texture holds four
geometric masks, and the shader uniforms are one `vec4` per parameter.

- `edits.MAX_MASKS` and `editor-panels.MAX_MASKS` go from 4 to 16.
  `MAX_MASK_COMPONENTS` (12) and `MAX_STROKES` (64) stay. Add a per-image
  cap of 20 000 brush points across all masks, with a toast when a stroke
  would exceed it, so the state blob stays bounded.
- **Texture atlas, not more textures.** `buildMaskTexture()` (`web/app.js`
  line 792) keeps one RGBA texture but stacks `ceil(n / 4)` tiles vertically:
  mask `m` lives in tile `m >> 2`, channel `m & 3`. `maskTextureSize()` is
  unchanged per tile. This costs no extra texture units and works the same in
  Metal.
- WebGL: `u_localOn`, `u_localOpacity`, `u_localLumaLow`, `u_localLumaHigh`
  and the seven grade uniforms become `vec4[4]` arrays indexed by tile;
  `main()` loops `for (int t = 0; t < 4; t++)` with a `u_maskTiles` early
  break, sampling `texture2D(u_masks, vec2(uv.x, (uv.y + float(t)) /
  u_maskTiles))`. GLSL ES 1.0 permits constant loop bounds and loop-index
  uniform array access. `applyLocal()` itself is unchanged.
- Metal: `GradeUniforms` (`app/NativePreview.metal` line 9) replaces
  `localTone0..3`, `localColor0..3`, `localRange0..3` with `localTone[16]`,
  `localColor[16]`, `localRange[16]`; `updateMasks()` in
  `app/NativePreview.swift` (line 229) accepts `channel` in `0..<16` with tile
  addressing and allows a texture height of `4 × 1024`;
  `nativeMaskChannelPayload()` sends `tile` beside `channel`.
- UI: `maskList` scrolls; the add buttons disable at sixteen exactly as they
  do at four today (`button.disabled = S.masks.length >= MAX_MASKS`).
- Cost: sixteen `applyLocal()` calls per pixel, each early-exiting on
  `enabled < 0.5 || geometric <= 0.0`, plus four texture fetches instead of
  one. Run `bench/browser_benchmark.mjs` with four masks before and after;
  budget is under 10 % frame-time regression at four masks, and it must stay
  interactive at sixteen.
- Tests: `test_mask_schema_is_bounded_and_normalized` extends to sixteen and
  refuses the seventeenth; a parity case in `paritytest.html` with masks in
  tiles 0 and 3.

### 2.2 Colour range (2 days)

Luminance range exists (`_luminance_weight()`, `applyLocal()`'s
`low`/`high`); colour range does not, and it is how the incumbents' users
refine a Subject or Sky mask down to one thing.

- Schema per mask, beside `lumaLow`/`lumaHigh`: `colorHue` (null, or
  0..360), `colorRange` (2..90, default 30), `colorAmount` (0..1, default 1).
  Cleaned in `clean_masks()` and `normalizeMasks()`.
- **The weight is Point Color's weight.** `w = (1 − smoothstep(range ·
  0.45, range, hueDistance)) · saturation`, the exact expression already in
  `grade._apply_point_color()` (line 262), `gl.js` (line 353) and the Metal
  point-colour loop. The mask multiplies its geometric weight by
  `1 − amount · (1 − w)`. Because all three renderers already carry this
  function, parity is structural. Python gets `_color_weight(image, hue,
  range, amount)` beside `_luminance_weight()` and `apply_masks()` applies
  both.
- Uniforms: WebGL `u_localColorRange[4]` as `vec4(hue, range, amount, on)`
  per tile; Metal `localColorRange[16]`.
- Sampling: a "Sample colour" button in the mask panel puts the overlay into
  a dropper mode under the same pointer-ownership rules as the white-balance
  dropper; a click samples one pixel's hue through
  `GradeRenderer.sample()`, shift-drag samples an area and takes the mean hue.
- Overlay: today the mask overlay draws geometry only, so neither the
  luminance nor the colour range is visible while adjusting it. Move the
  overlay into the shader: a `u_overlayMask` index tints pixels by the
  *final* per-mask weight. Verify the current overlay's behaviour first; if it
  already composes ranges on the CPU, keep it.
- Tests: `test_color_range_weight_equals_point_color_weight` on a hue ramp,
  `test_color_range_composes_with_luminance_and_geometry`, and a
  `paritytest.html` case.

### 2.3 Import compatibility (optional; not taken)

`preset_io.py` (line 342) currently reports Lightroom's
`MaskGroupBasedCorrections` as skipped. Where a defensible counterpart now
exists, map `CorrectionRangeMask` luminance ranges onto `lumaLow`/`lumaHigh`
and colour-range samples onto `colorHue`/`colorRange`; keep reporting
everything else as skipped, in the same voice as today.

## Phase 3 — External editor round trip (built)

Every editor in the field can hand a rendered 16-bit file to a pixel editor
and take the result back as a stacked derivative. LightTable has no trace of
it. This also answers the demand for pixel layers and generative tools without
building either.

- **Server.** `POST /api/edit-external {names, mode, space, app}`.
  `mode: "adjusted"` builds an export job from the image's current state
  (`entry_for()`), fixed recipe: `format: tif`, `outputSpace` from the
  request (default `prophoto`), no resize, no watermark, metadata policy
  `all`, and runs it through `export_with_resident_engine()` (line 2529) in
  the export pool, writing `<stem>-Edit.tif` **beside the original**, which
  is where the incumbents put it and what the catalog scanner already indexes
  (note that `catalog_scan.SKIP_DIRS` skips `LightTable Merges`, so a
  derivatives folder of that shape would be invisible in catalog mode; check
  whether merge outputs are, and fix that while here). Collisions go through
  `export_workflow.collision_path(path, "rename")`. An unwritable folder
  returns an error naming the folder; no silent fallback to another place.
  `mode: "original"` is allowed only for processed originals and opens the
  file itself; a RAW original gets a message saying why not. Video is
  refused.
- **Register.** New `catalog_scan.register_file(cat, path) -> image_id`:
  find the source whose root contains the path, build the record with
  `read_metadata()`, `upsert_file()`, ensure the base image row. Then the
  stack: if the original is already stacked, a new `Catalog.add_to_stack()`
  appends it; otherwise `add_stack(stem, [original, derivative])`. The
  derivative copies rating, flag, label, keywords and IPTC from the original
  through a new `Catalog.copy_organizational_state()`; develop edits are not
  copied because they are baked into the pixels.
- **Host.** A new `openWith {paths, app}` action in `app/main.swift`
  (`NSWorkspace.shared.open(_:withApplicationAt:configuration:)`, or the
  default handler when `app` is empty) and in `windows-shell/src/main.rs`
  (`cmd /C start "" <path>`, next to the `explorer.exe` reveal at line 514).
  `listEditors` returns candidates for `public.tiff` from
  `NSWorkspace.shared.urlsForApplications(toOpen:)` with names and icon
  paths; Windows offers "Default app" plus a "Choose app…" `rfd` picker that
  stores the executable path. Prefs: `externalEditor {path, name}`,
  `externalEditorSpace`.
- **Client.** "Edit in…" on the Export pane and `Ctrl/⌘E`; a small dialog for
  app, mode, and colour space; progress through the existing `exportTimer`
  poll; on completion `reloadLibrary()` selects the derivative.
- **The return leg.** When the pixel editor saves, the derivative's bytes
  change in place. `upsert_file()` matches on `(source_id, relpath)` so the
  row, its stack membership and its state survive, and the render key
  carries mtime so the preview refreshes. The derivative's folder joins the
  Phase 8 watcher for the session so size, mtime, hash and thumbnail refresh
  without a manual Synchronize. Contract test
  `test_in_place_rewrite_keeps_state_and_stack`: register, rate, rewrite the
  bytes, rescan; same image id, same stack, new header hash. This is the one
  scenario the content-identity rule previously had not covered.
- Tests: a server test with a temp source and a stub host asserts the
  derivative path, the stack, the copied rating and keywords, the refusal on
  RAW + original, and the refusal on a read-only folder; the
  `finish_export()` / `render_cli.py` parity test gains a
  `tif` + `prophoto` case.

## Phase 4 — Focus stacking (built)

The merge workflow already aligns frames with SIFT + RANSAC and fuses
exposures; focus stacking is the third mode, and it is a feature the leading
incumbent lacks.

- `merge_workflow.focus_merge(values)`, 2 to 60 frames. Rails produce dozens,
  so the algorithm streams:
  1. Reference is the middle frame. Alignment chains `_pair_transform()`
     between neighbours as `panorama_merge()` does; focus breathing is a
     small scale change and the projective model absorbs it, with the
     translation-only fallback kept. Frames are warped into the reference
     canvas, and the result is cropped to the intersection of every warped
     valid mask.
  2. Sharpness `s = gaussian(|laplace(gaussian(gray, 1.0))|, 2.5)`.
  3. Pass one aligns and keeps only the running `s_max`. Pass two recomputes
     `s_i`, sets `w_i = exp((s_i − s_max) / τ)` with `τ = 0.15 · mean(s_max)`
     (a soft argmax), and accumulates `Σ w_i · L_l(I_i)` and `Σ G_l(w_i)` over
     five Laplacian-pyramid levels (`skimage.transform.pyramid_laplacian` and
     `pyramid_gaussian`). Peak memory is three pyramids regardless of frame
     count. Blend in linear light and re-encode, as `hdr_merge()` does.
- `_run_merge()` gains mode `focus`, manifest `kind: focus-merge`, output
  prefix `Focus`; `start_merge()` limit 60; `MERGE_MAX_EDGE` applies.
- UI: `#mergeMode` gains "Focus stack · sharpness fused";
  `mergeSelectionSummary` says the frames should come from a tripod or rail.
  The `MERGE` status gains an alignment inlier count, reported the way
  auto-upright reports confidence.
- Tests: a synthetic sharp checkerboard-and-gradient scene; four frames each
  blurred except one band with a 0.5 % scale step between frames;
  `focus_merge()` recovers PSNR above 32 dB against the sharp original and at
  least 95 % of its Laplacian energy; a 61-frame request is refused; thirty
  small frames merge under the memory of three full-size arrays.

## Phase 5 — Hold-a-key slider control (built)

Hold a key, scroll or press an arrow, and the mapped slider moves without
touching the panel. It is the single thing users of the studio-oriented
incumbent say they miss elsewhere, and it fits the keymap table that
Phase 3.4 of the workflow milestone introduced.

- `KEY_SCHEMES[scheme].speed` in `web/labels.js`, the same in both schemes:
  `e` exposure, `j` contrast, `h` highlights, `s` shadows, `w` whites,
  `k` blacks, `t` temp, `i` tint, `v` vibrance, `l` saturation, `y` clarity,
  `z` dehaze. None of the culling keys (`p x u a`, digits, `n`) are in it.
  Only `e` collides, with Classic's Detail, and tap-versus-hold resolves it.
- Mechanics, in the `keydown` handler (`web/app.js` line 4526): a speed key
  with no modifier, in Detail with a photo, sets `S.speed = {key, control,
  used: false}`, shows `#speedHud` over the image ("Exposure  0.00"), and
  does **not** run the tap action. A `wheel` listener while `S.speed` is set
  calls `pushUndo()` once, steps the control by the slider's `step × 5`
  (Shift: `× 1`, Alt: `× 20`), writes `S.grade[control]`, updates the
  `[data-g]` slider and its `[data-gv]` readout, and calls `drawGrade()`,
  which is the slider's own input path and already posts `nativeGrade` to
  the Metal preview. Left and right arrows do the same. Horizontal pointer
  drag over the image while held also adjusts, claiming the gesture before
  crop, mask and heal per the pointer-ownership rule. Auto-repeat keydowns
  are ignored. `keyup`: if `used`, `saveState()` (the slider's `change`
  path); if not, run the tap action the key would have had. Two taps within
  350 ms reset the control to `GRADE_DEFAULTS`.
- Film controls (`S.params`) are out of scope for the first cut; their
  sliders live behind different ids.
- The shortcut list in `web/index.html` (`shortcut-details`) gets a
  "Hold + scroll" row and a toggle beside `keySchemeSelect`.
- Test: a contract test in `tests/` parses the `speed` table out of
  `labels.js` and asserts it is disjoint from pick, reject, unflag, digits
  and survey in both schemes.

## Phase 6 — People masks (built)

Every editor in the field now sells portrait retouching on person-part masks:
face skin, eyes, eyebrows, lips, teeth, hair. LightTable's Subject, Sky and
Object selections stop at the person. Apple Vision provides person
segmentation and 76-point face landmarks on macOS 14 and later, which is the
same floor `--foreground-mask` already requires.

- **Helper.** `vision_helper.swift` gains a `person-parts {input,
  outputDir}` command (server mode and `--person-parts` argv) that writes
  L8 PNGs `person`, `face-skin`, `eyes`, `eyebrows`, `lips`, `teeth`, `hair`
  at the source's size plus a JSON `{faces, parts: {name: nonzero}}`.
  Requests: `VNGeneratePersonSegmentationRequest` (`.accurate`) and
  `VNDetectFaceLandmarksRequest`. Landmark polygons are filled with
  `CGContext`: eyes are the two eye contours grown 1.5 px; eyebrows and
  teeth (inner lips) are their polygons; lips are outer minus inner; face
  skin is the face contour plus a forehead cap (the contour mirrored above
  the eyebrow line by 0.55 × eyebrow-to-chin height), intersected with the
  person mask, minus eyes, eyebrows and outer lips; hair is the person mask
  within an ellipse around the face box grown 60 % up and sideways, minus
  face skin and facial features. Several faces are unioned. Hair and face
  skin are reported as `provider:
  "vision-estimated"`, because they are.
- **Python.** `semantic_masks.generate()` accepts the seven kinds;
  `VisionProvider.person_parts()` makes one helper call and caches the set
  under `CACHE/semantic/<key>_parts/`, so adding Lips after Eyes is free.
  Without the helper (Windows, or older macOS) `person` falls back to
  `subject_mask()` with `provider: "local-segmentation"`; every part refuses
  with "People masks need the Vision helper (macOS 14 or later)". No part is
  ever approximated from colour.
- **Resolution.** Subject, Sky, Object, and portrait parts now retain up to a
  1024 px edge stored as PNG-compressed base64
  (`bitmap.encoding: "png"`) in `_clean_bitmap()` and `normalizeMasks()`,
  decoded by `_raster_component()` through Pillow and by the JS rasteriser
  through an `Image` data URL. Older raw 256 px bitmaps remain readable.
  Image-guided refinement and fractional Vision alpha preserve softer edges.
- **Local tone controls.** Masks include Texture, Clarity, Whites, Blacks,
  and master/R/G/B tone curves. These controls use an ordered server preview
  after the global grade and preceding masks, matching export. Their result
  requires a preview refresh. Local curve editing preserves an imported LUT
  until the user changes it, then uses shape-preserving cubic interpolation.
- **Brush and gradient update.** New strokes accumulate Flow up to Density;
  old strokes preserve their earlier behavior. Auto Mask freezes a color-based
  selection at each stroke's start. Cross-photo paste refuses these saved
  selections, which must be recreated for the new photo. Radial masks support
  independent axes, rotation, and direct move/resize/rotate handles.
- **Selection quality remains limited.** Higher resolution improves boundary
  detail without replacing the underlying detectors. Hair and skin remain
  geometric estimates around detected faces. The portrait fixture's hair mask
  misses much of the long hair below the face; inspect and paint refinements.
  Subject/Sky/Object estimates can still select the wrong region.
- **UI.** A "People" button in the mask toolbar opens a popover with the
  seven parts and the face count ("2 faces found"); each creates a mask
  named after the part. A bundled tool preset "Soften skin" (face skin,
  texture −0.4, clarity −0.2) shows the workflow.
- **Privacy.** Same rule as the local AI index: nothing leaves the machine,
  no identity is inferred, and the bitmaps live in the edit state like any
  other mask.
- Tests: `test_semantic_masks.py` with a stub provider that writes synthetic
  part PNGs (`teeth` returns a 1024-edge PNG bitmap; parts refuse without the
  helper with the exact message); `test_edits.py` (PNG bitmaps rasterise to
  the same weights as raw ones; local texture and clarity change only the
  masked region). The release script's smoke step runs `--person-parts` on a
  CC0 portrait in `demo-assets/`, added if none exists.

## Phase 7 — Skin-tone uniformity (built)

The studio-oriented incumbent's colour editor can even out hue, saturation and
lightness variance inside a colour range. That is one extra term in Point
Color, which already samples up to eight hues with a range and a weight.

- Schema per point (`grade._clean_point_color()`, line 90): `refSaturation`
  and `refLuminance`, captured from the sampled pixel when the point is made
  (`pointColorSample`), and `uniformHue`, `uniformSaturation`,
  `uniformLuminance` in 0..1, default 0. Points without references (made
  before this change) show the sliders disabled until re-sampled.
- Math, identical in `_apply_point_color()`, `gl.js` line 347 and the Metal
  loop, applied *before* the existing shift and multipliers so a slider at 0
  leaves today's output byte-identical: with the existing weight `w`,
  `h += shortestArc(hue − h) · uniformHue · w`,
  `s += (refSaturation − s) · uniformSaturation · w`,
  `v += (refLuminance − v) · uniformLuminance · w`.
- Uniforms: WebGL `u_pointUniform[8]` as `vec4(uHue, uSat, uLum, refSat)` and
  `u_pointRefLum[8]`; Metal `pointUniform0..7` and `pointRefLum0/1`.
- UI: three sliders under Luminance in the Point Color block with the hint
  "Evens out the sampled colour across its range. Sample the patch the rest
  should match."
- Presets: native `.ltpreset` carries the keys; the XMP exporter reports them
  as unsupported rather than approximating.
- Tests: `test_grade.py` (uniformity 1 collapses hue variance on a ramp
  inside the range to under 1°; 0 is byte-identical to the current output);
  a `paritytest.html` case.

## Phase 8 — Watched folder (built)

Full tethering needs vendor SDKs and is only worth it if studio work becomes a
target segment. A watched folder covers most tethered workflows through the
camera maker's own utility, and every hot-folder workflow, by reusing the
ingest and verification code.

- **`watch_workflow.py`** (new; allowlist). `WatchService` follows
  `catalog_scan.ScanService`: one daemon thread, yields to `render_busy`,
  polls every enabled watch every 2 s with `os.scandir` (non-recursive
  unless `recursive` is set), tracks candidates by `(name, size, mtime_ns)`.
  A file is *settled* when size and mtime are unchanged across two polls and
  `catalog_scan.read_metadata()` succeeds, which is what stops a
  half-written capture from being indexed. At most 200 arrivals per poll. A
  folder that disappears (card ejected) flips the watch to `unavailable`
  and keeps polling.
- Two modes per watch:
  - `catalog`: the folder is inside an existing source; settled files go
    through `catalog_scan.register_file()` (shared with Phase 3).
  - `ingest`: `ingest_workflow.build_plan([item], request, existing_hashes)`
    then `copy_item()` into the destination with the rename template,
    verification, sidecars and backup, then `register_file()` on the copy.
    The source is never deleted or moved; that is the ingest invariant.
- A `watch_ledger` table `(watch_id, header_hash, handled_at)` keyed by
  `ingest_workflow.header_hash()` is what stops re-import across restarts.
- Options: `presetId` applies a preset's state to each arrival through the
  same merge `save_state()` uses (`server.py` line 508), which is how a look
  lands on capture; `follow` reports the newest arrival so the client can
  select it.
- **Server.** `/api/watch` (list, save, delete; prefs key `watches`, validated
  by `clean_watch()`), `/api/watch/status` per watch (enabled, available,
  handled, last arrival, errors, latest). The service starts with the catalog;
  `LIGHTTABLE_WATCH=0` disables it.
- **Client.** A "Watch folder…" dialog beside Ingest in `catalog-ui.js`
  (reusing `chooseIngestFolder` with a new `field`), a status pill in the
  library rail while a watch is enabled ("Watching Capture · 12 new"),
  2 s polling only while enabled (the `ingestPoll` pattern), `reloadLibrary()`
  on arrivals, and `go()` to the latest when `follow` is on and the user has
  not navigated in the last 5 s, so a cull is never yanked.
- Both platforms, no host change beyond the folder picker.
- Tests (`tests/test_watch_workflow.py`): a progressive write is not
  registered until settled; a settled file registers exactly once across a
  restart; ingest mode copies with verification and leaves the source; a
  duplicate by hash is skipped; an unavailable folder flips status without
  raising; follow reports the newest; a preset lands on the arrival's state.

## Decisions applied

1. **Where the denoise model lives.** The converted fp16 package is 78 MB.
   It is bundled under `Contents/Resources/models` so it works offline;
   `model_root()` still accepts an explicit development override. The first
   Core ML compilation goes into the user's Caches directory under the package
   digest, never beside the signed bundle. The licences (MIT weights,
   Apache-2.0 code) permit this distribution.
2. **Super-resolution stays off** as recorded on 2026-09-03. The plan does
   not touch it.
3. **Sixteen masks** as the new cap. Unlimited would mean an unbounded
   uniform layout; sixteen is what the Metal struct and the atlas want.
4. **Round-trip default colour space**: ProPhoto RGB, 16-bit TIFF. Anyone
   who wants sRGB picks it in the dialog.
5. **The watcher never deletes or moves** from the watched folder. The
   incumbent's auto-import moves files; ours copies and keeps a ledger.
6. **Windows parity** for the denoiser (1.5) and for people masks (none
   possible without a Windows segmentation runtime) is accepted as a
   follow-on, reported as unavailable in the meantime.

## Implementation proof — 2026-09-03

- The complete Python suite passes: **553 tests in 4.922 seconds**. The expected
  malformed-RAW fixture warnings and the forward-schema fallback diagnostic do
  not fail the run.
- Every changed browser script passes `node --check`; `main.swift` and
  `NativePreview.swift` type-check with the updated helper protocol;
  `cargo check --locked --manifest-path windows-shell/Cargo.toml` passes.
- `build-app.sh` produced and strictly code-signature-verified a 79 MB local
  `build/LightTable.app` containing the model package, model metadata, both
  licences and the Enhance and Vision helpers. This proves a local build, not a
  release or deployment.
- The bundled Enhance helper reduced the smoke fixture's noise standard
  deviation from **0.079898 to 0.001376**. The full three-synthetic / three-real
  evaluation gate selected gamma encoding, improved middle-strength PSNR by
  **12.6462 dB**, held mean flat-patch Delta E 76 to **0.341**, and measured
  **1.8790 seconds/MP first run, 1.0787 seconds/MP warm** on this Mac. Its
  contact sheet was visually inspected for smoothing and invented structure.
- The bundled Vision helper completed a real portrait smoke with one detected
  face and non-empty `person`, `face-skin`, `eyes`, `eyebrows`, `lips`, `teeth`
  and `hair` masks. The running web UI showed all seven part actions and the
  Soften skin preset, and its console had no warnings or errors.
- Focus-stack tests cover 0.5 % focus breathing and require PSNR above 32 dB
  with at least 95 % of the sharpest input's Laplacian energy; a 61-frame stack
  is refused and a 30-frame low-resolution stack completes. Watch-folder,
  external-edit, renderer-parity, denoise-evaluation and export-parity tests are
  included in the 553-test run.

## Not in this milestone

- A print module. Soft proofing exists; page setup and a print dialog do not.
  Worth its own plan once someone asks for it.
- Tethering through vendor SDKs.
- Body-skin masks (no local model provides them).
- Pixel layers, sky replacement, generative fill. The round trip in Phase 3 is
  the answer to the first; the other two conflict with the rule that nothing
  creates a hidden transform.
