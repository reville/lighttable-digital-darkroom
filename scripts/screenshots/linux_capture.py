#!/usr/bin/env python3
"""Capture the packaged GTK/WebKitGTK app on an isolated native 2x X11 display.

Run under dbus-run-session and Xvfb with GDK_SCALE=2. This captures actual
Linux application window pixels (excluding window-manager decorations); it never creates browser
mockups or resizes a master. The shared plan supplies ten checked Pexels photos.
"""
from __future__ import annotations

import photo_sources
import shot_geometry
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageCms

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=30, **kwargs).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api(instance, route, body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{instance['port']}{route}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-LightTable-Token": instance["token"]})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # A shell may exit before its server has checkpointed its private catalog.
    for _ in range(100):
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--photo-root", "--raw-root", dest="photo_root", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--only", help="Recapture one scene while retaining the other raw captures")
    args = parser.parse_args()
    if (os.uname().sysname != "Linux" or os.environ.get("GDK_SCALE") != "2"
            or not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")):
        parser.error("Use a private Linux X11 display and D-Bus session with GDK_SCALE=2")
    for tool in ("xdotool", "xwininfo", "import"):
        if not shutil.which(tool):
            parser.error(f"Install the native capture dependency: {tool}")
    bundle = args.bundle.resolve()
    build = json.loads((bundle / "build-manifest.json").read_text())
    if (build.get("platform") != "linux" or build.get("source_dirty") is not False
            or not re.fullmatch(r"[0-9a-f]{40}", build.get("source_revision", ""))):
        parser.error("A clean, revision-identified native Linux package is required")
    plan = json.loads((ROOT / "manifest.json").read_text())
    shots = plan["shots"]
    if args.only and args.only not in {shot["id"] for shot in shots}:
        parser.error("Unknown scene")
    default_root, default_manifest = photo_sources.locations(plan, ROOT.parents[1])
    photo_root = args.photo_root or default_root
    sources = photo_sources.records(args.provenance or default_manifest)
    photo_sources.verify(plan, photo_root, sources)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda number, _frame: exit(128 + number))
    with tempfile.TemporaryDirectory(prefix="lighttable-linux-gallery-") as directory:
        temporary = Path(directory)
        photos = temporary / "Pexels photos"
        photos.mkdir()
        for name in sources:
            shutil.copy2(photo_root / name, photos / name)
        config = temporary / "config/lighttable"
        config.mkdir(parents=True)
        (config / "prefs.json").write_text(json.dumps({
            "firstRunSetup": {"version": 1, "status": "completed", "source": "folder"},
            "allowAutomation": True, "viewMode": "detail", "activePane": "filmPane",
            "pw": "3000", "engine": "rs", "sort": "capture",
        }))
        (config / "desktop-settings.json").write_text(json.dumps({
            "window": {"width": 1660, "height": 1000, "maximized": False}}))
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LIGHTTABLE_")}
        environment.update({
            "XDG_CONFIG_HOME": str(temporary / "config"), "XDG_DATA_HOME": str(temporary / "data"),
            "XDG_CACHE_HOME": str(temporary / "cache"), "XDG_STATE_HOME": str(temporary / "state"),
            "LIGHTTABLE_DIR": str(photos), "LIGHTTABLE_INSTANCE_DIR": str(temporary / "instances"),
            "LIGHTTABLE_CATALOG_FILE": str(temporary / "catalog.sqlite3"), "LIGHTTABLE_CATALOG_MIRROR": "0",
            "LIGHTTABLE_WATCH": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "NUMBA_CACHE_DIR": str(temporary / "numba"), "MPLCONFIGDIR": str(temporary / "matplotlib"),
            "GDK_BACKEND": "x11", "GTK_CSD": "1", "GTK_THEME": "Adwaita:dark",
            # VM captures are visual evidence, not hardware-performance benchmarks.
            "SPEKTRAFILM_BACKEND": "cpu", "LIBGL_ALWAYS_SOFTWARE": "1",
        })
        with (output / "desktop.log").open("w") as log:
            process = subprocess.Popen([str(bundle / "bin/lighttable-desktop")], env=environment,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                instance = None
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Native app exited; see desktop.log")
                    for path in (temporary / "instances").glob("*.json"):
                        candidate = json.loads(path.read_text())
                        if candidate.get("folder") == str(photos):
                            try:
                                state = api(candidate, "/api/ui/state")
                                if state.get("client") and state.get("age", 999) < 10:
                                    instance = candidate
                                    break
                            except (OSError, ValueError):
                                pass
                    if instance:
                        break
                    time.sleep(0.5)
                if not instance:
                    raise RuntimeError("Native window did not connect within 240 seconds")
                names = {item["displayName"]: item["name"]
                         for item in api(instance, "/api/images?limit=100")["images"]}
                window = run("xdotool", "search", "--onlyvisible", "--pid", process.pid,
                             "--name", "^LightTable").splitlines()
                if len(window) != 1:
                    raise RuntimeError(f"Expected one PID-scoped native window, found {window}")
                window = window[0]
                geometry = run("xwininfo", "-id", window)
                width = int(re.search(r"Width: (\d+)", geometry)[1])
                height = int(re.search(r"Height: (\d+)", geometry)[1])
                if width < 3320 or height < 2000:
                    raise RuntimeError(f"Native window is smaller than the 2x capture target: {width}x{height}")
                run("xdotool", "windowactivate", "--sync", window)

                def command(name, arguments=None):
                    result = api(instance, "/api/ui/command", {"command": name, "args": arguments or {},
                        "origin": "website-screenshot-pipeline", "timeout": 30})
                    if result.get("ok") is False:
                        raise RuntimeError(f"{name}: {result.get('error')}")
                    return result

                for shot in shots:
                    if args.only and shot["id"] != args.only:
                        continue
                    name = names[shot["source"]]
                    print(f"Preparing {shot['id']}", flush=True)
                    if api(instance, "/api/ui/state").get("compare", {}).get("active"):
                        command("compare")
                    command("goto", {"name": name})
                    command("view:detail")
                    for step in shot.get("historySteps") or [{"patch": shot.get("patch", {}), "label": shot["title"]}]:
                        current = api(instance, "/api/state?name=" + urllib.parse.quote(name))
                        patch = step["patch"]
                        merged = {**current, **patch}
                        for field in ("params", "grade", "optics"):
                            if field in patch:
                                merged[field] = {**(current.get(field) or {}), **patch[field]}
                        merged = shot_geometry.fit_state(
                            merged, shot, bundle / "Python/bin/python3", bundle / "Resources/LightTable", photos / shot["source"])
                        for field in ("name", "provenance", "width", "height", "raw", "kind", "folder",
                                      "sourceName", "displayName", "fileKey", "mtime", "ai", "virtual"):
                            merged.pop(field, None)
                        api(instance, "/api/state", {"name": name, **merged,
                            "origin": "website-screenshot-pipeline", "historyLabel": step["label"]})
                        time.sleep(1.5)
                    saved = api(instance, "/api/state?name=" + urllib.parse.quote(name))
                    render_width = min(3000, max(saved.get("width", 0), saved.get("height", 0))) or 3000
                    deadline = time.monotonic() + 240
                    while time.monotonic() < deadline:
                        if api(instance, "/api/refine", {"name": name, "params": saved["params"], "w": render_width}).get("ready"):
                            break
                        time.sleep(0.5)
                    else:
                        raise RuntimeError(f"Accurate photo preparation timed out: {shot['id']}")
                    rendered = api(instance, "/api/render", {"name": name, "params": saved["params"],
                        "w": render_width, "engine": "rs", "native": False})
                    if rendered.get("error") or rendered.get("refining") or not rendered.get("img"):
                        raise RuntimeError(f"Final photo render unavailable: {shot['id']}")
                    command("goto", {"name": name})
                    command("view:detail")
                    command("pane:" + shot["pane"])
                    if shot["pane"] == "mask":
                        command("mask.show", {"id": "gallery-radial"})
                    for action in shot.get("commands", []):
                        command(action)
                    command("zoomFit")
                    expectation = shot.get("expect", {})
                    desired_pane = expectation.get("pane", shot["pane"]) + "Pane"

                    def correct(state):
                        return (state.get("current") == name and state.get("pane") == desired_pane
                            and state.get("render", {}).get("state") == "ready"
                            and state.get("render", {}).get("name") == name
                            and state.get("compare", {}).get("active") == expectation.get("compare", False)
                            and all(state.get(key) == value for key, value in expectation.items() if key == "activeTool"))

                    deadline, settled = time.monotonic() + 240, None
                    while time.monotonic() < deadline:
                        state = api(instance, "/api/ui/state")
                        if correct(state):
                            settled = settled or time.monotonic()
                            if time.monotonic() - settled > 5:
                                break
                        else:
                            settled = None
                        time.sleep(0.5)
                    else:
                        raise RuntimeError(f"Native view did not settle: {shot['id']}: {state}")
                    command("zoomFit")
                    if shot.get("focusSection"):
                        run("xdotool", "mousemove", "--window", window, width - 300, 1200,
                            "click", "--repeat", 24, "--delay", 50, "5")
                    if shot.get("dialog") == "export":
                        command("exportPhotos")
                    # Move the cursor outside the captured window, clearing tooltips.
                    run("xdotool", "mousemove", "0", "0")
                    time.sleep(4)
                    before = api(instance, "/api/ui/state")
                    if not correct(before):
                        raise RuntimeError(f"View changed before capture: {shot['id']}")
                    destination = output / f"{shot['id']}.png"
                    run("import", "-window", window, destination)
                    after = api(instance, "/api/ui/state")
                    if not correct(after):
                        raise RuntimeError(f"View changed during capture: {shot['id']}")
                    with Image.open(destination) as captured:
                        if captured.size != (width, height):
                            raise RuntimeError("Native capture dimensions changed")
                        # Xvfb's native surface is sRGB. Attach the profile without
                        # changing any captured pixel or inventing a display profile.
                        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                        captured.save(destination, icc_profile=profile)
                    record = {"id": shot["id"], "source": shot["source"], "sourceSha256": sources[shot["source"]]["sha256"],
                        "appRevision": build["source_revision"], "sourceDirty": False,
                        "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "width": width, "height": height, "deviceScaleFactor": 2,
                        "backend": after["render"]["backend"], "pane": after["pane"],
                        "previewWidth": render_width, "sha256": digest(destination),
                        "nativeWindow": {"toolkit": "GTK/WebKitGTK", "display": "X11/Xvfb",
                            "gdkScale": 2, "theme": "Adwaita:dark", "windowOnly": True,
                            "windowManagerDecorations": False,
                            "geometry": geometry, "before": before, "after": after},
                        "build": build, "distribution": platform.freedesktop_os_release(),
                        "architecture": platform.machine()}
                    (output / f"{shot['id']}.json").write_text(json.dumps(record, indent=2) + "\n")
                    print(f"Captured {shot['id']}: {width}x{height}, {record['backend']}", flush=True)
            finally:
                stop(process)


if __name__ == "__main__":
    main()
