#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Build LightTable.app — a native shell around the local Python render server.
# The Python venv stays in this project directory; the bundle points at it.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
APP="build/LightTable.app"
BUNDLE_IDENTIFIER="${LIGHTTABLE_BUNDLE_IDENTIFIER:-com.reville.lighttable}"

# Keep Metal, compiled shaders, and decoded inputs resident across requests.
# The server discovers this release binary directly from rust-engine/target.
if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust/Cargo is required to build the resident GPU engine." >&2
  exit 1
fi
cargo build --release --manifest-path rust-engine/Cargo.toml

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- icon -------------------------------------------------------------------
# Keep the known-good compiled icon as a source asset. macOS 26's iconutil
# rejects otherwise valid legacy iconsets, so rebuilding it here can destroy a
# working app bundle before Swift compilation even begins.
ICON="build/LightTable.icns"
if [[ ! -f "$ICON" ]]; then
  echo "Missing $ICON" >&2
  exit 1
fi
cp "$ICON" "$APP/Contents/Resources/LightTable.icns"

# --- native preview shader -------------------------------------------------
# Keep source compilation inside Metal at runtime. That makes local builds
# work with the normal macOS SDK even when Xcode's optional offline Metal
# toolchain component is not installed.
cp app/NativePreview.metal "$APP/Contents/Resources/NativePreview.metal"

# --- Info.plist -------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>LightTable</string>
  <key>CFBundleDisplayName</key><string>LightTable</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_IDENTIFIER</string>
  <key>CFBundleURLTypes</key><array><dict>
    <key>CFBundleURLName</key><string>org.lighttable.preset</string>
    <key>CFBundleURLSchemes</key><array><string>lighttable</string></array>
    <key>CFBundleTypeRole</key><string>Viewer</string>
  </dict></array>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>LightTable</string>
  <key>CFBundleIconFile</key><string>LightTable</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSPhotoLibraryUsageDescription</key><string>LightTable reads your Photos library to copy original photos into your LightTable import folder. Your Photos library is never changed.</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 LightTable contributors. Free and open source under GNU GPL v3.</string>
  <key>NSAppTransportSecurity</key><dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict></plist>
PLIST

# A developer shell may be copied out of build/. Preserve the checkout it was
# built against instead of relying on a historical folder-name fallback.
/usr/libexec/PlistBuddy -c "Add :LightTableProjectDir string $PROJECT" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LightTableSourceRevision string $(git rev-parse HEAD)" "$APP/Contents/Info.plist"

# Record the exact native source inputs; review runs refuse stale developer shells.
NATIVE_SOURCE_DIGEST="$(cat app/main.swift app/NativePreview.swift app/DiagnosticReports.swift | shasum -a 256 | cut -d ' ' -f 1)"
/usr/libexec/PlistBuddy -c "Add :LightTableNativeSourceDigest string $NATIVE_SOURCE_DIGEST" "$APP/Contents/Info.plist"

# --- binary -----------------------------------------------------------------
SWIFT_CACHE="${TMPDIR:-/tmp}/lighttable-swift-module-cache"
CLANG_CACHE="${TMPDIR:-/tmp}/lighttable-clang-module-cache"
mkdir -p "$SWIFT_CACHE" "$CLANG_CACHE"
export SWIFT_MODULECACHE_PATH="$SWIFT_CACHE"
export CLANG_MODULE_CACHE_PATH="$CLANG_CACHE"
swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -o "$APP/Contents/MacOS/LightTable" \
  app/main.swift app/NativePreview.swift app/DiagnosticReports.swift
cp lighttable "$APP/Contents/MacOS/lighttable-cli"
chmod 755 "$APP/Contents/MacOS/lighttable-cli"

# The optional local photo index uses Apple's purpose-built Vision framework.
# Keep this helper separate from both the render engine and the web UI so the
# feature remains removable and inert when it is disabled.
swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -o "build/LightTableVision" \
  film_lab_ai/vision_helper.swift
cp "build/LightTableVision" "$APP/Contents/MacOS/LightTableVision"

# The Enhance helper runs Core ML. A developer build bundles the converted
# denoise package when present and otherwise reports the capability unavailable.
swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -o "build/LightTableEnhance" \
  film_lab_ai/enhance_helper.swift
cp "build/LightTableEnhance" "$APP/Contents/MacOS/LightTableEnhance"

# Bundle a converted, provenance-indexed denoise package when it has been
# prepared locally. Release builds require it; developer builds remain usable
# without it and report the feature unavailable.
if [[ -d "scripts/models/denoise.mlpackage" && -f "scripts/models/models.json" ]]; then
  mkdir -p "$APP/Contents/Resources/models"
  cp -R "scripts/models/denoise.mlpackage" "$APP/Contents/Resources/models/"
  cp "scripts/models/models.json" "$APP/Contents/Resources/models/"
  for LICENSE_FILE in scripts/models/SCUNet-*-LICENSE.txt; do
    [[ -f "$LICENSE_FILE" ]] && cp "$LICENSE_FILE" "$APP/Contents/Resources/models/"
  done
fi

python3 scripts/native_localization_sources.py --bundle "$APP"

codesign --force --deep --sign - "$APP"

echo "Built: $PROJECT/$APP"
