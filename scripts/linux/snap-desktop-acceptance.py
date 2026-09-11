#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Run the existing precision/persistence journey inside Snap confinement.

The host captures its private Xvfb display at explicit close checkpoints. Test
helpers live in SNAP_USER_COMMON and are never added to the distributed snap.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time

spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).with_name('desktop-acceptance.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
output = Path(os.environ['SNAP_USER_COMMON']) / 'acceptance-evidence'
output.mkdir(exist_ok=True)
bundle = Path(os.environ['SNAP']) / 'LightTable'
assert os.environ['SNAP_NAME'] == 'lighttable'
assert json.loads((bundle/'installation-owner.json').read_text())['owner'] == 'snap'
original_environment = a.isolated_environment

def snap_environment(root):
    environment = original_environment(root)
    # The GNOME content snap supplies schemas through these system search paths.
    # Isolate writable user state without discarding its runtime configuration.
    for key in ('XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE'):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment

a.isolated_environment = snap_environment
original_close = a.normal_close
captures = 0

def capture_close(process, server_pid, deadline):
    global captures
    captures += 1
    checkpoint = output / f'capture-{captures}'
    checkpoint.with_suffix('.request').touch()
    capture_deadline = min(deadline, time.monotonic()+12)
    while not checkpoint.with_suffix('.done').exists():
        a.require(time.monotonic() < capture_deadline, 'Host did not capture the native window')
        time.sleep(.1)
    return original_close(process, server_pid, deadline)

a.normal_close = capture_close
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
a.run(bundle.resolve(), 'be537f2f3e2e431ae6b42af716c2a8b365f57bab', 240, output)
