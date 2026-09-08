#!/usr/bin/env python3
"""Launch the actual GTK/WebKit desktop and require its web UI to render a photo.

Run with the packaged Python under dbus-run-session and Xvfb or Wayland. This checks
native startup and the UI bridge; it does not certify hardware GPU or display color.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(port: int, route: str, body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-LightTable-Token"] = token
    query = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=data, headers=headers)
    with urllib.request.urlopen(query, timeout=3) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--backend", choices=("x11", "wayland"), default="x11")
    parser.add_argument("--timeout", type=int, default=105)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    display_variable = "WAYLAND_DISPLAY" if args.backend == "wayland" else "DISPLAY"
    if (sys.platform != "linux" or not os.environ.get(display_variable)
            or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")):
        parser.error(f"Run on Linux with a private D-Bus session and {display_variable}")
    if not 10 <= args.timeout <= 105:
        parser.error("--timeout must be between 10 and 105 seconds")
    wayland_socket = None
    if args.backend == "wayland":
        wayland_socket = Path(os.environ["WAYLAND_DISPLAY"])
        if not wayland_socket.is_absolute():
            wayland_socket = Path(os.environ["XDG_RUNTIME_DIR"]) / wayland_socket
        if not wayland_socket.is_socket():
            parser.error("WAYLAND_DISPLAY does not point to a Wayland socket")
    shell = bundle / "bin/lighttable-desktop"
    if not shell.is_file():
        parser.error("The extracted Linux bundle is incomplete")
    # The CI watchdog must still enter the process-group cleanup below.
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="lighttable-desktop-smoke-") as temporary:
        root = Path(temporary)
        photos = root / "photos"
        photos.mkdir()
        Image.linear_gradient("L").resize((160, 120)).convert("RGB").save(photos / "smoke.jpg")
        config = root / "config/lighttable"
        config.mkdir(parents=True)
        (config / "prefs.json").write_text(json.dumps({
            "firstRunSetup": {"version": 1, "status": "completed", "source": "folder"},
            "allowAutomation": True, "viewMode": "detail",
        }))
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LIGHTTABLE_")}
        environment.update({
            "XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(runtime), "LIGHTTABLE_DIR": str(photos),
            "LIGHTTABLE_INSTANCE_DIR": str(root / "instances"), "LIGHTTABLE_WATCH": "0",
            "NUMBA_CACHE_DIR": str(root / "compiled"), "MPLCONFIGDIR": str(root / "matplotlib"),
            "SPEKTRAFILM_BACKEND": "cpu", "LIBGL_ALWAYS_SOFTWARE": "1", "GDK_BACKEND": args.backend,
        })
        if wayland_socket:
            # Preserve the compositor connection while isolating LightTable's
            # own XDG runtime files; libwayland accepts an absolute socket path.
            environment["WAYLAND_DISPLAY"] = str(wayland_socket)
            environment.pop("DISPLAY", None)
            environment.pop("XAUTHORITY", None)
        log_path = root / "desktop.log"
        state, phase = {}, "server registration"
        deadline = time.monotonic() + args.timeout
        with log_path.open("w") as log:
            process = subprocess.Popen([str(shell)], env=environment, stdout=log, stderr=log, start_new_session=True)
            try:
                instance, selected = None, False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Desktop exited with {process.returncode} while waiting for {phase}")
                    if instance is None:
                        for path in (root / "instances").glob("[0-9]*.json"):
                            try:
                                candidate = json.loads(path.read_text())
                                if candidate.get("folder") == str(photos) and candidate.get("port"):
                                    instance = candidate
                                    break
                            except (OSError, ValueError):
                                pass
                    if instance:
                        try:
                            port = int(instance["port"])
                            phase = "HTTP health and native UI connection"
                            health = request(port, "/api/health")
                            if not health.get("ok") or health.get("folder") != str(photos):
                                raise RuntimeError("Desktop server opened an unexpected library")
                            state = request(port, "/api/ui/state")
                            if state.get("client") and state.get("age", 999) < 10:
                                phase = "photo rendering in the native window"
                                render = state.get("render") or {}
                                if (state.get("current") and state["current"].endswith("smoke.jpg")
                                        and render.get("name") == state["current"]
                                        and render.get("state") == "ready"):
                                    print(json.dumps({"ok": True, "desktop": "GTK/WebKitGTK", "http": 200,
                                                      "display_backend": args.backend,
                                                      "current": state["current"], "render": render}))
                                    return
                                if not selected:
                                    images = request(port, "/api/images").get("images", [])
                                    photo = next((item for item in images if item["name"].endswith("smoke.jpg")), None)
                                    if photo:
                                        # A slow render can outlast the command response deadline;
                                        # do not repeatedly restart that same UI navigation.
                                        selected = True
                                        result = request(port, "/api/ui/command", {
                                            "command": "goto", "args": {"name": photo["name"]}, "timeout": 1,
                                        }, instance.get("token", ""))
                                        if not result.get("ok"):
                                            raise RuntimeError("The native window rejected photo navigation")
                        except (urllib.error.URLError, TimeoutError):
                            pass  # Startup and UI event registration are asynchronous.
                    time.sleep(0.2)
                raise RuntimeError(f"Desktop timed out waiting for {phase}; final UI state: {json.dumps(state)}")
            except Exception:
                for path in (log_path, root / "state/lighttable/logs/server.log"):
                    if path.exists():
                        print(f"--- {path.name} ---\n{path.read_text(errors='replace')[-4000:]}", file=sys.stderr)
                raise
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                # The shell can exit before Python finishes checkpointing and
                # recording its session. Wait for the whole group before the
                # temporary directory is removed, or cleanup races those writes.
                cleanup_deadline = time.monotonic() + 5
                while time.monotonic() < cleanup_deadline:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    main()
