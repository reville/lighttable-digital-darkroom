#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
request=${1:?JSON file containing a watch object is required}
lighttable watch add --body "$request" --json
