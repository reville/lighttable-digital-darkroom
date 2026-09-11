#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# LightTable: review a photo folder through the spektrafilm pipeline.
# Usage: run.sh [photo-folder] [port]
set -euo pipefail
DIR="${1:-$HOME/Pictures}"
PORT="${2:-8321}"
cd "$(dirname "$0")"
export LIGHTTABLE_DIR="$DIR"
export LIGHTTABLE_PORT="$PORT"
export OMP_NUM_THREADS=4 NUMBA_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
echo "LightTable starting on http://127.0.0.1:$PORT"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
exec .venv/bin/python server.py
