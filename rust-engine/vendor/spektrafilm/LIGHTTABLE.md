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

On Vulkan adapters limited to 256 or 512 threads per workgroup, independent
linear kernels evaluate several pixels per invocation while retaining the same
1024-pixel dispatch grid and per-pixel arithmetic. Adapters supporting 1024
threads keep the original shader source. Blur and reduction kernels retain
their existing 256-thread groups. `spektrafilm-gpu` unit tests validate every
shader variant; the ignored `portable_linear_dispatch_covers_partial_and_multiple_groups`
test verifies pixel coverage on a real GPU using a 256-thread device limit.

Linux builds link the distribution's OpenBLAS via `pkg-config`; install
`libopenblas-dev` on Debian/Ubuntu or `openblas` on Arch before building.

The worker reports the adapter name, API, device type, driver, workgroup limits,
and whether transfers use mapped primary buffers or staging. Software Vulkan
is selected only when explicitly requested with `SPEKTRAFILM_BACKEND=wgpu`;
automatic selection uses the threaded CPU backend instead. An image exceeding
the device's per-buffer or dispatch limits uses CPU for that request while the
GPU remains available for smaller previews.

Discrete GPUs reuse one dimension-matched readback buffer up to 32 MiB, using
spare scratch capacity within the existing 320 MiB total cache ceiling. Mapped
primary buffers retain the established unified-memory path. `gpu_timings`
reports CPU setup, submit-to-map wait, CPU readback copy, input-image upload and
output readback bytes,
and readback reuse. These are wall-clock boundaries; submit-to-map includes GPU
work and staging transfers, not an isolated hardware timestamp measurement.

Run `bench_resident_cache.py --binary <engine> --data <engine/data> --width 2200 --require-hardware --output <report.json>` on each intended Linux GPU. The report
contains adapter identity, per-request cache/transfer data, float32 pixel parity,
and downstream edit medians. Omitting `--require-hardware` permits explicit
software-Vulkan execution checks but does not establish hardware performance.
