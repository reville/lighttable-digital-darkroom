#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 /path/to/LightTable.app [signing-identity]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$1"
IDENTITY="${2:--}"

if [[ ! -d "$APP/Contents" ]]; then
  echo "Not an application bundle: $APP" >&2
  exit 1
fi

SIGN_OPTIONS=()
if [[ "$IDENTITY" != "-" ]]; then
  SIGN_OPTIONS+=(--options runtime)
  if [[ "$IDENTITY" == Developer\ ID\ Application:* ]]; then
    SIGN_OPTIONS+=(--timestamp)
  else
    SIGN_OPTIONS+=(--timestamp=none)
  fi
fi

sign_path() {
  local TARGET="$1"
  shift
  if [[ ${#SIGN_OPTIONS[@]} -gt 0 ]]; then
    /usr/bin/codesign --force --sign "$IDENTITY" \
      "${SIGN_OPTIONS[@]}" "$@" "$TARGET"
  else
    /usr/bin/codesign --force --sign "$IDENTITY" "$@" "$TARGET"
  fi
}

# Wheels and the film engines contain nested Mach-O code. Sign every leaf
# before sealing the framework and application bundles around them.
while IFS= read -r -d '' CANDIDATE; do
  # Signing the main executable seals its containing app as well. Defer it
  # until the CLI and the rest of the nested code have their own signatures.
  if [[ "$CANDIDATE" == "$APP/Contents/MacOS/LightTable" ]]; then
    continue
  fi
  if /usr/bin/file -b "$CANDIDATE" | /usr/bin/grep -q 'Mach-O'; then
    sign_path "$CANDIDATE"
  fi
done < <(/usr/bin/find "$APP/Contents" -type f -print0)

APP_CLI="$APP/Contents/MacOS/lighttable-cli"
if [[ -f "$APP_CLI" ]]; then
  sign_path "$APP_CLI"
fi

PYTHON="$APP/Contents/Resources/Python/bin/python3.13"
if [[ -x "$PYTHON" && "$IDENTITY" != "-" ]]; then
  sign_path "$PYTHON" \
    --entitlements "$ROOT/release/python-runtime.entitlements"
fi

FRAMEWORK="$APP/Contents/Frameworks/Sparkle.framework"
if [[ -d "$FRAMEWORK" ]]; then
  sign_path "$FRAMEWORK" --deep
fi

sign_path "$APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
