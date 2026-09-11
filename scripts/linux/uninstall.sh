#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$ROOT/Python/bin/python3" -I -B "$ROOT/desktop-integration.py" uninstall "$ROOT" "$@"
