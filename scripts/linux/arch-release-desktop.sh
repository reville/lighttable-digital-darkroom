#!/usr/bin/env bash
set -euo pipefail
cd /work
backend=$1
mkdir -p "evidence/$backend"
children=()
cleanup() {
  if [[ "$backend" == hyprland && -n ${XDG_RUNTIME_DIR:-} ]]; then
    cp "$XDG_RUNTIME_DIR"/hypr/*/hyprland.log evidence/hyprland/detail.log 2>/dev/null || true
  fi
  for child in "${children[@]}"; do kill "$child" 2>/dev/null || true; done
}
trap cleanup EXIT
export LIBGL_ALWAYS_SOFTWARE=1 SPEKTRAFILM_BACKEND=cpu PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 NUMBA_NUM_THREADS=2
if [[ "$backend" == x11 ]]; then
  openbox > evidence/x11/compositor.log 2>&1 & children+=("$!")
else
  export XDG_RUNTIME_DIR
  XDG_RUNTIME_DIR=$(mktemp -d)
  export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=Hyprland
  unset DISPLAY
  if [[ ${VM_GPU:-0} == 1 ]]; then
    export AQ_NO_KMS_REQUIREMENT=1 LIBSEAT_BACKEND=seatd
    for card in /sys/class/drm/card[0-9]; do
      if [[ $(basename "$(readlink "$card/device/driver")") == virtio_gpu ]]; then
        export AQ_DRM_DEVICES="/dev/dri/$(basename "$card")"
      fi
    done
    unset WAYLAND_DISPLAY
  else
  weston --backend=headless --renderer=gl --width=1440 --height=1000 --socket=wayland-host --idle-time=0 > evidence/hyprland/weston.log 2>&1 & children+=("$!")
  for attempt in $(seq 1 80); do [[ -S "$XDG_RUNTIME_DIR/wayland-host" ]] && break; sleep .25; done
  test -S "$XDG_RUNTIME_DIR/wayland-host"
  export WAYLAND_DISPLAY=wayland-host AQ_BACKEND=wayland
  fi
  printf 'monitor=,1440x1000@60,auto,1\nmisc {\n disable_hyprland_logo=true\n}\n' > /tmp/lighttable-hyprland.conf
  printf 'hl.monitor({output="", mode="1440x1000@60", position="auto", scale=1})\n' > /tmp/lighttable-hyprland.lua
  config=/tmp/lighttable-hyprland.lua
  Hyprland --config "$config" > evidence/hyprland/compositor.log 2>&1 & children+=("$!")
  for attempt in $(seq 1 100); do
    socket=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-*' ! -name wayland-host -type s -print -quit)
    ipc=$(find "$XDG_RUNTIME_DIR/hypr" -name '.socket.sock' -print -quit 2>/dev/null || true)
    if [[ -n "$socket" && -n "$ipc" ]]; then break; fi
    sleep .25
  done
  test -S "$socket" && test -S "$ipc"
  export WAYLAND_DISPLAY="$socket" HYPRLAND_INSTANCE_SIGNATURE
  HYPRLAND_INSTANCE_SIGNATURE=$(basename "$(dirname "$ipc")")
  hyprctl -j version > evidence/hyprland/version.json
  hyprctl -j monitors > evidence/hyprland/monitors.json
  hyprctl -j configerrors > evidence/hyprland/config-errors.json
  python -c 'import json; errors=json.load(open("evidence/hyprland/config-errors.json")); assert isinstance(errors,list) and not any(str(error).strip() for error in errors), errors'
fi
/opt/lighttable/Python/bin/python3 -B scripts/linux/arch-desktop-acceptance.py /opt/lighttable \
  --source be537f2f3e2e431ae6b42af716c2a8b365f57bab --backend "$backend" --film \
  --package /work/dist/lighttable-bin-0.5.0-1-x86_64.pkg.tar.zst \
  --fixtures demo-assets/cc0-raw/files --output "evidence/$backend"
