#!/usr/bin/env bash
# This runs only on an ephemeral runner, inside Xvfb and a private D-Bus session.
set -euo pipefail
common="$HOME/snap/lighttable/common"
mkdir -p "$common/acceptance" "$common/acceptance-evidence"
cp scripts/linux/{desktop-acceptance,snap-desktop-acceptance}.py "$common/acceptance/"
cp "$XAUTHORITY" "$common/acceptance/Xauthority"
export XAUTHORITY="$common/acceptance/Xauthority"
children=()
cleanup() { for child in "${children[@]}"; do kill "$child" 2>/dev/null || true; done; }
trap cleanup EXIT
openbox > "$common/acceptance-evidence/openbox.log" 2>&1 & children+=("$!")
(
  for attempt in $(seq 1 300); do
    for number in 1 2; do
      checkpoint="$common/acceptance-evidence/capture-$number"
      if [[ -f "$checkpoint.request" && ! -f "$checkpoint.done" ]]; then
        import -window root "$checkpoint.png"
        touch "$checkpoint.done"
      fi
    done
    sleep 1
  done
) & children+=("$!")
timeout 270s snap run --shell lighttable -c \
  'exec "$SNAP/LightTable/Python/bin/python3" -B "$SNAP_USER_COMMON/acceptance/snap-desktop-acceptance.py"'
