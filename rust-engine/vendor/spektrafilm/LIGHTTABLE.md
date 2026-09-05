# LightTable resident rendering fork

Vendored from https://github.com/turbasvin/spektrafilm-rs at
`9dd59b0380194b93686aaa230a8bb9680aa270a4` (GPL-3.0; see LICENSE).
Only the four required library crates and shader sources are included.
No profiles, sample images, CLI, GUI, or build artifacts are vendored.

Local changes add an opt-in, caller-keyed film-density checkpoint, bounded
GPU resource reuse, and a borrowed metering result to the resident pipeline.
The unchanged processing path remains available when no cache key is supplied.

The cache keeps one image-size-specific scratch pair (192 MiB maximum), one
developed-film checkpoint (96 MiB maximum), and an LRU of immutable tables
(32 MiB including both CPU comparison bytes and GPU buffers). Larger images
use the original complete chain without retaining these image allocations.
Temporary buffers needed by an active render are additional to the cache.

The checkpoint is taken after deterministic grain and DIR, before print
spectral processing. LightTable assigns monotonic input and pipeline
generations and includes the full runtime parameter object in its identity,
normalizing only negative-film print exposure. A rebuilt pipeline, reloaded
input, changed dimensions, metering method, or any other parameter invalidates
reuse. CPU and unsupported resident backends retain their original fallback.

`LIGHTTABLE_RESIDENT_STAGE_CACHE=0` disables this opt-in path. Responses expose
`film_stage_cache_hit`, `gpu_buffers_reused`, and `resident_cache_bytes` for
benchmarks. `bench_resident_cache.py` compares float32 outputs and timings,
checks invalidation and cache budgets, and fails if a GPU was unavailable.
