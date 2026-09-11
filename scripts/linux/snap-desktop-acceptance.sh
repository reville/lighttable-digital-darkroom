#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# This runs only on an ephemeral runner, inside Xvfb with its systemd user bus.
set -euo pipefail
mode="${1:-precision}"
case "$mode" in precision|before|after) ;; *) echo 'Use precision, before, or after' >&2; exit 2;; esac
# Keep the real systemd user bus reachable after the tests isolate XDG state.
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
common="$HOME/snap/lighttable/common"
mkdir -p "$common/acceptance" "$common/acceptance-evidence"
cp scripts/linux/{desktop-acceptance,snap-desktop-acceptance}.py "$common/acceptance/"
if [[ "$mode" != precision ]]; then
  cp scripts/linux/snap-raw-acceptance.py "$common/acceptance/"
  if [[ "$mode" == before ]]; then
    mkdir -p "$common/acceptance/fixtures"
    cp demo-assets/cc0-raw/files/{01-canon-eos-80d-city-tree.CR2,03-fujifilm-xq2-harbor-ferry.RAF} "$common/acceptance/fixtures/"
  fi
fi
cp "$XAUTHORITY" "$common/acceptance/Xauthority"
export XAUTHORITY="$common/acceptance/Xauthority"
children=()
cleanup() { for child in "${children[@]}"; do kill "$child" 2>/dev/null || true; done; }
trap cleanup EXIT
openbox > "$common/acceptance-evidence/openbox.log" 2>&1 & children+=("$!")
(
  shopt -s nullglob
  for attempt in $(seq 1 1020); do
    if [[ "$attempt" == 30 ]]; then
      import -window root "$common/acceptance-evidence/$mode-startup.png"
    fi
    for request in "$common/acceptance-evidence/"*.request; do
      checkpoint="${request%.request}"
      if [[ ! -f "$checkpoint.done" ]]; then
        import -window root "$checkpoint.png"
        touch "$checkpoint.done"
      fi
    done
    sleep 1
  done
) & children+=("$!")
if [[ "$mode" == precision ]]; then
  timeout 270s snap run --shell lighttable -c \
    'exec "$SNAP/LightTable/Python/bin/python3" -B "$SNAP_USER_COMMON/acceptance/snap-desktop-acceptance.py"'
else
  export LIGHTTABLE_SNAP_TEST_PHASE="$mode"
  export LIGHTTABLE_SNAP_REOPEN_COUNT=1
  if [[ "$mode" == after ]]; then export LIGHTTABLE_SNAP_REOPEN_COUNT=8; fi
  timeout 1000s snap run --shell lighttable -c \
    'exec "$SNAP/LightTable/Python/bin/python3" -B "$SNAP_USER_COMMON/acceptance/snap-raw-acceptance.py" --phase "$LIGHTTABLE_SNAP_TEST_PHASE" --reopen-count "$LIGHTTABLE_SNAP_REOPEN_COUNT"'
fi
