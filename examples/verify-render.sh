#!/bin/sh
set -eu
photo=${1:?photo reference required}
destination=${2:-/tmp/lighttable-compare.png}
lighttable analyze "$photo" --json
lighttable compare "$photo" --wipe 0.5 -o "$destination"
printf 'Comparison: %s\n' "$destination" >&2
