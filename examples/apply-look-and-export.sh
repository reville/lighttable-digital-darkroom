#!/bin/sh
set -eu
if [ "$#" -lt 2 ]; then
  printf 'usage: %s PRESET DESTINATION [PHOTO...]\n' "$0" >&2
  exit 2
fi
preset=$1
destination=$2
shift 2
lighttable presets apply "$preset" "$@"
lighttable export run "$@" --destination "$destination"
