# Responsiveness implementation and evidence

Implemented September 4–5, 2026, on `codex/responsiveness-20260905`, based on
`f93e822`. Development build and tests use an isolated worktree and disposable
photo profiles. The results below describe that development validation; personal
installation is a separate step.

## Delivered

1. **Coalesced native drawing.** Basic grades no longer trigger redundant layout
   redraws. Unchanged curve arrays stay out of bridge messages. Layout updates
   compare geometry before sending or drawing. Histogram helper requests and
   uploads wait for idle, including helpers that finish loading during a drag.
2. **Corrected previews stay in Metal.** Lens-profile requests and more than 16
   enabled heals use a cached corrected RGBA base. The renderer then applies an
   identity optics transform and no live heals, preventing double application.
   Small live manual corrections and browser JPEG fallback retain their paths.
3. **Reusable photo presentation.** An LRU holds 48 response descriptions keyed
   by source revision and complete base recipe; provisional RAW results are not
   retained. Native prefetch uploads immutable textures to a 256 MiB / 12-entry
   cache, cancelled by navigation epoch and cleared on memory pressure. Active
   textures remain owned by the display. A cached return still commits the new
   photo's native state, even after a failed photo with identical prior pixels.
4. **Interactive work gets priority.** The shared reentrant admission gate orders
   current previews ahead of waiting exports and prefetch. Stale generations are
   checked before resident dispatch. RAW refinement retains at most 16 pending
   jobs, replacing obsolete work per window. Running CPU/GPU work is allowed to
   complete safely; this is admission priority, not mid-kernel preemption.
5. **Automatic preview size.** Auto chooses resolution from viewport size, display
   scale, zoom, crop, and available source dimensions. The existing manual values
   remain available. Interaction remains progressive, followed by settled quality.
6. **Bounded library DOM.** Square and photo grids virtualize visible cells plus
   overscan, binary-search each layout column, reuse cells, and preserve scroll
   anchors. Cached summaries and delta selection updates avoid repeat full scans.
7. **Resident film reuse.** Print-exposure changes reuse developed-film results.
   Metering, tables, and image buffers also reuse bounded resources. The necessary
   dependency libraries and shaders are vendored with provenance and GPL license;
   builds do not depend on an edited Cargo cache. Retained resources have explicit
   budgets; active render allocations are additional. Large images bypass retention.

## Measured results

On this arm64 macOS host, the final native stress run delivered all **1,080 input
acknowledgments** across nine scenarios: exposure, exposure with detail/curve,
local mask, 17 heals, and concurrent export. Inputs were dispatched once per
animation frame, about 60 Hz. Median acknowledgment was **18–19 ms**; p95 was
**19–22 ms**. The worst individual acknowledgment was 46 ms. These timings end
at GPU completion plus bridge acknowledgment: the driver did not deliver drawable
presentation callbacks, so they do not establish physical display latency or
prove that every acknowledged frame was scanned out.

The earlier matched two-scenario drag test showed essentially unchanged median
basic-grade latency. The observed 239 ms histogram-upload hitch was absent after
idle gating (the direct repeated scenario peaked at 19 ms; the larger final mask
matrix peaked at 46 ms). These are individual machine runs, not a universal bound.

The full **35-photo real RAW collection** opened successfully. A 30-navigation
burst with reversal settled on the intended photo; the final request took 10 ms.
Returning from a deliberately corrupt file completed in 6 ms using the cached
Metal texture. Cold RAW decoding still costs substantially more than cached
navigation; no claim is made that every first RAW open is instant.

In the Chrome library check with **3,000 photos**, both grid modes retained only
**30–58 cells**, with no overlaps or page errors in the tested scroll/reversal
sequence. Resize preserved the anchor within 0.5 px. Auto at DPR 2 selected 1800 px;
manual 1100 px remained 1100 px. This is browser proof, separate from native proof.

The matched resident-engine experiment showed complete 2200 px print-exposure
requests improving from **83.28 to 35.16 ms (2.37×)**; 1100 px requests improved
from **15.43 to 8.25 ms (1.87×)**. **82 float32 Metal outputs were byte-identical**
to the original release binary, including invalidation, eviction, and oversized
cases. Those are engine timings, not end-to-end UI measurements. First frames
and changes to upstream film parameters still require the full pipeline.

## Validation and reproduction

- 811 Python tests passed, including scheduling, corrected pixel parity, cache
  identity/eviction, stale navigation, automatic sizing, and grid layout.
- 8 Rust tests passed; locked offline release build succeeded.
- Native app smoke, export, 35 RAWs, corrupt recovery, strict code signature, and
  actual native screenshots passed. The screenshot was visually inspected.
- Release packaging copies all root Python modules and the web directory, so the
  new scheduling and browser modules follow the existing packaging path. A fresh
  distributable release bundle, hosted CI, and personal install are not
  part of this local implementation validation.

```sh
.venv/bin/python -m unittest discover -s tests
cargo test --locked --offline --manifest-path rust-engine/Cargo.toml
cargo build --release --locked --offline --manifest-path rust-engine/Cargo.toml
LIGHTTABLE_INTERACTION_BENCHMARK=1 .venv/bin/python scripts/native-app-smoke.py \
  --app build/LightTable.app --layer pr --timeout 180 \
  --output build/responsiveness-final.json --screenshot build/responsiveness-final.png
.venv/bin/python scripts/native-app-smoke.py --app build/LightTable.app \
  --layer raw-full --timeout 360 --output build/responsiveness-raw-full.json
```

Native smoke requires macOS GUI and local loopback access; RAW runs require the
repository's real fixture collection. Each harness closes its test app and server.
The interaction recorder is opt-in and inactive during normal use.

Evidence: [native interactions](bench/responsiveness/native-interactions.json),
[RAW journey](bench/responsiveness/raw-journey.json),
[3,000-photo browser grid](bench/responsiveness/grid-3000.json), and
[resident engine results and reproduction](rust-engine/bench-results/README.md).
Full native per-input samples and screenshots remain under `build/` locally.
