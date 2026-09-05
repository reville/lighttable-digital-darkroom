# Responsiveness profile — 2026-09-01

Matched before/after runs used the same Apple Silicon Mac, local photo corpus,
resident Rust/WGPU renderer, cached headless Chromium, and command:

```sh
MPLCONFIGDIR=/tmp/lighttable-mpl .venv/bin/python bench/benchmark.py \
  --app-root . --photos raw-test --widths 1100,2200,5000 \
  --iterations 30 --browser-iterations 30 --large-library-count 2048 \
  --skip-export --output RESULT.json
```

The browser result is measured from control input until the new frame survives
two animation frames. It includes UI scheduling, HTTP, image decode, WebGL
texture upload, and paint. "First" is the responsive frame; "settled" is the
requested-resolution, fully refined frame.

| Scenario | Before median | After median | Change | Before p95 | After p95 |
|---|---:|---:|---:|---:|---:|
| Cold navigation to first visible frame | 2364 ms | 501 ms | -78.8% | — | — |
| 1100 px edit, first frame | 189.9 ms | 71.85 ms | -62.2% | 202.3 ms | 79.8 ms |
| 2200 px edit, first frame | 398.45 ms | 55.5 ms | -86.1% | 500.4 ms | 103.3 ms |
| 5000 px edit, first frame | 630.1 ms | 68.25 ms | -89.2% | 688.6 ms | 79.4 ms |
| Photo navigation, first frame | 383.35 ms | 77.2 ms | -79.9% | 524.0 ms | 94.1 ms |
| 1100 px edit, settled | 189.9 ms | 71.8 ms | -62.2% | — | — |
| 2200 px edit, settled | 398.45 ms | 357.85 ms | -10.2% | — | — |
| 5000 px edit, settled | 630.1 ms | 633.35 ms | +0.5% | — | — |
| Photo navigation, settled | 381.0 ms | 330.65 ms | -13.2% | — | — |
| 2048-image `/api/images` | 134.36 ms | 11.72 ms | -91.3% | 138.9 ms | 13.97 ms |
| 2048-image server startup | 664.16 ms | 438.22 ms | -34.0% | — | — |

Warm server-only 1100 px render latency changed from 52.97 ms to 50.5 ms.
That confirms the largest gains came from the user-visible scheduling, RAW
decode, browser handoff, texture-upload, and library-enumeration paths rather
than rewriting the small Python HTTP layer. Export was intentionally excluded
because interactive work was the optimization priority for this pass.
