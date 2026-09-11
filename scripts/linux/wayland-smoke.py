#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Run the native desktop smoke on a private headless Weston compositor."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def stop(process: subprocess.Popen, *, group: bool = False) -> None:
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or not shutil.which("weston") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        parser.error("Run on Linux with Weston installed under dbus-run-session")
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    with tempfile.TemporaryDirectory(prefix="lighttable-wayland-smoke-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        environment = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "XAUTHORITY", "WAYLAND_SOCKET")}
        environment.update({"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "lighttable-test",
                            "GDK_BACKEND": "wayland", "LIBGL_ALWAYS_SOFTWARE": "1"})
        log = root / "weston.log"
        weston = subprocess.Popen([
            "weston", "--no-config", "--backend=headless-backend.so", "--renderer=pixman",
            "--socket=lighttable-test", "--width=1280", "--height=900", "--idle-time=0", f"--log={log}",
        ], env=environment, start_new_session=True)
        smoke = None
        try:
            deadline = time.monotonic() + 8
            while not (runtime / "lighttable-test").is_socket():
                if weston.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Headless Weston did not create its Wayland socket")
                time.sleep(0.05)
            # The private bus predates the compositor. Portal processes started
            # by D-Bus need this socket too; update only this bus, not systemd's
            # user environment or any desktop configuration.
            subprocess.run([
                "dbus-update-activation-environment", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "GDK_BACKEND",
            ], env=environment, check=True, timeout=5)
            smoke = subprocess.Popen([
                sys.executable, "-B", str(Path(__file__).with_name("desktop-smoke.py")),
                str(args.bundle.resolve()), "--backend", "wayland", "--timeout", "90",
            ], env=environment)
            code = smoke.wait(timeout=110)
            if code:
                raise RuntimeError(f"Native Wayland desktop smoke failed with status {code}")
        except Exception:
            if log.exists():
                print(log.read_text(errors="replace")[-4000:], file=sys.stderr)
            raise
        finally:
            if smoke is not None and smoke.poll() is None:
                stop(smoke)
            stop(weston, group=True)
            # The Documents portal can mount FUSE beneath this private runtime.
            # Release our mount before TemporaryDirectory removes the directory;
            # the surrounding dbus-run-session ends its portal processes next.
            document_mount = runtime / "doc"
            if os.path.ismount(document_mount):
                unmount = shutil.which("fusermount3") or shutil.which("fusermount")
                if not unmount:
                    raise RuntimeError(f"Cannot unmount the private Documents portal at {document_mount}")
                subprocess.run([unmount, "-u", str(document_mount)], check=True, timeout=5)


if __name__ == "__main__":
    main()
