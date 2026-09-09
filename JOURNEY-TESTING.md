# Product journey testing

The product journey suite launches the real macOS application, drives the same
command executor used by the local control bridge, captures the composited
WebKit and Metal window, verifies that the photo viewport contains non-blank
pixels, exports a photo, and records structured timing data.

## Layers

| Layer | Fixtures | Journey | Intended gate |
| --- | --- | --- | --- |
| `pr` | Two checked-in CC0 JPEG contact sheets | Navigate across dimensions, rapid navigation, Metal to WebGL and back, Film off/on, slider, Compare, Actual/Fit zoom, export, screenshot | Every proposed change |
| `package` | The same two JPEGs | The complete `pr` journey plus executable separation, Mach-O, and strict codesign checks | Release and personal-app staging |
| `raw-curated` | The smallest practical example of all eight supported RAW extensions | Open every format in Metal, reject an intentionally corrupt RAW, recover to a valid photo, screenshot | `main` and regular scheduled testing |
| `raw-full` | All 35 CC0/public-domain RAW originals | Open the complete 627 MiB collection, corrupt-file recovery, screenshot | Less-frequent scheduled testing |

Run a layer locally with:

```sh
scripts/run-product-journey.sh pr
scripts/run-product-journey.sh raw-curated
scripts/run-product-journey.sh raw-full
scripts/run-product-journey.sh package /path/to/LightTable.app
```

The first, curated, and full commands rebuild `build/LightTable.app` unless
`LIGHTTABLE_JOURNEY_SKIP_BUILD=1` is set. Package mode never creates a release;
it tests the supplied already-built bundle.

Results and screenshots default to `build/product-journeys/`. Set
`LIGHTTABLE_JOURNEY_OUTPUT_DIR` to retain them elsewhere. The JSON schema
contains total process wall time, every user-step duration, and count/median,
p95, and maximum values for server, resident renderer, GPU, decode, texture
upload, paint, and end-to-end render latency. Compare results only on like
hardware and runner images; CI virtualization and cold caches are part of the
measurement.

The package layer is a blocking step in release and personal-app staging. The
PR and RAW layers are deliberately exposed as local commands without automatic
scheduled CI activation: enabling recurring macOS runners and the 627 MiB LFS
download is an explicit repository-cost decision.

## Occasional browser UI audits

The [on-demand UI audit](scripts/ui-audit/README.md) checks layout and language
states, captures candidate snapshots, and explores seeded command/pointer walks.
Run it explicitly when useful. It is not part of commit checks or `mnb`, and it
does not replace native recorded review for Metal or macOS behavior.
