#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
: "${SNAP:?Run this launcher through snap run}"
: "${SNAP_USER_COMMON:?Snap user storage is unavailable}"
# Keep catalogs and compiled caches stable across revision changes. The desktop
# shell, Python server and CLI all consume the same XDG paths.
export XDG_DATA_HOME="$SNAP_USER_COMMON/data"
export XDG_CONFIG_HOME="$SNAP_USER_COMMON/config"
export XDG_CACHE_HOME="$SNAP_USER_COMMON/cache"
export XDG_STATE_HOME="$SNAP_USER_COMMON/state"
export PYTHONNOUSERSITE=1
export GTK_USE_PORTAL=1
unset PYTHONHOME PYTHONPATH
# The GNOME extension supplies GTK, WebKit and the GPU provider. These are only
# additional libraries staged by this package; never bundle a host GPU driver.
export LD_LIBRARY_PATH="$SNAP/usr/lib/x86_64-linux-gnu/openblas-pthread:$SNAP/usr/lib/x86_64-linux-gnu:$SNAP/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME"
exec "$@"
