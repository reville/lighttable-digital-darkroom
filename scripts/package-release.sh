#!/bin/bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 /path/to/LightTable.app version [release-notes.md]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$1"
VERSION="$2"
RELEASE_NOTES="${3:-}"
OUTPUT_DIR="${LIGHTTABLE_OUTPUT_DIR:-$ROOT/dist}"
APPCAST_DIR="$OUTPUT_DIR/appcast"
ARCHIVE_NAME="LightTable-$VERSION-macos-arm64.zip"
ARCHIVE="$APPCAST_DIR/$ARCHIVE_NAME"
DMG="$OUTPUT_DIR/LightTable-$VERSION-macos-arm64.dmg"
TAG="v$VERSION"

if [[ ! -d "$APP/Contents" ]]; then
  echo "Not an application bundle: $APP" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR" "$APPCAST_DIR"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"

if [[ -n "$RELEASE_NOTES" ]]; then
  /usr/bin/ditto "$RELEASE_NOTES" "$APPCAST_DIR/LightTable-$VERSION.md"
fi

UPDATE_TOOLS="$("$ROOT/scripts/fetch-update-tools.sh")"
GENERATE_ARGS=(
  --download-url-prefix "https://github.com/reville/lighttable-digital-darkroom/releases/download/$TAG/"
  --link "https://github.com/reville/lighttable-digital-darkroom"
  --maximum-versions 1
  --maximum-deltas 0
  --embed-release-notes
  -o "$APPCAST_DIR/appcast.xml"
)

if [[ -n "${SPARKLE_PRIVATE_KEY:-}" ]]; then
  printf '%s' "$SPARKLE_PRIVATE_KEY" | \
    "$UPDATE_TOOLS/bin/generate_appcast" \
      "${GENERATE_ARGS[@]}" --ed-key-file - "$APPCAST_DIR"
else
  "$UPDATE_TOOLS/bin/generate_appcast" \
    "${GENERATE_ARGS[@]}" "$APPCAST_DIR"
fi

"$ROOT/scripts/package-dmg.sh" "$APP" "$DMG"

echo "Created update archive: $ARCHIVE"
echo "Created signed appcast: $APPCAST_DIR/appcast.xml"
echo "Created installer image: $DMG"
