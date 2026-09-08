#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$ROOT/Python/bin/python3" -I -B "$ROOT/desktop-integration.py" install "$ROOT" "$@"
