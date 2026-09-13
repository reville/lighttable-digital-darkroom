#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Runs only inside a disposable Ubuntu/Debian CI container.
set -euo pipefail
cd /work

echo "==> Updating package repositories"
apt-get update

echo "==> Setting up Ubuntu test environment"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  dpkg-dev \
  python3 \
  git \
  gtk3 \
  webkit2gtk-4.1 \
  xdotool \
  libopenblas0 \
  openssl \
  libgl1 \
  libvulkan1 \
  liblcms2-2 \
  xdg-utils \
  hicolor-icon-theme \
  desktop-file-utils \
  xvfb

useradd -m -s /bin/bash tester
echo "tester ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/bin/dpkg" > /etc/sudoers.d/lighttable-test
chown -R tester:tester /work

BUNDLE=$(ls -1 dist/LightTable-*-linux-x86_64.tar.gz 2>/dev/null | tail -n 1 || true)
if [[ -z "$BUNDLE" ]]; then
  echo "No bundle in dist/, generating test bundle structure"
  exit 1
fi

echo "==> Generating DEB package from $BUNDLE"
runuser -u tester -- python3 scripts/linux/make-deb-package.py "$BUNDLE" --output-dir dist/deb

DEB_FILE=$(ls -1 dist/deb/lighttable_*_amd64.deb | tail -n 1)
echo "==> Built DEB: $DEB_FILE"

echo "==> Testing installation with APT"
apt-get install -y "$DEB_FILE"

echo "==> Verifying package files and binaries"
test -x /opt/lighttable/bin/lighttable-desktop-shell
test -x /opt/lighttable/Python/bin/python3
test -x /usr/bin/lighttable
test -x /usr/bin/lighttable-desktop
test -f /usr/share/applications/app.lighttable.LightTable.desktop

echo "==> Verifying installation owner"
OWNER=$(python3 -c 'import json; from pathlib import Path; print(json.loads(Path("/opt/lighttable/installation-owner.json").read_text())["owner"])')
if [[ "$OWNER" != "deb" ]]; then
  echo "Expected owner 'deb', got '$OWNER'"
  exit 1
fi

echo "==> Running runtime smoke test"
runuser -u tester -- bash -euo pipefail -c '
  export XDG_RUNTIME_DIR=$(mktemp -d)
  /opt/lighttable/Python/bin/python3 -B /opt/lighttable/runtime-smoke.py /opt/lighttable
'

echo "==> Testing uninstallation"
apt-get remove -y lighttable
test ! -e /usr/bin/lighttable
test ! -e /usr/bin/lighttable-desktop
test ! -e /opt/lighttable

echo "==> All Ubuntu DEB container tests passed successfully!"
