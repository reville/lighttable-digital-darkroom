# Resident film reuse — September 4, 2026

Measured on the same macOS host using the actual WGPU/Metal backend, an original
`f93e822` release binary, and the optimized release binary. The fixture is a
synthetic linear RGB image with gradients, saturated patches, edges, and highlights,
processed through Portra 400 / Endura with grain and halation enabled. These are
resident-engine timings, not UI input-to-presentation measurements.

| Preview width | Median film render before → after | Median complete request before → after |
| --- | --- | --- |
| 1100 px | 12.79 → 4.46 ms (2.87×) | 15.43 → 8.25 ms (1.87×) |
| 2200 px | 53.38 → 14.02 ms (3.81×) | 83.28 → 35.16 ms (2.37×) |

Each median includes 12 distinct print-exposure changes after the initial warmup.
Each size also covers camera EV, metering method, auto-exposure off, camera blur,
film/paper gamma, preflash, print filtration, different inputs, dimensions, stock
changes, pipeline eviction/rebuild, and slide scanning: 37 cases per size.
All 74 float32 TIFFs matched the original binary **byte for byte**. Cache telemetry
confirmed checkpoint hits for all measured print changes and misses for invalidated
inputs/parameters. Peak retained cache was 30.3 MiB at 1100 and 113.3 MiB at 2200.

Four further cases at 3600 px and a small-size transition matched byte for byte.
The large image exceeds the retention limits; the full pipeline runs and the large
image buffers are not retained. No speedup is expected or claimed for that case.
Four final cases with a one-byte input-cache budget also matched after forcing
input eviction and reload; they confirm that generations prevent stale reuse.
CPU fallback output also matched across the original 37-case matrix; the benchmark
rejects CPU-only execution as GPU performance proof.

The first frame and non-print changes must still run the full film chain, plus a
checkpoint copy for cacheable images. These are not promised to become faster.
Some measured individual cold requests were slower; these improvements target
repeated print adjustments. The cache limits bound retained allocations, not the
additional temporary memory needed by an active render.

Reproduce (Python standard library only):

```sh
cargo build --release --locked --manifest-path rust-engine/Cargo.toml
python3 rust-engine/bench_resident_cache.py --data /path/to/engine/data --width 1100
python3 rust-engine/bench_resident_cache.py --data /path/to/engine/data --width 2200
python3 rust-engine/bench_resident_cache.py --data /path/to/engine/data --width 3600 --budget-check
```

Without `--baseline-binary`, the script compares the current binary with reuse
explicitly disabled/enabled. Supply an original binary with `--baseline-binary`
to repeat the before/after comparison recorded here. It fails on a float32 pixel
mismatch, incorrect checkpoint hit/miss, excess retained bytes, or unavailable GPU.
All subprocesses have bounded request waits and are closed after execution.
