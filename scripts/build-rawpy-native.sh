#!/bin/bash
# Pinned source builds: Homebrew bottles can require a newer OS than our app.
set -euo pipefail
WORK="$1"
PREFIX="$2"
mkdir -p "$WORK" "$PREFIX" "$PREFIX/licenses"
fetch() {
  local NAME="$1" URL="$2" SHA="$3"
  curl --fail --location --silent --show-error --retry 3 "$URL" -o "$WORK/$NAME.tar"
  echo "$SHA  $WORK/$NAME.tar" | shasum -a 256 --check --status
  mkdir -p "$WORK/$NAME"
  tar -xf "$WORK/$NAME.tar" -C "$WORK/$NAME" --strip-components=1
}
fetch openmp https://github.com/llvm/llvm-project/releases/download/llvmorg-19.1.7/openmp-19.1.7.src.tar.xz bd7e6901ab086fd268750363017935fd4a717c153dad3c2aab86cb0140d9e3fe
fetch cmake https://github.com/llvm/llvm-project/releases/download/llvmorg-19.1.7/cmake-19.1.7.src.tar.xz 11c5a28f90053b0c43d0dec3d0ad579347fc277199c005206b963c19aae514e3
fetch jpeg https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/3.2.0/libjpeg-turbo-3.2.0.tar.gz 6f30092cef9fb839779646608f4ee14ae3cbac989c47fa05e841b0841f09878e
fetch jasper https://github.com/jasper-software/jasper/releases/download/version-4.2.8/jasper-4.2.8.tar.gz 98058a94fbff57ec6e31dcaec37290589de0ba6f47c966f92654681a56c71fae
fetch lcms https://downloads.sourceforge.net/project/lcms/lcms/2.19.1/lcms2-2.19.1.tar.gz bfc54f7bab59fbc921012014a8032e4cba4abd46db47d46b76416a8c0b2815c8
build() {
  local NAME="$1"
  shift
  cmake -S "$WORK/$NAME" -B "$WORK/$NAME-build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_DEPLOYMENT_TARGET=13.0 \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_NAME_DIR="$PREFIX/lib" \
    -DCMAKE_PREFIX_PATH="$PREFIX" -DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "$@"
  cmake --build "$WORK/$NAME-build" --target install --parallel 8
}
build openmp -DOPENMP_ENABLE_LIBOMPTARGET=OFF -DOPENMP_ENABLE_OMPT_TOOLS=OFF \
  -DLIBOMP_ENABLE_SHARED=ON
build jpeg -DENABLE_STATIC=OFF -DWITH_JPEG8=ON -DWITH_TURBOJPEG=OFF
build jasper -DJAS_ENABLE_PROGRAMS=OFF -DJAS_ENABLE_DOC=OFF \
  -DJAS_ENABLE_OPENGL=OFF -DJAS_ENABLE_LIBHEIF=OFF \
  -DJPEG_INCLUDE_DIR="$PREFIX/include" -DJPEG_LIBRARY_RELEASE="$PREFIX/lib/libjpeg.dylib"
(
  cd "$WORK/lcms"
  CC=clang CPPFLAGS= lt_cv_sys_max_cmd_len=262144 \
    CFLAGS="-O3 -mmacosx-version-min=13.0" LDFLAGS="-mmacosx-version-min=13.0" \
    ./configure --prefix="$PREFIX" --disable-static --without-jpeg --without-tiff
  make -j8
  make install
)
cp "$WORK/openmp/LICENSE.TXT" "$PREFIX/licenses/LLVM-OpenMP.txt"
cp "$WORK/jpeg/README.ijg" "$PREFIX/licenses/libjpeg.txt"
cp "$WORK/jpeg/LICENSE.md" "$PREFIX/licenses/libjpeg-turbo.txt"
cp "$WORK/jasper/LICENSE.txt" "$PREFIX/licenses/jasper.txt"
cp "$WORK/lcms/LICENSE" "$PREFIX/licenses/lcms2.txt"
