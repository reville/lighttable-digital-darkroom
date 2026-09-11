#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the actual installed sandbox, including private POSIX shared memory."""
from __future__ import annotations

import argparse
import configparser
import ctypes
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import uuid

APP_ID = "app.lighttable.LightTable"
BUNDLE = Path("/app/LightTable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--denied-host-file", type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == "linux" and os.environ.get("FLATPAK_ID") == APP_ID
    info = configparser.ConfigParser()
    info.read("/.flatpak-info")
    permissions = info["Context"]
    assert not permissions.get("filesystems", "").strip(";"), dict(permissions)
    assert "ipc" not in permissions.get("shared", "").split(";"), dict(permissions)
    try:
        args.denied_host_file.read_bytes()
    except (PermissionError, FileNotFoundError):
        pass
    else:
        raise AssertionError("The sandbox could read the ungranted host fixture")
    # Parent and renderer are in the same sandbox: host IPC/shm permissions
    # should not be required for the resident RAW/native transfer contract.
    libc = ctypes.CDLL(None, use_errno=True)
    name = "/lighttable-flatpak-" + uuid.uuid4().hex
    fd = libc.shm_open(name.encode(), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    assert fd >= 0, ctypes.get_errno()
    try:
        os.ftruncate(fd, 4096)
        with mmap.mmap(fd, 4096) as shared:
            shared[:8] = b"LTRItest"
            code = """import ctypes,mmap,os,sys
libc=ctypes.CDLL(None)
fd=libc.shm_open(sys.argv[1].encode(),os.O_RDONLY,0)
assert fd>=0
with mmap.mmap(fd,4096,access=mmap.ACCESS_READ) as data: assert data[:8]==b'LTRItest'
os.close(fd)
"""
            subprocess.run([sys.executable, "-c", code, name], check=True, timeout=20)
    finally:
        os.close(fd)
        assert libc.shm_unlink(name.encode()) == 0
    engine = BUNDLE / "Resources/LightTable/engine/lighttable-engine"
    result = subprocess.run([str(engine)], input='{"id":1,"command":"ping"}\n', text=True,
        capture_output=True, check=True, timeout=30,
        env=dict(os.environ, SPEKTRAFILM_BACKEND="wgpu"))
    adapter = json.loads(result.stdout)
    assert adapter["ok"], adapter
    # Exercise the installed wrapper as well as the unchanged portable runtime.
    subprocess.run(["/app/bin/lighttable-cli", "--help"], check=True, timeout=30)
    subprocess.run([sys.executable, "-B", str(BUNDLE / "runtime-smoke.py"), str(BUNDLE)],
                   check=True, timeout=300)
    print(json.dumps({"ok": True, "host_file_denied": True, "private_shared_memory": True,
                      "runtime_smoke": True, "installed_cli": True, "adapter_probe": adapter,
                      "hardware_performance": "not measured"}))


if __name__ == "__main__":
    main()
