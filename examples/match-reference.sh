#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
if [ "$#" -lt 2 ]; then
  printf 'usage: %s REFERENCE TARGET...\n' "$0" >&2
  exit 2
fi
lighttable match-exposure run "$@" --json
