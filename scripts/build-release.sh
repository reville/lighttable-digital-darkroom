#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${LIGHTTABLE_VERSION:-0.1.0}"
if [[ "$(uname -m)" != arm64 ]]; then
  echo "The macOS release currently supports Apple silicon only." >&2
  exit 1
fi
BUILD_NUMBER="${LIGHTTABLE_BUILD_NUMBER:-1}"
SIGN_IDENTITY="${LIGHTTABLE_SIGN_IDENTITY:--}"
OUTPUT_DIR="${LIGHTTABLE_OUTPUT_DIR:-$ROOT/dist}"
BUILD_ROOT="$ROOT/.build/release"
DERIVED_DATA="$BUILD_ROOT/DerivedData"
APP="$OUTPUT_DIR/LightTable.app"
PYTHON_VERSION="3.13.12"
PYTHON_SOURCE_REV="3bb2c2d2801ff68b92019cf1dbcbb133d60832bc"
RUST_SOURCE_REV="9dd59b0380194b93686aaa230a8bb9680aa270a4"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "LIGHTTABLE_VERSION must be a semantic version such as 0.1.0" >&2
  exit 2
fi
if [[ ! "$BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
  echo "LIGHTTABLE_BUILD_NUMBER must be a positive integer" >&2
  exit 2
fi

for TOOL in cargo git swiftc uv xcodebuild; do
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    echo "$TOOL is required to build a release" >&2
    exit 1
  fi
done

ensure_checkout() {
  local URL="$1"
  local REVISION="$2"
  local DESTINATION="$3"

  if [[ ! -d "$DESTINATION/.git" ]]; then
    mkdir -p "$(dirname "$DESTINATION")"
    git clone --filter=blob:none --no-checkout "$URL" "$DESTINATION"
  fi
  if ! git -C "$DESTINATION" cat-file -e "$REVISION^{commit}" 2>/dev/null; then
    git -C "$DESTINATION" fetch --depth=1 origin "$REVISION"
  fi
  git -C "$DESTINATION" checkout --detach --quiet "$REVISION"
  if [[ -n "$(git -C "$DESTINATION" status --porcelain)" ]]; then
    echo "Dependency checkout is dirty: $DESTINATION" >&2
    exit 1
  fi
}

mkdir -p "$OUTPUT_DIR" "$BUILD_ROOT/dependencies"

PYTHON_SOURCE="$BUILD_ROOT/dependencies/spektrafilm-python"
RUST_SOURCE="$BUILD_ROOT/dependencies/spektrafilm-rust"
ensure_checkout \
  "https://github.com/andreavolpato/agx-emulsion.git" \
  "$PYTHON_SOURCE_REV" "$PYTHON_SOURCE"
ensure_checkout \
  "https://github.com/turbasvin/spektrafilm-rs.git" \
  "$RUST_SOURCE_REV" "$RUST_SOURCE"

xcodebuild \
  -project LightTable.xcodeproj \
  -scheme LightTable \
  -configuration Release \
  -derivedDataPath "$DERIVED_DATA" \
  -destination 'generic/platform=macOS' \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  MARKETING_VERSION="$VERSION" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  build

BUILT_APP="$DERIVED_DATA/Build/Products/Release/LightTable.app"
if [[ ! -x "$BUILT_APP/Contents/MacOS/LightTable" ]]; then
  echo "Xcode did not produce the expected application" >&2
  exit 1
fi

rm -rf "$APP"
/usr/bin/ditto "$BUILT_APP" "$APP"
/usr/bin/ditto "$ROOT/build/LightTable.icns" \
  "$APP/Contents/Resources/LightTable.icns"
# The app compiles this source through Metal at runtime. Keeping it outside the
# Xcode Resources phase avoids requiring Apple's optional offline Metal tools.
/usr/bin/ditto "$ROOT/app/NativePreview.metal" \
  "$APP/Contents/Resources/NativePreview.metal"

PAYLOAD="$APP/Contents/Resources/LightTable"
mkdir -p "$PAYLOAD" "$PAYLOAD/engine" "$PAYLOAD/vendor/spektrafilm/src"

for SOURCE_FILE in "$ROOT"/*.py; do
  /usr/bin/ditto "$SOURCE_FILE" "$PAYLOAD/$(basename "$SOURCE_FILE")"
done
/usr/bin/ditto "$ROOT/lighttable_cli" "$PAYLOAD/lighttable_cli"
/usr/bin/ditto "$ROOT/lighttable" "$APP/Contents/MacOS/lighttable-cli"
/bin/chmod 755 "$APP/Contents/MacOS/lighttable-cli"
/usr/bin/ditto "$ROOT/media-formats.json" "$PAYLOAD/media-formats.json"
mkdir -p "$PAYLOAD/film_lab_ai" "$PAYLOAD/build"
for AI_SOURCE in "$ROOT"/film_lab_ai/*.py; do
  /usr/bin/ditto "$AI_SOURCE" "$PAYLOAD/film_lab_ai/$(basename "$AI_SOURCE")"
done
swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -o "$PAYLOAD/build/LightTableVision" \
  "$ROOT/film_lab_ai/vision_helper.swift"
swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -o "$PAYLOAD/build/LightTableEnhance" \
  "$ROOT/film_lab_ai/enhance_helper.swift"
VISION_SMOKE="$BUILD_ROOT/vision-person-parts"
rm -rf "$VISION_SMOKE"
VISION_RESULT="$("$PAYLOAD/build/LightTableVision" --person-parts \
  "$ROOT/tests/fixtures/photos/portrait.jpg" \
  "$VISION_SMOKE")"
if ! /usr/bin/grep -Eq '"faces":[1-9][0-9]*' <<<"$VISION_RESULT"; then
  echo "Vision person-parts smoke found no face: $VISION_RESULT" >&2
  exit 1
fi
for PART in person face-skin eyes eyebrows lips teeth hair; do
  if [[ ! -s "$VISION_SMOKE/$PART.png" ]]; then
    echo "Vision person-parts smoke omitted $PART.png" >&2
    exit 1
  fi
done
if [[ ! -d "$ROOT/scripts/models/denoise.mlpackage" \
      || ! -f "$ROOT/scripts/models/models.json" ]]; then
  echo "Missing converted denoise model; run scripts/fetch-models.py and scripts/convert-models.py" >&2
  exit 1
fi
mkdir -p "$APP/Contents/Resources/models"
/usr/bin/ditto "$ROOT/scripts/models/denoise.mlpackage" \
  "$APP/Contents/Resources/models/denoise.mlpackage"
/usr/bin/ditto "$ROOT/scripts/models/models.json" \
  "$APP/Contents/Resources/models/models.json"
for LICENSE_NAME in SCUNet-CODE-LICENSE.txt SCUNet-WEIGHTS-LICENSE.txt; do
  if [[ ! -f "$ROOT/scripts/models/$LICENSE_NAME" ]]; then
    echo "Missing model licence: scripts/models/$LICENSE_NAME" >&2
    exit 1
  fi
  /usr/bin/ditto "$ROOT/scripts/models/$LICENSE_NAME" \
    "$APP/Contents/Resources/models/$LICENSE_NAME"
done
/usr/bin/ditto "$ROOT/web" "$PAYLOAD/web"
/usr/bin/ditto "$ROOT/profiles" "$PAYLOAD/profiles"
/usr/bin/ditto "$ROOT/presets" "$PAYLOAD/presets"
/usr/bin/ditto "$PYTHON_SOURCE/src/spektrafilm" \
  "$PAYLOAD/vendor/spektrafilm/src/spektrafilm"

/usr/bin/ditto "$RUST_SOURCE/data" "$PAYLOAD/engine/data"
for PROFILE in "$ROOT"/profiles/*.json; do
  /usr/bin/ditto "$PROFILE" "$PAYLOAD/engine/data/profiles/$(basename "$PROFILE")"
done

mkdir -p "$PAYLOAD/licenses"
/usr/bin/ditto "$ROOT/LICENSE" "$PAYLOAD/licenses/LightTable-GPL-3.0.txt"
/usr/bin/ditto "$ROOT/THIRD_PARTY_NOTICES.md" "$PAYLOAD/licenses/THIRD_PARTY_NOTICES.md"
/usr/bin/ditto "$PYTHON_SOURCE/LICENSE" \
  "$PAYLOAD/licenses/spektrafilm-python-GPL-3.0.txt"
/usr/bin/ditto "$RUST_SOURCE/LICENSE" \
  "$PAYLOAD/licenses/spektrafilm-rust-GPL-3.0.txt"

CARGO_TARGET_DIR="$BUILD_ROOT/cargo/resident" \
  cargo build --release --locked --manifest-path "$ROOT/rust-engine/Cargo.toml"
/usr/bin/ditto "$BUILD_ROOT/cargo/resident/release/lighttable-engine" \
  "$PAYLOAD/engine/lighttable-engine"

CARGO_TARGET_DIR="$BUILD_ROOT/cargo/cli" \
  cargo build --release --locked --manifest-path "$RUST_SOURCE/Cargo.toml" \
  -p spektrafilm-cli --bin spektrafilm
/usr/bin/ditto "$BUILD_ROOT/cargo/cli/release/spektrafilm" \
  "$PAYLOAD/engine/spektrafilm-rs"
/bin/chmod 755 "$PAYLOAD/engine/lighttable-engine" \
  "$PAYLOAD/engine/spektrafilm-rs"

printf 'rev: %s\n' "$RUST_SOURCE_REV" > "$PAYLOAD/engine/VERSION.txt"
printf 'rev: %s\n' "$PYTHON_SOURCE_REV" > "$PAYLOAD/vendor/spektrafilm/VERSION.txt"

PYTHON_INSTALLS="$BUILD_ROOT/python-installs"
uv python install --install-dir "$PYTHON_INSTALLS" --no-bin "$PYTHON_VERSION"
PYTHON_HOME="$(find "$PYTHON_INSTALLS" -mindepth 1 -maxdepth 1 \
  -type d -name "cpython-$PYTHON_VERSION-*" -print -quit)"
if [[ ! -x "$PYTHON_HOME/bin/python3.13" ]]; then
  echo "Managed Python $PYTHON_VERSION was not installed" >&2
  exit 1
fi
/usr/bin/ditto "$PYTHON_HOME" "$APP/Contents/Resources/Python"
uv pip sync --no-config --compile-bytecode --system --break-system-packages \
  --python "$APP/Contents/Resources/Python/bin/python3.13" \
  "$ROOT/requirements-runtime.lock"
RAWPY_WHEELS="$BUILD_ROOT/rawpy-openmp"
"$ROOT/scripts/build-rawpy-openmp.sh" \
  "$APP/Contents/Resources/Python/bin/python3.13" "$RAWPY_WHEELS"
uv pip install --no-config --system --break-system-packages --no-deps --reinstall \
  --python "$APP/Contents/Resources/Python/bin/python3.13" \
  "$RAWPY_WHEELS"/rawpy_openmp-0.26.1-*.whl
"$APP/Contents/Resources/Python/bin/python3.13" \
  "$ROOT/scripts/verify-rawpy-openmp.py"
/usr/bin/ditto "$RAWPY_WHEELS/licenses" "$PAYLOAD/licenses/rawpy-openmp"
"$APP/Contents/Resources/Python/bin/python3.13" \
  "$ROOT/scripts/relocate-python-runtime.py" \
  "$APP/Contents/Resources/Python"

LIGHTTABLE_MODEL_DIR="$APP/Contents/Resources/models" \
  "$PAYLOAD/build/LightTableEnhance" --probe \
  | /usr/bin/grep -q '"denoise":true'
LIGHTTABLE_MODEL_DIR="$APP/Contents/Resources/models" \
LIGHTTABLE_ENHANCE_HELPER="$PAYLOAD/build/LightTableEnhance" \
PYTHONPATH="$PAYLOAD" \
  "$APP/Contents/Resources/Python/bin/python3.13" \
  "$ROOT/scripts/smoke-denoise.py"

"$APP/Contents/Resources/Python/bin/python3.13" \
  "$ROOT/scripts/native_localization_sources.py" --bundle "$APP" \
  --catalogs "$PAYLOAD/web/locales"

"$ROOT/scripts/sign-app.sh" "$APP" "$SIGN_IDENTITY"

echo "Built self-contained application: $APP"
