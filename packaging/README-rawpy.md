# macOS RAW runtime

`scripts/build-release.sh` installs the runtime lock, then adds
`scripts/build-rawpy-openmp.sh`'s `rawpy_openmp` package alongside standard rawpy.
`raw_decode_runtime.py` selects the decoder from a header-only sensor check.
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

## X-Trans correctness guard

LibRaw 0.22's X-Trans tile loop reads the same image buffer that adjacent
parallel tiles overwrite. On a real 40 MP X100VI RAF, eight threads produced
different output between runs and against one thread. The observed maximum
error was 1119 in a 16-bit channel. This is not a floating-point tolerance test.

`packaging/libraw-xtrans-determinism.patch` keeps that particular loop serial
as a defense in depth. The new serial build was slower than the existing wheel
on X-Trans, so application dispatch retains stock rawpy for those sensors.
Bayer files use the OpenMP extension. Do not remove this
guard solely because a faster decode completes successfully. The large
X-Trans parallel demosaic speedup remains unavailable until the upstream
algorithm provides independent tile inputs or another deterministic schedule.

Run the candidate interpreter with real X-Trans and Bayer sources before
changing the dependency pins or guard:

```sh
candidate-python scripts/benchmark-rawpy-openmp.py \
  --baseline-python stock-wheel-python photo.RAF photo.DNG
```

The harness starts fresh processes with one and eight threads, repeats both,
reports phase timings and SHA-256 hashes, and fails on any pixel change. It
reads originals only and never opens a catalog.

## RAW grid thumbnails

`scripts/benchmark-raw-thumbnails.py` compares ImageIO and rawpy embedded
thumbnail extraction on supplied RAW files. The initial NEF/CR2 measurements
favoured rawpy, so RAW thumbnail decoding keeps the existing draft JPEG path.
Scan callbacks enqueue bounded background work after catalog transactions
commit. On-demand grid thumbnails remain the fallback when the queue is full,
rendering is busy, or a source cannot be decoded.
