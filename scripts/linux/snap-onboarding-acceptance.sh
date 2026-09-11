#!/usr/bin/env bash
# Keyboard interaction with the real WebKit UI on an ephemeral CI display only.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo 'Use an ephemeral GitHub Actions runner'; exit 2; }
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
common="$HOME/snap/lighttable/common"
mkdir -p "$common/onboarding-evidence" "$common/acceptance"
cp scripts/linux/{desktop-acceptance,snap-onboarding-acceptance}.py "$common/acceptance/"
cp "$XAUTHORITY" "$common/acceptance/Xauthority"
export XAUTHORITY="$common/acceptance/Xauthority"
children=()
cleanup() { for child in "${children[@]}"; do kill "$child" 2>/dev/null || true; done; }
trap cleanup EXIT
openbox > "$common/onboarding-evidence/openbox.log" 2>&1 & children+=("$!")
(
  shopt -s nullglob
  for attempt in $(seq 1 320); do
    for request in "$common/onboarding-evidence/"*.request; do
      checkpoint="${request%.request}"
      if [[ ! -f "$checkpoint.done" ]]; then
        # Client registration precedes WebKit's first paint and focusPage().
        sleep 3
        import -window root "$checkpoint.png"
        case "$(basename "$checkpoint")" in
          onboarding-choices)
            window=$(xdotool search --onlyvisible --name '^LightTable — photos$' | head -1)
            xdotool windowactivate --sync "$window"
            # The pinned 0.5.0 UI includes its unavailable Apple Photos choice
            # between Lightroom and Folder. Later source versions hide it.
            xdotool key --clearmodifiers Tab Tab Tab Return
            ;;
          onboarding-folder)
            xdotool key --clearmodifiers Tab
            xdotool type --clearmodifiers --delay 10 "$common/onboarding-state/import/photos"
            xdotool key --clearmodifiers Tab Return
            ;;
          onboarding-result) xdotool key --clearmodifiers Tab Return ;;
        esac
        touch "$checkpoint.done"
      fi
    done
    sleep 1
  done
) & children+=("$!")
timeout 315s snap run --shell lighttable -c \
  'exec "$SNAP/LightTable/Python/bin/python3" -B "$SNAP_USER_COMMON/acceptance/snap-onboarding-acceptance.py"'
