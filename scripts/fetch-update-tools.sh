#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="2.9.6"
EXPECTED_SHA256="52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192"
CACHE="$ROOT/.build/update-tools/$VERSION"
ARCHIVE="$CACHE/Sparkle-$VERSION.tar.xz"
EXTRACTED="$CACHE/extracted"

mkdir -p "$CACHE" "$EXTRACTED"
if [[ ! -f "$ARCHIVE" ]]; then
  curl -fsSL --retry 3 \
    "https://github.com/sparkle-project/Sparkle/releases/download/$VERSION/Sparkle-$VERSION.tar.xz" \
    -o "$ARCHIVE"
fi

ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "Update-tools archive checksum mismatch" >&2
  exit 1
fi

if [[ ! -x "$EXTRACTED/bin/generate_appcast" ]]; then
  tar -xJf "$ARCHIVE" -C "$EXTRACTED"
fi

printf '%s\n' "$EXTRACTED"
