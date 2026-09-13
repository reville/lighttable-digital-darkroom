#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Runs only inside a disposable Fedora CI container.
set -euo pipefail
cd /work

echo "==> Setting up Fedora test environment"
dnf -y install \
  rpm-build \
  python3 \
  git \
  gtk3 \
  webkit2gtk4.1 \
  xdotool \
  openblas \
  openssl \
  libglvnd-glx \
  vulkan-loader \
  lcms2 \
  xdg-utils \
  hicolor-icon-theme \
  desktop-file-utils \
  xorg-x11-server-Xvfb \
  mesa-dri-drivers \
  mesa-vulkan-drivers

useradd -m -s /bin/bash tester
echo "tester ALL=(ALL) NOPASSWD: /usr/bin/dnf" > /etc/sudoers.d/lighttable-test
chown -R tester:tester /work

# Find latest bundle or build one
BUNDLE=$(ls -1 dist/LightTable-*-linux-x86_64.tar.gz 2>/dev/null | tail -n 1 || true)
if [[ -z "$BUNDLE" ]]; then
  echo "No bundle in dist/, generating test bundle structure"
  exit 1
fi

echo "==> Generating RPM package from $BUNDLE"
runuser -u tester -- python3 scripts/linux/make-rpm-package.py "$BUNDLE" --output-dir dist/rpm --build

RPM_FILE=$(ls -1 dist/rpm/RPMS/x86_64/lighttable-*.x86_64.rpm | tail -n 1)
echo "==> Built RPM: $RPM_FILE"

echo "==> Testing installation with DNF"
dnf -y install "$RPM_FILE"

echo "==> Verifying package files and binaries"
test -x /opt/lighttable/bin/lighttable-desktop-shell
test -x /opt/lighttable/Python/bin/python3
test -x /usr/bin/lighttable
test -x /usr/bin/lighttable-desktop
test -f /usr/share/applications/app.lighttable.LightTable.desktop

echo "==> Verifying installation owner"
OWNER=$(python3 -c 'import json, Path; from pathlib import Path; print(json.loads(Path("/opt/lighttable/installation-owner.json").read_text())["owner"])')
if [[ "$OWNER" != "rpm" ]]; then
  echo "Expected owner 'rpm', got '$OWNER'"
  exit 1
fi

echo "==> Running runtime smoke test"
runuser -u tester -- bash -euo pipefail -c '
  export XDG_RUNTIME_DIR=$(mktemp -d)
  /opt/lighttable/Python/bin/python3 -B /opt/lighttable/runtime-smoke.py /opt/lighttable
'

echo "==> Testing uninstallation"
dnf -y remove lighttable
test ! -e /usr/bin/lighttable
test ! -e /usr/bin/lighttable-desktop
test ! -e /opt/lighttable

echo "==> All Fedora RPM container tests passed successfully!"
