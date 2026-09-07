#!/bin/bash
# Build a relocatable macOS wheel, including LibRaw and its OpenMP runtime.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# != 2 || "$(uname -m)" != arm64 || "$(uname -s)" != Darwin ]]; then
  echo "usage: $0 /path/to/python /path/to/wheel-output (macOS arm64)" >&2
  exit 2
fi
PYTHON="$1"
OUTPUT="$2"
RAWPY_REV=a411daac7d5bf1ab07f6285164e41596a205393e
# Submodule commits are recorded by RAWPY_REV, rather than moving branches.
LIBRAW_REV=0b56545a4f828743f28a4345cdfdd4c49f9f9a2a
CMAKE_REV=6e26c9e73677dc04f9eb236a97c6a4dc225ba7e8
for TOOL in git cmake pkg-config uv; do
  command -v "$TOOL" >/dev/null
done
# Clang 21 substantially regresses the serial X-Trans kernel. Prefer the
# installed, measured Clang 17 toolchain when the caller chose no compilers;
# Other toolchains remain available and explicit overrides take priority.
CLT_BIN=/Library/Developer/CommandLineTools/usr/bin
if [[ -z "${CC+x}" && -z "${CXX+x}" \
      && -x "$CLT_BIN/clang" && -x "$CLT_BIN/clang++" ]]; then
  CLT_VERSION="$("$CLT_BIN/clang++" --version)"
  if [[ "$CLT_VERSION" == "Apple clang version 17."* ]]; then
    export CC="$CLT_BIN/clang" CXX="$CLT_BIN/clang++"
    if [[ -z "${SDKROOT+x}" ]]; then
      export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
    fi
  fi
fi
export SDKROOT="${SDKROOT:-$(xcrun --show-sdk-path)}"
mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
# rawpy's upstream setup script contains unquoted shell paths. Keep its
# disposable checkout out of user/workspace paths which can contain spaces.
WORK="$(mktemp -d /tmp/lighttable-rawpy.XXXXXX)"
trap '/bin/rm -rf "$WORK"' EXIT
PREFIX="$WORK/native"
export MACOSX_DEPLOYMENT_TARGET=13.0
"$ROOT/scripts/build-rawpy-native.sh" "$WORK/native-source" "$PREFIX"
git clone --filter=blob:none --no-checkout https://github.com/letmaik/rawpy.git "$WORK/source"
git -C "$WORK/source" checkout --detach "$RAWPY_REV"
git -C "$WORK/source" submodule update --init --depth 1 external/LibRaw external/LibRaw-cmake
test "$(git -C "$WORK/source/external/LibRaw" rev-parse HEAD)" = "$LIBRAW_REV"
test "$(git -C "$WORK/source/external/LibRaw-cmake" rev-parse HEAD)" = "$CMAKE_REV"
git -C "$WORK/source/external/LibRaw" apply "$ROOT/packaging/libraw-xtrans-determinism.patch"
"$PYTHON" - "$WORK/source" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
# Keep the upstream stock wheel alongside this extension. Separate Python
# package and Mach-O loader paths prevent one decoder replacing the other.
package = root / 'rawpy'
source = package / '_rawpy.pyx'
text = source.read_text()
native_method = '            int dcraw_process() nogil\n'
assert text.count(native_method) == 2
text = text.replace(native_method, native_method + '            void setCancelFlag() nogil\n')
needle = '    property raw_type:\n'
assert text.count(needle) == 1
text = text.replace(needle, '''    def request_cancel(self):
        # The caller joins its monitor before recycling this RawPy object.
        # LibRaw owns the atomic flag and unwinds on its processing thread.
        self.p.setCancelFlag()

    property is_xtrans:
        def __get__(self):
            return self.p.imgdata.idata.filters == 9

''' + needle)
source.write_text(text)
for path in [root/'setup.py', package/'__init__.py', package/'enhance.py']:
    text = path.read_text().replace('rawpy', 'rawpy_openmp')
    text = text.replace('_rawpy_openmp', '_rawpy')
    path.write_text(text.replace('github.com/letmaik/rawpy_openmp',
                                 'github.com/letmaik/rawpy'))
package.rename(root/'rawpy_openmp')
with (root/'rawpy_openmp'/'__init__.py').open('a') as handle:
    handle.write('\n# The bundled LibRaw has deterministic X-Trans wavefront scheduling.\n')
    handle.write('LIGHTTABLE_XTRANS_WAVEFRONT = 1\n')
    handle.write('LIGHTTABLE_RAW_CANCEL = 1\n')
PY

uv venv --python "$PYTHON" "$WORK/env"
uv pip sync --python "$WORK/env/bin/python" "$ROOT/packaging/rawpy-build.lock"
cmake -S "$WORK/source/external/LibRaw-cmake" -B "$WORK/libraw-build" \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON \
  -DLIBRAW_PATH="$WORK/source/external/LibRaw" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_DEPLOYMENT_TARGET=13.0 \
  -DCMAKE_OSX_SYSROOT="$SDKROOT" \
  -DCMAKE_INSTALL_PREFIX="$WORK/install" \
  -DCMAKE_INSTALL_NAME_DIR="$WORK/install/lib" \
  -DCMAKE_PREFIX_PATH="$PREFIX" -DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew \
  -DLCMS2_INCLUDE_DIR="$PREFIX/include" -DLCMS2_LIBRARIES="$PREFIX/lib/liblcms2.dylib" \
  -DJPEG_INCLUDE_DIR="$PREFIX/include" -DJPEG_LIBRARY_RELEASE="$PREFIX/lib/libjpeg.dylib" \
  -DJASPER_INCLUDE_DIR="$PREFIX/include" \
  -DJASPER_LIBRARY_RELEASE="$PREFIX/lib/libjasper.dylib" \
  -DENABLE_OPENMP=ON \
  "-DOpenMP_CXX_FLAGS=-Xpreprocessor -fopenmp -I$PREFIX/include" \
  -DOpenMP_CXX_LIB_NAMES=omp \
  -DOpenMP_omp_LIBRARY="$PREFIX/lib/libomp.dylib" \
  -DENABLE_EXAMPLES=OFF -DENABLE_X3FTOOLS=ON -DENABLE_6BY9RPI=ON \
  -DENABLE_RAWSPEED=OFF
cmake --build "$WORK/libraw-build" --target install --parallel 8
"$WORK/env/bin/python" - "$WORK/libraw-build" "$OUTPUT/build-info.json" <<'PY'
from pathlib import Path
import json, re, subprocess, sys
build = Path(sys.argv[1])
cache = {}
for line in (build/'CMakeCache.txt').read_text().splitlines():
    match = re.match(r'([^/#][^:]*):[^=]+=(.*)', line)
    if match:
        cache[match[1]] = match[2]
compiler = cache['CMAKE_CXX_COMPILER']
metadata = {
    'compiler': compiler,
    'compilerVersion': subprocess.check_output([compiler, '--version'], text=True).strip(),
    'sdk': cache['CMAKE_OSX_SYSROOT'],
    'deploymentTarget': cache['CMAKE_OSX_DEPLOYMENT_TARGET'],
    'buildType': cache['CMAKE_BUILD_TYPE'],
    'releaseFlags': cache.get('CMAKE_CXX_FLAGS_RELEASE', ''),
    'openmpFlags': cache.get('OpenMP_CXX_FLAGS', ''),
    'xtransWavefront': 1,
    'cooperativeCancellation': 1,
}
sdk_settings = Path(metadata['sdk'])/'SDKSettings.json'
if sdk_settings.is_file():
    metadata['sdkVersion'] = json.loads(sdk_settings.read_text()).get('Version')
Path(sys.argv[2]).write_text(json.dumps(metadata, indent=2) + '\n')
print(json.dumps(metadata, indent=2), flush=True)
PY
/usr/bin/grep -q '#define LIBRAW_USE_OPENMP 1' "$WORK/install/include/libraw/libraw_config.h"
(
  cd "$WORK/source"
  RAWPY_USE_SYSTEM_LIBRAW=1 PKG_CONFIG_PATH="$WORK/install/lib/pkgconfig" \
    CFLAGS="-I$PREFIX/include" CXXFLAGS="-I$PREFIX/include" \
    "$WORK/env/bin/python" setup.py bdist_wheel --dist-dir "$WORK/wheels"
)
"$WORK/env/bin/delocate-wheel" --require-archs arm64 \
  --wheel-dir "$OUTPUT" "$WORK"/wheels/*.whl
uv pip install --python "$WORK/env/bin/python" --reinstall --no-deps \
  rawpy==0.26.1 "$OUTPUT"/rawpy_openmp-0.26.1-*.whl
"$WORK/env/bin/python" "$ROOT/scripts/verify-rawpy-openmp.py"
mkdir -p "$OUTPUT/licenses"
cp "$WORK/source/LICENSE" "$OUTPUT/licenses/rawpy-MIT.txt"
cp "$WORK/source/external/LibRaw/LICENSE.LGPL" "$OUTPUT/licenses/LibRaw-LGPL-2.1.txt"
cp "$PREFIX/licenses/"* "$OUTPUT/licenses/"
echo "OpenMP-enabled rawpy wheel: $OUTPUT"
