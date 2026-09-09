#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 /path/to/LightTable.app /path/to/output.dmg" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The builder writes Finder metadata directly and never opens a Finder window.
uv run --no-project --with-requirements "$ROOT/scripts/dmg/requirements.txt" \
  python "$ROOT/scripts/dmg/build.py" "$1" "$2"
