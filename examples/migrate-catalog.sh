#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
request=${1:?JSON request file with a catalog path is required}
lighttable import catalog-inspect --body "$request" --json
lighttable import catalog-run --body "$request" --json
