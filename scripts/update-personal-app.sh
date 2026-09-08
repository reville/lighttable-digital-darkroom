#!/bin/bash
# Quickly refresh the isolated personal app without rebuilding its 628 MB
# Python environment or pinned third-party engines. A full release build remains
# the source of truth whenever one of those runtime inputs changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="install"
case "${1:-}" in
  "") ;;
  --build-only) MODE="build-only" ;;
  --check) MODE="check" ;;
  -h|--help)
    cat <<'USAGE'
usage: scripts/update-personal-app.sh [--build-only|--check]

Refresh /Applications/LightTable - NPR Installed.app from the current source.
The installed app supplies the unchanged, self-contained Python runtime.

  --build-only  Build and verify .build/personal/LightTable - NPR Installed.app
  --check       Validate that the installed runtime can be reused, then exit

Environment overrides:
  LIGHTTABLE_PERSONAL_APP       installed app path
  LIGHTTABLE_PERSONAL_BASE_APP  known-good app whose runtime should be reused
  LIGHTTABLE_BUILD_NUMBER       numeric bundle build (default: current minute)
USAGE
    exit 0
    ;;
  *)
    echo "Unknown option: $1" >&2
    exit 2
    ;;
esac

INSTALL_APP="${LIGHTTABLE_PERSONAL_APP:-/Applications/LightTable - NPR Installed.app}"
BASE_APP="${LIGHTTABLE_PERSONAL_BASE_APP:-$INSTALL_APP}"
BUILD_ROOT="$ROOT/.build/personal"
BUILD_NUMBER="${LIGHTTABLE_BUILD_NUMBER:-$(date +%Y%m%d%H%M)}"
VERSION="${LIGHTTABLE_VERSION:-0.1.0}"
PRODUCT_NAME="LightTable - NPR Installed"
BUNDLE_IDENTIFIER="com.reville.filmlab.nprinstalled"
DATA_NAME="Film Lab - NPR Installed"

if [[ ! "$BUILD_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
  echo "LIGHTTABLE_BUILD_NUMBER must be a positive integer" >&2
  exit 2
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "LIGHTTABLE_VERSION must be a semantic version such as 0.1.0" >&2
  exit 2
fi

for TOOL in cargo codesign git plutil rsync shasum swiftc xcodebuild; do
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    echo "$TOOL is required for a personal app update" >&2
    exit 1
  fi
done

BASE_CONTENTS="$BASE_APP/Contents"
BASE_PAYLOAD="$BASE_CONTENTS/Resources/LightTable"
BASE_NATIVE="$BASE_CONTENTS/MacOS/LightTable"
BASE_PYTHON="$BASE_CONTENTS/Resources/Python/bin/python3.13"
BUILD_RELEASE="$ROOT/scripts/build-release.sh"
PYTHON_VERSION="$(sed -n 's/^PYTHON_VERSION="\([^"]*\)"/\1/p' "$BUILD_RELEASE")"
PYTHON_SOURCE_REV="$(sed -n 's/^PYTHON_SOURCE_REV="\([^"]*\)"/\1/p' "$BUILD_RELEASE")"
RUST_SOURCE_REV="$(sed -n 's/^RUST_SOURCE_REV="\([^"]*\)"/\1/p' "$BUILD_RELEASE")"
PYTHON_SOURCE="$ROOT/.build/release/dependencies/spektrafilm-python"
RUST_SOURCE="$ROOT/.build/release/dependencies/spektrafilm-rust"
PACKAGE_RESOLVED="$ROOT/LightTable.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
MODEL_PACKAGE="$ROOT/scripts/models/denoise.mlpackage"
MODEL_INDEX="$ROOT/scripts/models/models.json"
APP_ICON="$ROOT/build/LightTable.icns"

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required file: $1" >&2
    exit 1
  fi
}

require_directory() {
  if [[ ! -d "$1" ]]; then
    echo "Missing required directory: $1" >&2
    exit 1
  fi
}

require_file "$BUILD_RELEASE"
require_file "$ROOT/requirements-runtime.lock"
require_file "$PACKAGE_RESOLVED"
require_file "$MODEL_INDEX"
require_file "$APP_ICON"
require_directory "$MODEL_PACKAGE"
require_directory "$BASE_CONTENTS"
require_file "$BASE_CONTENTS/Info.plist"
require_file "$BASE_NATIVE"
require_file "$BASE_PAYLOAD/engine/spektrafilm-rs"
require_file "$BASE_PAYLOAD/engine/VERSION.txt"
require_file "$BASE_PAYLOAD/vendor/spektrafilm/VERSION.txt"
require_file "$BASE_PYTHON"
require_directory "$PYTHON_SOURCE/.git"
require_directory "$RUST_SOURCE/.git"

if [[ -z "$PYTHON_VERSION" || -z "$PYTHON_SOURCE_REV" || -z "$RUST_SOURCE_REV" ]]; then
  echo "Could not read the pinned runtime versions from scripts/build-release.sh" >&2
  exit 1
fi

# Never clone a damaged app as the next personal build's foundation.
/usr/bin/codesign --verify --deep --strict "$BASE_APP"
if ! /usr/bin/file -b "$BASE_NATIVE" | /usr/bin/grep -q 'Mach-O'; then
  echo "The base app executable is not a native Mach-O binary; use a known-good base app" >&2
  exit 1
fi
if /usr/bin/find "$BASE_APP" -type f \( -name '*.nbi' -o -name '*.nbc' \) \
    -print -quit | /usr/bin/grep -q .; then
  echo "The base app contains runtime compiler cache files; run a full release build" >&2
  exit 1
fi

ACTUAL_PYTHON_VERSION="$(PYTHONDONTWRITEBYTECODE=1 "$BASE_PYTHON" -c \
  'import platform; print(platform.python_version())')"
if [[ "$ACTUAL_PYTHON_VERSION" != "$PYTHON_VERSION" ]]; then
  echo "Python changed ($ACTUAL_PYTHON_VERSION -> $PYTHON_VERSION); run scripts/build-release.sh" >&2
  exit 1
fi

# The version pin alone cannot distinguish the stock single-threaded wheel
# from our bundled OpenMP build. Upgrade old base apps with a full release.
if ! PYTHONDONTWRITEBYTECODE=1 "$BASE_PYTHON" "$ROOT/scripts/verify-rawpy-openmp.py"; then
  echo "The bundled RAW runtime changed; run scripts/build-release.sh before the quick updater" >&2
  exit 1
fi

TEMP_CHECK="$(mktemp -d "${TMPDIR:-/tmp}/lighttable-personal-check.XXXXXX")"
cleanup_check() {
  /bin/rm -rf "$TEMP_CHECK"
}
trap cleanup_check EXIT

/usr/bin/sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
  "$ROOT/requirements-runtime.lock" \
  | /usr/bin/tr '[:upper:]_' '[:lower:]-' \
  | LC_ALL=C /usr/bin/sort > "$TEMP_CHECK/expected-packages.txt"
PYTHONDONTWRITEBYTECODE=1 "$BASE_PYTHON" -c \
  'import importlib.metadata as m
for d in sorted(m.distributions(), key=lambda item: item.metadata["Name"].lower()):
    name = d.metadata["Name"].lower().replace("_", "-")
    if name not in ("pip", "rawpy-openmp"):
        print(f"{name}=={d.version}")' \
  | LC_ALL=C /usr/bin/sort > "$TEMP_CHECK/actual-packages.txt"
if ! /usr/bin/diff -u "$TEMP_CHECK/expected-packages.txt" \
    "$TEMP_CHECK/actual-packages.txt"; then
  echo "Python packages changed; run scripts/build-release.sh before using the quick updater" >&2
  exit 1
fi

BASE_PYTHON_REV="$(sed -n 's/^rev: //p' \
  "$BASE_PAYLOAD/vendor/spektrafilm/VERSION.txt")"
BASE_RUST_REV="$(sed -n 's/^rev: //p' "$BASE_PAYLOAD/engine/VERSION.txt")"
if [[ "$BASE_PYTHON_REV" != "$PYTHON_SOURCE_REV" \
      || "$BASE_RUST_REV" != "$RUST_SOURCE_REV" ]]; then
  echo "A pinned film engine changed; run scripts/build-release.sh" >&2
  exit 1
fi
if [[ "$(git -C "$PYTHON_SOURCE" rev-parse HEAD)" != "$PYTHON_SOURCE_REV" \
      || "$(git -C "$RUST_SOURCE" rev-parse HEAD)" != "$RUST_SOURCE_REV" ]]; then
  echo "The cached film-engine sources do not match the release pins" >&2
  exit 1
fi

SPARKLE_VERSION="$(/usr/bin/plutil -extract pins.0.state.version raw \
  "$PACKAGE_RESOLVED")"
BASE_SPARKLE_VERSION="$(/usr/libexec/PlistBuddy \
  -c 'Print :CFBundleShortVersionString' \
  "$BASE_CONTENTS/Frameworks/Sparkle.framework/Versions/Current/Resources/Info.plist")"
if [[ "$SPARKLE_VERSION" != "$BASE_SPARKLE_VERSION" ]]; then
  echo "Sparkle changed ($BASE_SPARKLE_VERSION -> $SPARKLE_VERSION); run scripts/build-release.sh" >&2
  exit 1
fi

if [[ "$MODE" == "install" ]]; then
  if /usr/bin/pgrep -f "$INSTALL_APP/Contents/MacOS/LightTable" >/dev/null 2>&1; then
    echo "$PRODUCT_NAME is running. Quit it before installing the update." >&2
    exit 1
  fi
  if /usr/bin/pgrep -f \
      "$INSTALL_APP/Contents/Resources/Python/bin/python3.*server.py" \
      >/dev/null 2>&1; then
    echo "$PRODUCT_NAME's server is running. Quit the app before installing." >&2
    exit 1
  fi
fi

if [[ "$MODE" == "check" ]]; then
  echo "Quick personal update is ready: the installed runtime matches all pinned dependencies."
  exit 0
fi

cleanup_check
trap - EXIT

hash_sources() {
  /usr/bin/find "$@" \
    \( -name .git -o -name __pycache__ -o -name target -o -name xcuserdata \) \
      -prune -o -type f -exec /usr/bin/shasum -a 256 {} \; \
    | /usr/bin/sed "s|$ROOT/||g" \
    | LC_ALL=C /usr/bin/sort \
    | /usr/bin/shasum -a 256 \
    | /usr/bin/awk '{print $1}'
}

NATIVE_HASH="$(hash_sources \
  "$ROOT/app/main.swift" "$ROOT/app/NativePreview.swift" "$ROOT/app/DiagnosticReports.swift")"
XCODE_CONFIG_HASH="$(hash_sources \
  "$ROOT/app/Info.plist" "$ROOT/LightTable.xcodeproj/project.pbxproj" \
  "$PACKAGE_RESOLVED")"
HELPER_HASH="$(hash_sources \
  "$ROOT/film_lab_ai/vision_helper.swift" \
  "$ROOT/film_lab_ai/enhance_helper.swift")"
ENGINE_HASH="$(hash_sources "$ROOT/rust-engine/Cargo.toml" \
  "$ROOT/rust-engine/Cargo.lock" "$ROOT/rust-engine/src")"
MODEL_HASH="$(hash_sources "$MODEL_PACKAGE" "$MODEL_INDEX" \
  "$ROOT/scripts/models/SCUNet-CODE-LICENSE.txt" \
  "$ROOT/scripts/models/SCUNet-WEIGHTS-LICENSE.txt")"
SOURCE_TREE_HASH="$(hash_sources \
  "$ROOT"/*.py "$ROOT/lighttable" "$ROOT/lighttable_cli" \
  "$ROOT/media-formats.json" "$ROOT/web" "$ROOT/profiles" "$ROOT/presets" \
  "$ROOT/film_lab_ai" "$ROOT/app/main.swift" \
  "$ROOT/app/NativePreview.swift" "$ROOT/app/DiagnosticReports.swift" "$ROOT/app/NativePreview.metal" \
  "$ROOT/rust-engine/Cargo.toml" "$ROOT/rust-engine/Cargo.lock" \
  "$ROOT/rust-engine/src")"
SOURCE_REVISION="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  SOURCE_DIRTY="true"
else
  SOURCE_DIRTY="false"
fi

PREVIOUS_MANIFEST="$BASE_PAYLOAD/personal-build.env"
manifest_value() {
  local KEY="$1"
  if [[ -f "$PREVIOUS_MANIFEST" ]]; then
    /usr/bin/awk -F= -v key="$KEY" '$1 == key {print substr($0, length(key) + 2)}' \
      "$PREVIOUS_MANIFEST"
  fi
}

PREVIOUS_XCODE_CONFIG_HASH="$(manifest_value XCODE_CONFIG_HASH)"
if [[ -n "$PREVIOUS_XCODE_CONFIG_HASH" \
      && "$PREVIOUS_XCODE_CONFIG_HASH" != "$XCODE_CONFIG_HASH" ]]; then
  echo "Xcode packaging changed; run scripts/build-release.sh once to establish a new base" >&2
  exit 1
fi

mkdir -p "$BUILD_ROOT"
if [[ "$MODE" == "install" ]]; then
  INSTALL_PARENT="$(dirname "$INSTALL_APP")"
  STAGE_ROOT="$(mktemp -d "$INSTALL_PARENT/.lighttable-personal.XXXXXX")"
else
  STAGE_ROOT="$(mktemp -d "$BUILD_ROOT/.stage.XXXXXX")"
fi
STAGE_APP="$STAGE_ROOT/$PRODUCT_NAME.app"
cleanup_stage() {
  /bin/rm -rf "$STAGE_ROOT"
}
trap cleanup_stage EXIT

echo "Reusing the verified Python runtime with an APFS clone..."
if ! /bin/cp -cRp "$BASE_APP" "$STAGE_APP"; then
  /bin/rm -rf "$STAGE_APP"
  /usr/bin/ditto "$BASE_APP" "$STAGE_APP"
fi

STAGE_CONTENTS="$STAGE_APP/Contents"
STAGE_PAYLOAD="$STAGE_CONTENTS/Resources/LightTable"
PREVIOUS_NATIVE_HASH="$(manifest_value NATIVE_HASH)"
PREVIOUS_HELPER_HASH="$(manifest_value HELPER_HASH)"
PREVIOUS_ENGINE_HASH="$(manifest_value ENGINE_HASH)"
PREVIOUS_MODEL_HASH="$(manifest_value MODEL_HASH)"

if [[ -z "$PREVIOUS_NATIVE_HASH" || "$PREVIOUS_NATIVE_HASH" != "$NATIVE_HASH" ]]; then
  echo "Building the native shell (incremental)..."
  DERIVED_DATA="$ROOT/.build/release/DerivedData"
  xcodebuild \
    -quiet \
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
  require_file "$BUILT_APP/Contents/MacOS/LightTable"
  /usr/bin/ditto "$BUILT_APP/Contents/MacOS/LightTable" \
    "$STAGE_CONTENTS/MacOS/LightTable"
else
  echo "Native shell unchanged; reusing it."
fi

# Rebuild the small app-owned payload. The expensive Python environment,
# Sparkle framework, and pinned external CLI remain the verified cloned copies.
BASE_EXTERNAL_CLI="$BASE_PAYLOAD/engine/spektrafilm-rs"
/bin/rm -rf "$STAGE_PAYLOAD"
mkdir -p "$STAGE_PAYLOAD/engine" "$STAGE_PAYLOAD/vendor/spektrafilm/src" \
  "$STAGE_PAYLOAD/build" "$STAGE_PAYLOAD/licenses"

for SOURCE_FILE in "$ROOT"/*.py; do
  /usr/bin/ditto "$SOURCE_FILE" "$STAGE_PAYLOAD/$(basename "$SOURCE_FILE")"
done
/usr/bin/rsync -a --exclude='__pycache__' --exclude='*.pyc' \
  "$ROOT/lighttable_cli/" "$STAGE_PAYLOAD/lighttable_cli/"
/usr/bin/ditto "$ROOT/lighttable" "$STAGE_CONTENTS/MacOS/lighttable-cli"
/bin/chmod 755 "$STAGE_CONTENTS/MacOS/lighttable-cli"
/usr/bin/ditto "$ROOT/media-formats.json" "$STAGE_PAYLOAD/media-formats.json"
/usr/bin/ditto "$ROOT/build/icon-1024.png" "$STAGE_PAYLOAD/build/icon-1024.png"
/usr/bin/rsync -a --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  "$ROOT/web/" "$STAGE_PAYLOAD/web/"
/usr/bin/rsync -a --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  "$ROOT/profiles/" "$STAGE_PAYLOAD/profiles/"
/usr/bin/rsync -a --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  "$ROOT/presets/" "$STAGE_PAYLOAD/presets/"
/usr/bin/rsync -a --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.swift' "$ROOT/film_lab_ai/" "$STAGE_PAYLOAD/film_lab_ai/"
/usr/bin/rsync -a --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  "$PYTHON_SOURCE/src/spektrafilm/" \
  "$STAGE_PAYLOAD/vendor/spektrafilm/src/spektrafilm/"
/usr/bin/rsync -a --exclude='.DS_Store' "$RUST_SOURCE/data/" \
  "$STAGE_PAYLOAD/engine/data/"
for PROFILE in "$ROOT"/profiles/*.json; do
  /usr/bin/ditto "$PROFILE" \
    "$STAGE_PAYLOAD/engine/data/profiles/$(basename "$PROFILE")"
done
/usr/bin/ditto "$BASE_EXTERNAL_CLI" "$STAGE_PAYLOAD/engine/spektrafilm-rs"
/usr/bin/ditto "$PYTHON_SOURCE/LICENSE" \
  "$STAGE_PAYLOAD/licenses/spektrafilm-python-GPL-3.0.txt"
/usr/bin/ditto "$RUST_SOURCE/LICENSE" \
  "$STAGE_PAYLOAD/licenses/spektrafilm-rust-GPL-3.0.txt"
printf 'rev: %s\n' "$RUST_SOURCE_REV" > "$STAGE_PAYLOAD/engine/VERSION.txt"
printf 'rev: %s\n' "$PYTHON_SOURCE_REV" \
  > "$STAGE_PAYLOAD/vendor/spektrafilm/VERSION.txt"

if [[ -z "$PREVIOUS_HELPER_HASH" || "$PREVIOUS_HELPER_HASH" != "$HELPER_HASH" ]]; then
  echo "Building the local Vision and Enhance helpers..."
  swiftc -O -swift-version 5 -target arm64-apple-macos13.0 \
    -o "$STAGE_PAYLOAD/build/LightTableVision" \
    "$ROOT/film_lab_ai/vision_helper.swift"
  swiftc -O -swift-version 5 -target arm64-apple-macos13.0 \
    -o "$STAGE_PAYLOAD/build/LightTableEnhance" \
    "$ROOT/film_lab_ai/enhance_helper.swift"
else
  echo "Local helpers unchanged; reusing them."
  /usr/bin/ditto "$BASE_PAYLOAD/build/LightTableVision" \
    "$STAGE_PAYLOAD/build/LightTableVision"
  /usr/bin/ditto "$BASE_PAYLOAD/build/LightTableEnhance" \
    "$STAGE_PAYLOAD/build/LightTableEnhance"
fi

if [[ -z "$PREVIOUS_ENGINE_HASH" || "$PREVIOUS_ENGINE_HASH" != "$ENGINE_HASH" ]]; then
  echo "Building the resident GPU engine (incremental)..."
  CARGO_TARGET_DIR="$ROOT/.build/release/cargo/resident" \
    cargo build --release --locked --manifest-path "$ROOT/rust-engine/Cargo.toml"
  /usr/bin/ditto \
    "$ROOT/.build/release/cargo/resident/release/lighttable-engine" \
    "$STAGE_PAYLOAD/engine/lighttable-engine"
else
  echo "Resident GPU engine unchanged; reusing it."
  /usr/bin/ditto "$BASE_PAYLOAD/engine/lighttable-engine" \
    "$STAGE_PAYLOAD/engine/lighttable-engine"
fi
/bin/chmod 755 "$STAGE_PAYLOAD/build/LightTableVision" \
  "$STAGE_PAYLOAD/build/LightTableEnhance" \
  "$STAGE_PAYLOAD/engine/lighttable-engine" \
  "$STAGE_PAYLOAD/engine/spektrafilm-rs"

if [[ -z "$PREVIOUS_MODEL_HASH" || "$PREVIOUS_MODEL_HASH" != "$MODEL_HASH" ]]; then
  echo "Refreshing the denoise model..."
  /bin/rm -rf "$STAGE_CONTENTS/Resources/models"
  mkdir -p "$STAGE_CONTENTS/Resources/models"
  /bin/cp -cRp "$MODEL_PACKAGE" \
    "$STAGE_CONTENTS/Resources/models/denoise.mlpackage"
  /usr/bin/ditto "$MODEL_INDEX" "$STAGE_CONTENTS/Resources/models/models.json"
  for LICENSE_NAME in SCUNet-CODE-LICENSE.txt SCUNet-WEIGHTS-LICENSE.txt; do
    require_file "$ROOT/scripts/models/$LICENSE_NAME"
    /usr/bin/ditto "$ROOT/scripts/models/$LICENSE_NAME" \
      "$STAGE_CONTENTS/Resources/models/$LICENSE_NAME"
  done
else
  echo "Denoise model unchanged; reusing it."
fi

/usr/bin/ditto "$ROOT/app/NativePreview.metal" \
  "$STAGE_CONTENTS/Resources/NativePreview.metal"
/usr/bin/ditto "$APP_ICON" "$STAGE_CONTENTS/Resources/LightTable.icns"

PLIST="$STAGE_CONTENTS/Info.plist"
plist_string() {
  /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST"
}
plist_bool() {
  /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :$1 bool $2" "$PLIST"
}

# The quick update starts with an older bundle's plist. Refresh protocol
# declarations from source as well as its version and personal identity.
"$BASE_PYTHON" - "$ROOT/app/Info.plist" "$PLIST" <<'PYPLIST'
import plistlib
import sys
from pathlib import Path
source = plistlib.loads(Path(sys.argv[1]).read_bytes())
target_path = Path(sys.argv[2])
target = plistlib.loads(target_path.read_bytes())
target["CFBundleURLTypes"] = source["CFBundleURLTypes"]
target_path.write_bytes(plistlib.dumps(target))
PYPLIST

plist_string CFBundleName "$PRODUCT_NAME"
plist_string CFBundleDisplayName "$PRODUCT_NAME"
plist_string CFBundleIdentifier "$BUNDLE_IDENTIFIER"
plist_string CFBundleVersion "$BUILD_NUMBER"
plist_string CFBundleShortVersionString "$VERSION"
plist_string LightTableSourceRevision "$SOURCE_REVISION"
plist_string FilmLabSourceRevision "$SOURCE_REVISION"
plist_string LightTableSourceTree "$SOURCE_TREE_HASH"
plist_bool LightTableSourceDirty "$SOURCE_DIRTY"
plist_bool LightTablePersonalInstall true
plist_bool SUEnableAutomaticChecks false
/usr/libexec/PlistBuddy -c 'Delete :LSEnvironment' "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c 'Add :LSEnvironment dict' "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_CATALOG_FILE string ${HOME}/Library/Application Support/${DATA_NAME}/Catalog/library.sqlite3" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_PRESETS_FILE string ${HOME}/Library/Application Support/${DATA_NAME}/presets.json" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_PREFS_FILE string ${HOME}/Library/Application Support/${DATA_NAME}/prefs.json" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_AI_DIR string ${HOME}/Library/Application Support/${DATA_NAME}/AI Index" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_CACHE_DIR string ${HOME}/Library/Caches/${DATA_NAME}" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:NUMBA_CACHE_DIR string ${HOME}/Library/Caches/${DATA_NAME}/compiled-runtime" "$PLIST"
/usr/libexec/PlistBuddy -c \
  "Add :LSEnvironment:LIGHTTABLE_SERVER_LOG string ${HOME}/Library/Logs/${DATA_NAME}/server.log" "$PLIST"

cat > "$STAGE_PAYLOAD/personal-build.env" <<MANIFEST
FORMAT=1
SOURCE_REVISION=$SOURCE_REVISION
SOURCE_TREE_HASH=$SOURCE_TREE_HASH
SOURCE_DIRTY=$SOURCE_DIRTY
NATIVE_HASH=$NATIVE_HASH
XCODE_CONFIG_HASH=$XCODE_CONFIG_HASH
HELPER_HASH=$HELPER_HASH
ENGINE_HASH=$ENGINE_HASH
MODEL_HASH=$MODEL_HASH
PYTHON_VERSION=$PYTHON_VERSION
PYTHON_SOURCE_REV=$PYTHON_SOURCE_REV
RUST_SOURCE_REV=$RUST_SOURCE_REV
MANIFEST

echo "Signing and verifying the staged app..."
if [[ -z "$PREVIOUS_HELPER_HASH" || "$PREVIOUS_HELPER_HASH" != "$HELPER_HASH" ]]; then
  /usr/bin/codesign --force --sign - "$STAGE_PAYLOAD/build/LightTableVision"
  /usr/bin/codesign --force --sign - "$STAGE_PAYLOAD/build/LightTableEnhance"
fi
if [[ -z "$PREVIOUS_ENGINE_HASH" || "$PREVIOUS_ENGINE_HASH" != "$ENGINE_HASH" ]]; then
  /usr/bin/codesign --force --sign - "$STAGE_PAYLOAD/engine/lighttable-engine"
fi
/usr/bin/codesign --force --sign - "$STAGE_CONTENTS/MacOS/lighttable-cli"
/usr/bin/codesign --force --sign - "$STAGE_APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGE_APP"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE_PAYLOAD" \
  "$STAGE_CONTENTS/Resources/Python/bin/python3.13" -c \
  'import catalog, film_pipeline, server; print("Packaged Python import smoke: OK")'
/usr/bin/codesign --verify --deep --strict "$STAGE_APP"
"$STAGE_CONTENTS/Resources/Python/bin/python3.13" \
  "$ROOT/scripts/native-app-smoke.py" --app "$STAGE_APP" --layer package

if [[ "$MODE" == "build-only" ]]; then
  OUTPUT_APP="$BUILD_ROOT/$PRODUCT_NAME.app"
  /bin/rm -rf "$OUTPUT_APP"
  /bin/mv "$STAGE_APP" "$OUTPUT_APP"
  trap - EXIT
  /bin/rm -rf "$STAGE_ROOT"
  echo "Built and verified: $OUTPUT_APP"
  exit 0
fi

OLD_REVISION="$(/usr/libexec/PlistBuddy -c 'Print :LightTableSourceRevision' \
  "$INSTALL_APP/Contents/Info.plist" 2>/dev/null || echo unknown)"
BACKUP_APP="${HOME}/.Trash/$PRODUCT_NAME.app.pre-${OLD_REVISION:0:7}-$BUILD_NUMBER"
if [[ -e "$BACKUP_APP" ]]; then
  BACKUP_APP="$BACKUP_APP-$$"
fi

echo "Installing the verified bundle and preserving the previous app in Trash..."
if ! /bin/mv "$INSTALL_APP" "$BACKUP_APP"; then
  echo "Could not move the previous app to $BACKUP_APP" >&2
  exit 1
fi
if ! /bin/mv "$STAGE_APP" "$INSTALL_APP"; then
  echo "Install failed; restoring the previous app" >&2
  /bin/mv "$BACKUP_APP" "$INSTALL_APP"
  exit 1
fi

trap - EXIT
/bin/rm -rf "$STAGE_ROOT"
/usr/bin/codesign --verify --deep --strict "$INSTALL_APP"
echo "Updated: $INSTALL_APP"
echo "Rollback copy: $BACKUP_APP"
echo "Source: $SOURCE_REVISION (tree $SOURCE_TREE_HASH, dirty=$SOURCE_DIRTY)"
