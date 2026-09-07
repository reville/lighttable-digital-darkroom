# macOS RAW runtime

`scripts/build-release.sh` installs the runtime lock, then adds
`scripts/build-rawpy-openmp.sh`'s `rawpy_openmp` package alongside standard rawpy.
`raw_decode_runtime.py` uses the parallel decoder for Bayer and X-Trans. A
header-only sensor check retains stock X-Trans decoding for older wheels that
lack the verified scheduling marker.
The personal updater checks the actual runtime's OpenMP support and library
dependencies, so an old
base app must first receive a full release build.

The rawpy/LibRaw/CMake source commits are pinned in that script. Native library
source URLs and SHA-256 digests live in `scripts/build-rawpy-native.sh`; build
Python dependencies live in `packaging/rawpy-build.lock`. No Homebrew library
is linked into the finished wheel. Source builds retain macOS 13 compatibility;
using recent Homebrew bottles would accidentally raise the minimum OS to the
build machine's version. `delocate` bundles native libraries and their license
texts are copied into the app. `scripts/verify-rawpy-openmp.py` checks OpenMP,
bundled dependencies, and each binary's deployment target after installation.

When neither `CC` nor `CXX` is supplied, the builder prefers installed Apple
Clang 17 from Command Line Tools and its SDK. Clang 21 produced identical
pixels but substantially slower X-Trans code in local comparisons. Explicit
compiler/SDK overrides are honored; other toolchains still build. The wheel
output's `build-info.json` records the actual compiler, SDK, deployment target,
and optimization flags. No compiler is needed at app runtime.

## Deterministic X-Trans parallelism

LibRaw 0.22's X-Trans tile loop reads the same image buffer that adjacent
parallel tiles overwrite. On a real 40 MP X100VI RAF, eight threads produced
different output between runs and against one thread. The observed maximum
error was 1119 in a 16-bit channel. This is not a floating-point tolerance test.

`packaging/libraw-xtrans-determinism.patch` preserves the original tile math
and schedules independent tiles on diagonals. Each tile waits for its left
neighbor and the preceding row through the tile above-right. The diagonal
number is `2 * row + column`; the OpenMP barrier completes every predecessor
before advancing. Round-robin assignment distributes each diagonal's work
across threads. Tile scratch buffers remain private to each worker.

This preserves the serial algorithm's already-developed neighboring pixels.
An immutable copy of the pre-development input removed the race but changed
pixels, so that approach was rejected. The diagonal schedule needs no extra
full-frame copy. Both one-pass and three-pass interpolation use this schedule.

Half-size input has a separate race: adjacent sensor rows can write the same
output pixel/channel during the initial RAW copy. The patch serializes only
that inexpensive copy when shrinking; full-size copies remain parallel. This
also covers shrink used by other LibRaw preprocessing options.

The generated package carries `LIGHTTABLE_XTRANS_WAVEFRONT = 1`, which the
packaging verifier requires. The app falls back to stock for an older unmarked
X-Trans runtime. Do not remove the dependency order solely because a faster
decode completes successfully; repeated byte identity is the acceptance gate.

Run the candidate interpreter with real X-Trans and Bayer sources before
changing the dependency pins or guard:

```sh
candidate-python scripts/benchmark-rawpy-openmp.py \
  photo.RAF photo.DNG --baseline-python stock-wheel-python --threads 1 4 8
candidate-python scripts/benchmark-rawpy-openmp.py \
  photo.RAF --baseline-python stock-wheel-python \
  --variants onepass smooth daylight half --threads 1 8
```

The harness starts fresh processes at each thread count, repeats each twice,
reports phase timings and SHA-256 hashes, and fails on any pixel change. It
also fails if a candidate silently routes back to the stock decoder. It reads
originals only and never opens a catalog.

## RAW grid thumbnails

`scripts/benchmark-raw-thumbnails.py` compares ImageIO and rawpy embedded
thumbnail extraction on supplied RAW files. The initial NEF/CR2 measurements
favoured rawpy, so RAW thumbnail decoding keeps the existing draft JPEG path.
Scan callbacks enqueue bounded background work after catalog transactions
commit. On-demand grid thumbnails remain the fallback when the queue is full,
rendering is busy, or a source cannot be decoded.
