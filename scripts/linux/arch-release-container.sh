#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Runs only inside a disposable CI container; package signatures stay required.
set -euo pipefail
cd /work
pacman-key --init
pacman-key --populate archlinux
if [[ "$DISTRIBUTION" == omarchy ]]; then
  curl -fsSL --retry 2 --max-time 30 https://keys.openpgp.org/vks/v1/by-fingerprint/40DFB630FF42BCFFB047046CF0134EE680CAC571 -o /tmp/omarchy-key.asc
  gpg --show-keys --with-colons /tmp/omarchy-key.asc | awk -F: '$1=="fpr" {print $10}' | grep -qx 40DFB630FF42BCFFB047046CF0134EE680CAC571
  pacman-key --add /tmp/omarchy-key.asc
  pacman-key --lsign-key 40DFB630FF42BCFFB047046CF0134EE680CAC571
  printf 'Server = https://stable-mirror.omarchy.org/$repo/os/$arch\n' > /etc/pacman.d/mirrorlist
  printf '\n[omarchy]\nServer = https://pkgs.omarchy.org/stable/$arch\n' >> /etc/pacman.conf
fi
pacman -Syyuu --noconfirm
pacman -S --needed --noconfirm sudo python git base-devel namcap \
  gtk3 webkit2gtk-4.1 xdotool openblas openssl libglvnd vulkan-icd-loader lcms2 \
  xdg-utils glib2 dbus gvfs zenity xdg-desktop-portal hicolor-icon-theme desktop-file-utils \
  xorg-server-xvfb xorg-xauth openbox imagemagick hyprland weston grim mesa vulkan-swrast ttf-dejavu seatd
useradd -m -s /bin/bash tester
if [[ ${VM_GPU:-0} == 1 ]]; then
  usermod -aG video,render,seat tester
  systemctl start seatd
fi
printf 'tester ALL=(ALL) NOPASSWD: /usr/bin/pacman\n' > /etc/sudoers.d/lighttable-test
chown -R tester:tester /work
curl -fsSL --retry 2 --max-time 30 https://raw.githubusercontent.com/omacom/omarchy/0534987009061cbe2dacdde4ad564092ab698d12/themes/tokyo-night/colors.toml -o evidence/omarchy-colors.toml
sha256sum evidence/omarchy-colors.toml > evidence/omarchy-colors.sha256
pacman -Q > evidence/installed-packages.txt
cp /etc/pacman.conf /etc/pacman.d/mirrorlist evidence/
if [[ ${REUSE_PACKAGE:-0} != 1 ]]; then
runuser -u tester -- bash -euo pipefail -c '
  cd /work/packaging/linux/arch/release
  makepkg --printsrcinfo > /tmp/actual-srcinfo
  diff -u .SRCINFO /tmp/actual-srcinfo
  makepkg --noconfirm --cleanbuild
  cp lighttable-bin-*.pkg.tar.zst /work/dist/
  namcap PKGBUILD lighttable-bin-*.pkg.tar.zst > /work/evidence/namcap.txt
'
fi
runuser -u tester -- python scripts/fetch-demo-raws.py --file 01-canon-eos-80d-city-tree.CR2 --file 03-fujifilm-xq2-harbor-ferry.RAF
pacman -U --noconfirm dist/lighttable-bin-*.pkg.tar.zst
pacman -Qkk lighttable-bin > evidence/package-integrity.txt
runuser -u tester -- bash -euo pipefail -c '
  cd /work
  export XDG_RUNTIME_DIR=$(mktemp -d)
  /opt/lighttable/Python/bin/python3 -B /opt/lighttable/runtime-smoke.py /opt/lighttable > evidence/runtime.txt
  if [[ ${VM_GPU:-0} == 1 ]]; then
    timeout 1200s dbus-run-session -- bash scripts/linux/arch-release-desktop.sh hyprland
  else
    timeout 1200s xvfb-run -a --server-args="-screen 0 1440x1000x24" dbus-run-session -- bash scripts/linux/arch-release-desktop.sh x11
  fi
'
pacman -Qkk lighttable-bin > evidence/package-integrity-after.txt
sha256sum dist/*.pkg.tar.zst > dist/SHA256SUMS
