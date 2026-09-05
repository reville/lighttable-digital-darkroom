#!/bin/sh
set -eu
body=${1:?JSON file containing a keywords array is required}
shift
lighttable ai-index results --json
lighttable keywords set "$@" --body "$body" --json
