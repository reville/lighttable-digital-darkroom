#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Resolve workspace convenience symlinks before locating the implementation.
set -euo pipefail
SCRIPT="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT" ]]; do
  DIRECTORY="$(cd -P "$(dirname "$SCRIPT")" && pwd)"
  SCRIPT="$(readlink "$SCRIPT")"
  [[ "$SCRIPT" = /* ]] || SCRIPT="$DIRECTORY/$SCRIPT"
done
DIRECTORY="$(cd -P "$(dirname "$SCRIPT")" && pwd)"
exec python3 "$DIRECTORY/personal_build.py" "$@"
