# Native preview profile — 2026-09-02

The hybrid preview keeps the HTML interface in `WKWebView` and places an
`MTKView` directly underneath its transparent preview region. The resident
Rust/WGPU renderer writes tightly packed RGBA8 (`FLRA` header plus pixels), the
server streams that surface without JPEG encoding, and Metal performs the
display grade, curves, HSL, zoom, and pan. A small WebGL/JPEG helper for
histogram and sampling tools is refreshed only after interaction settles.

## Matched method

- Apple Silicon Mac, macOS 26.6.2.
- The same app build and `18-IDG_20250706_203649_381.DNG` source for both paths.
- Resident Rust/WGPU renderer (`wgpu (GPU)`) and fresh per-run disk caches.
- 30 `print_exposure` edits from 0.333 through 2.110 at each requested width.
- Each run warms the selected width, resets its measurements, then records 30
  edits. High-resolution runs record both the 1100 px responsive frame and the
  requested-resolution settled frame.
- The control is the real AppKit app with `LIGHTTABLE_DISABLE_NATIVE_PREVIEW=1`,
  so its presentation path is `WKWebView` + JPEG decode + WebGL upload + paint.
- Native completion is recorded after its Metal command buffer completes.
  WebKit completion is recorded after the new WebGL image survives two
  animation frames.

Values are median milliseconds; p95 is shown in parentheses for end-to-end
latency. All 300 expected frames completed, with no native fallback or failed
presentation.

| Requested width | Frame | WebKit end-to-end | Native end-to-end | Change | WebKit display handoff | Native surface handoff | Handoff change |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1100 | settled | 67.0 (100.0) | 59.5 (99.0) | -11.2% | 22.0 | 4.04 | -81.7% |
| 2200 | responsive 1100 | 88.0 (98.0) | 41.0 (80.0) | -53.4% | 39.0 | 4.00 | -89.7% |
| 2200 | settled | 374.5 (416.0) | 186.5 (223.0) | -50.2% | 108.5 | 9.11 | -91.6% |
| 5000 | responsive 1100 | 86.5 (104.0) | 41.0 (51.0) | -52.6% | 37.0 | 4.04 | -89.1% |
| 5000 | settled | 660.5 (746.0) | 311.5 (336.0) | -52.8% | 212.0 | 14.99 | -92.9% |

“Display handoff” is URL fetch/decode + texture upload + visible paint for the
WebKit path, and native-surface fetch + Metal texture upload + GPU completion
for the hybrid path. At 5000 px specifically, median texture upload fell from
71.0 ms to 4.20 ms and post-upload paint/GPU completion fell from 137.0 ms to
1.06 ms.

The 1100 px result is now limited mostly by the film render itself: its native
surface handoff is about 18 ms faster, but the total improves by 7.5 ms. At
2200 and 5000 px, native mode also avoids synchronous JPEG encoding, lowering
the median request phase from 177.0 to 132.5 ms and from 362.5 to 253.5 ms,
respectively.

## Benchmark controls

The app accepts these test-only environment variables:

- `LIGHTTABLE_DISABLE_NATIVE_PREVIEW=1` selects the WKWebView/WebGL control.
- `LIGHTTABLE_NATIVE_BENCHMARK_IMAGE`, `_WIDTH`, `_ITERATIONS`, and `_OUTPUT`
  select a deterministic source and write the result as JSON.
- `LIGHTTABLE_DIR`, `LIGHTTABLE_CACHE_DIR`, and `LIGHTTABLE_PREFS_FILE` isolate the
  photo corpus, cache, and preferences without changing the user's saved app
  sources.
