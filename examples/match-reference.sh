#!/bin/sh
set -eu
if [ "$#" -lt 2 ]; then
  printf 'usage: %s REFERENCE TARGET...\n' "$0" >&2
  exit 2
fi
lighttable match-exposure run "$@" --json
