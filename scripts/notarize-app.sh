#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/LightTable.app" >&2
  exit 2
fi

: "${APPLE_ID:?APPLE_ID is required}"
: "${APPLE_TEAM_ID:?APPLE_TEAM_ID is required}"
: "${APPLE_APP_SPECIFIC_PASSWORD:?APPLE_APP_SPECIFIC_PASSWORD is required}"

APP="$1"
if [[ ! -d "$APP/Contents" ]]; then
  echo "Not an application bundle: $APP" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d /tmp/lighttable-notarization.XXXXXX)"
trap '/bin/rm -rf "$WORK_DIR"' EXIT
ARCHIVE="$WORK_DIR/LightTable.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"

xcrun notarytool submit "$ARCHIVE" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_SPECIFIC_PASSWORD" \
  --wait
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
