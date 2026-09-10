#!/usr/bin/env bash
# Only for the ephemeral CI desktop; never run this against a personal session.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo 'Use an ephemeral GitHub Actions runner'; exit 2; }
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
export XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=x11
common="$HOME/snap/lighttable/common"
mkdir -p "$common/portal-evidence" "$common/acceptance" "$HOME/.config/xdg-desktop-portal"
cp scripts/linux/{desktop-acceptance,snap-portal-acceptance}.py "$common/acceptance/"
cp "$XAUTHORITY" "$common/acceptance/Xauthority"
export XAUTHORITY="$common/acceptance/Xauthority"
printf '[preferred]\ndefault=gtk\n' > "$HOME/.config/xdg-desktop-portal/portals.conf"
dbus-update-activation-environment --systemd DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
systemctl --user restart xdg-desktop-portal-gtk.service xdg-desktop-portal.service
children=()
cleanup() { for child in "${children[@]}"; do kill "$child" 2>/dev/null || true; done; }
trap cleanup EXIT
openbox > "$common/portal-evidence/openbox.log" 2>&1 & children+=("$!")
(
  shopt -s nullglob
  selected=false
  for attempt in $(seq 1 240); do
    if [[ "$selected" == false ]]; then
      window=$(xdotool search --onlyvisible --name '^Choose a photo folder$' 2>/dev/null | head -1 || true)
      if [[ -n "$window" ]]; then
        import -window root "$common/portal-evidence/folder-chooser.png"
        xdotool windowactivate --sync "$window" key --clearmodifiers ctrl+l
        xdotool type --clearmodifiers --delay 20 '/media/lighttable-portal/photos/'
        xdotool key --clearmodifiers Return
        sleep 1
        xdotool key --clearmodifiers alt+o
        selected=true
      fi
    fi
    for request in "$common/portal-evidence/"*.request; do
      checkpoint="${request%.request}"
      if [[ ! -f "$checkpoint.done" ]]; then
        import -window root "$checkpoint.png"
        touch "$checkpoint.done"
      fi
    done
    if [[ "$attempt" == 30 || "$attempt" == 120 ]]; then
      import -window root "$common/portal-evidence/diagnostic-$attempt.png"
    fi
    sleep 1
  done
) & children+=("$!")
timeout 235s snap run --shell lighttable -c \
  'exec "$SNAP/LightTable/Python/bin/python3" -B "$SNAP_USER_COMMON/acceptance/snap-portal-acceptance.py"'
