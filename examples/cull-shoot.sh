#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
lighttable photos list --where status=pending --sort capture --jsonl
printf '%s\n' 'Review the candidates, then rate or flag the exact names.' >&2
