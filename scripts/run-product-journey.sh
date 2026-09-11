#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Build (when appropriate), run, screenshot, and measure one product journey.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAYER="${1:-pr}"
APP="${2:-}"
OUTPUT_DIR="${LIGHTTABLE_JOURNEY_OUTPUT_DIR:-$ROOT/build/product-journeys}"

case "$LAYER" in
  pr|raw-curated|raw-full)
    if [[ -z "$APP" ]]; then APP="$ROOT/build/LightTable.app"; fi
    if [[ "${LIGHTTABLE_JOURNEY_SKIP_BUILD:-0}" != "1" ]]; then
      (cd "$ROOT" && LIGHTTABLE_BUNDLE_IDENTIFIER="${LIGHTTABLE_JOURNEY_BUNDLE_ID:-com.reville.lighttable.product-journey}" bash build-app.sh)
    fi
    ;;
  package)
    if [[ -z "$APP" ]]; then APP="$ROOT/dist/LightTable.app"; fi
    ;;
  *)
    echo "Usage: $0 {pr|package|raw-curated|raw-full} [path-to-LightTable.app]" >&2
    exit 2
    ;;
esac

if [[ ! -d "$APP" ]]; then
  echo "Application bundle not found: $APP" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
TIMEOUT=900
if [[ "$LAYER" == "raw-curated" ]]; then TIMEOUT=3600; fi
if [[ "$LAYER" == "raw-full" ]]; then TIMEOUT=7200; fi

exec "$ROOT/scripts/native-app-smoke.py" \
  --app "$APP" \
  --layer "$LAYER" \
  --timeout "$TIMEOUT" \
  --output "$OUTPUT_DIR/$LAYER-performance.json" \
  --screenshot "$OUTPUT_DIR/$LAYER-screenshot.png"
