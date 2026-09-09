#!/usr/bin/env python3
"""Require a packaged X11 edit/export/normal-close/reopen journey in private data.

Run with the bundle's Python under Xvfb and a private D-Bus session. This uses
software graphics and a synthetic RGB16 TIFF; it does not certify RAW/hardware,
display color, or Wayland close behavior. SIGTERM is cleanup only, never proof
that the native save-and-close flow succeeded.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

TITLE = "LightTable — photos"
SHAPE = (128, 1024, 3)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def isolated_environment(root: Path, inherited=None):
    env = {key: value for key, value in (os.environ if inherited is None else inherited).items()
           if not key.startswith(("LIGHTTABLE_", "PYTHON", "XDG_"))}
    env.update({f"XDG_{name.upper()}_HOME": str(root / name) for name in ("data", "config", "cache", "state")})
    env.update({"XDG_RUNTIME_DIR": str(root / "runtime"), "LIGHTTABLE_DIR": str(root / "photos"),
        "LIGHTTABLE_CATALOG_FILE": str(root / "catalog/library.sqlite3"),
        "LIGHTTABLE_INSTANCE_DIR": str(root / "instances"), "LIGHTTABLE_WATCH": "0",
        "NUMBA_CACHE_DIR": str(root / "compiled"), "MPLCONFIGDIR": str(root / "matplotlib"),
        "GDK_BACKEND": "x11", "SPEKTRAFILM_BACKEND": "cpu", "LIBGL_ALWAYS_SOFTWARE": "1"})
    return env


def validate_bundle(bundle: Path, expected: str):
    require(Path(sys.prefix).resolve() == bundle / "Python", "Use this bundle's Python runtime")
    manifest = json.loads((bundle / "build-manifest.json").read_text())
    require(re.fullmatch(r"[0-9a-f]{40}", expected) and manifest.get("source_revision") == expected
            and manifest.get("source_dirty") is False and manifest.get("platform") == "linux"
            and manifest.get("architecture") == platform.machine(), "Bundle source/platform identity does not match")
    for name in ("bin/lighttable-desktop", "bin/lighttable-desktop-shell", "Python/bin/python3",
                 "Resources/LightTable/server.py", "Resources/LightTable/render_cli.py"):
        path = bundle / name
        require(path.is_file() and path.resolve().is_relative_to(bundle), f"Incomplete bundle: {name}")
    return manifest


def same_path(value, expected):
    try:
        return bool(value) and Path(value).is_absolute() and expected.samefile(value)
    except (OSError, TypeError, ValueError):
        return False


def owns_server(bundle, desktop, pid):
    try:
        return (type(pid) is int and pid > 1 and os.getpgid(pid) == desktop.pid
                and Path(f"/proc/{desktop.pid}/exe").resolve() == (bundle / "bin/lighttable-desktop-shell").resolve()
                and Path(f"/proc/{pid}/exe").resolve() == (bundle / "Python/bin/python3").resolve()
                and os.fsencode(bundle / "Resources/LightTable/server.py") in
                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"))
    except (OSError, ValueError):
        return False


class XClientMessage(C.Structure):
    # Xlib's format-32 values occupy native longs, including on 64-bit Linux.
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("message_type", C.c_ulong),
                ("format", C.c_int), ("data", C.c_long * 5)]


class XEvent(C.Union):
    _fields_ = [("client", XClientMessage), ("padding", C.c_long * 24)]


def owned_window(windows, pid):
    matches = [window for window in windows if window["pid"] == pid and window["title"] == TITLE]
    require(type(pid) is int and pid > 1 and len(matches) == 1,
            f"Expected exactly one X11 window owned by PID {pid} with title {TITLE!r}; found {len(matches)}")
    return matches[0]["id"]


def close_event(display, window, protocols, delete):
    event = XEvent()
    event.client.type, event.client.send_event = 33, 1  # ClientMessage
    event.client.display, event.client.window = display, window
    event.client.message_type, event.client.format = protocols, 32
    event.client.data[0], event.client.data[1] = delete, 0  # CurrentTime
    return event


class X11:
    """Read bounded window properties; send WM_DELETE only to a proven window.

    Protocol/layout reference: https://www.x.org/releases/X11R7.5/doc/libX11/libX11.html
    """
    def __init__(self):
        self.lib = C.CDLL("libX11.so.6")
        pointer, ulong = C.c_void_p, C.c_ulong
        signatures = {
            "XOpenDisplay": ([C.c_char_p], pointer), "XCloseDisplay": ([pointer], C.c_int),
            "XDefaultRootWindow": ([pointer], ulong), "XInternAtom": ([pointer, C.c_char_p, C.c_int], ulong),
            "XQueryTree": ([pointer, ulong, C.POINTER(ulong), C.POINTER(ulong), C.POINTER(C.POINTER(ulong)), C.POINTER(C.c_uint)], C.c_int),
            "XGetWindowProperty": ([pointer, ulong, ulong, C.c_long, C.c_long, C.c_int, ulong,
                C.POINTER(ulong), C.POINTER(C.c_int), C.POINTER(ulong), C.POINTER(ulong), C.POINTER(pointer)], C.c_int),
            "XSendEvent": ([pointer, ulong, C.c_int, C.c_long, C.POINTER(XEvent)], C.c_int),
            "XFlush": ([pointer], C.c_int), "XFree": ([pointer], C.c_int),
            "XSetErrorHandler": ([pointer], pointer),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result
        self.display = self.lib.XOpenDisplay(None)
        require(self.display, "Could not connect to the private X11 display")
        # A transient child window can disappear during enumeration. Xlib's
        # default protocol-error handler exits Python without process cleanup.
        self.handler = C.CFUNCTYPE(C.c_int, pointer, pointer)(lambda *_: 0)
        self.previous_handler = self.lib.XSetErrorHandler(C.cast(self.handler, pointer))

    def atom(self, name):
        return self.lib.XInternAtom(self.display, name.encode(), 1)

    def property(self, window, name, kind, expected_format):
        actual, count, remaining, fmt, data = C.c_ulong(), C.c_ulong(), C.c_ulong(), C.c_int(), C.c_void_p()
        atom = self.atom(name)
        if not atom or not kind:
            return None
        try:
            status = self.lib.XGetWindowProperty(self.display, window, atom, 0, 1024, 0, kind,
                C.byref(actual), C.byref(fmt), C.byref(count), C.byref(remaining), C.byref(data))
            if status or actual.value != kind or fmt.value != expected_format:
                return None
            require(not remaining.value and count.value <= 4096, "X11 property exceeds the inspection bound")
            return (C.string_at(data, count.value) if fmt.value == 8 else
                    tuple(C.cast(data, C.POINTER(C.c_ulong))[i] for i in range(count.value)))
        finally:
            if data:
                self.lib.XFree(data)

    def delete_window(self, pid, deadline):
        pending, seen, windows = [self.lib.XDefaultRootWindow(self.display)], set(), []
        while pending:
            require(len(seen) < 4096 and time.monotonic() < deadline, "X11 window enumeration exceeded its bound")
            window = pending.pop()
            if window in seen:
                continue
            seen.add(window)
            owner = self.property(window, "_NET_WM_PID", 6, 32)  # XA_CARDINAL
            if owner == (pid,):
                title = self.property(window, "_NET_WM_NAME", self.atom("UTF8_STRING"), 8)
                windows.append({"id": window, "pid": pid, "title": title.decode("utf-8", "replace") if title else ""})
            root, parent, children, count = C.c_ulong(), C.c_ulong(), C.POINTER(C.c_ulong)(), C.c_uint()
            try:
                if self.lib.XQueryTree(self.display, window, C.byref(root), C.byref(parent), C.byref(children), C.byref(count)):
                    require(count.value <= 4096, "X11 child count exceeds the inspection bound")
                    pending.extend(children[i] for i in range(count.value))
            finally:
                if children:
                    self.lib.XFree(children)
        window = owned_window(windows, pid)
        protocols, delete = self.atom("WM_PROTOCOLS"), self.atom("WM_DELETE_WINDOW")
        require(delete and delete in (self.property(window, "WM_PROTOCOLS", 4, 32) or ()),
                "The owned X11 window does not advertise WM_DELETE_WINDOW")
        # Recheck ownership/title immediately before delivering the event.
        require(self.property(window, "_NET_WM_PID", 6, 32) == (pid,)
                and self.property(window, "_NET_WM_NAME", self.atom("UTF8_STRING"), 8) == TITLE.encode(),
                "The X11 window identity changed before close")
        event = close_event(self.display, window, protocols, delete)
        require(self.lib.XSendEvent(self.display, window, 0, 0, C.byref(event)), "WM_DELETE_WINDOW delivery failed")
        self.lib.XFlush(self.display)
        return {"window_id": window, "pid": pid, "title": TITLE, "protocol": "WM_DELETE_WINDOW"}

    def close(self):
        self.lib.XCloseDisplay(self.display)
        self.lib.XSetErrorHandler(self.previous_handler)


def cleanup(process):
    for number, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            continue
        # The native parent may exit before its server/WebKit children.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
    process.wait(timeout=3)


class API:
    def __init__(self, instance, deadline):
        require(type(instance.get("port")) is int and 1 <= instance["port"] <= 65535
                and isinstance(instance.get("token"), str) and instance["token"], "Invalid private API registration")
        self.port, self.token, self.deadline = instance["port"], instance["token"], deadline
        self.opener = build_opener(ProxyHandler({}))

    def request(self, route, body=None):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Native acceptance exceeded its deadline")
        request = Request(f"http://127.0.0.1:{self.port}{route}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-LightTable-Token": self.token, "X-LightTable-Strict": "1"})
        with self.opener.open(request, timeout=min(5, remaining)) as response:
            require(response.status == 200, "Native API did not return HTTP 200")
            result = json.load(response)
        require(not result.get("error"), f"Native API rejected {route.split('?')[0]}")
        return result


def wait_for(process, deadline, phase, check):
    while time.monotonic() < deadline:
        require(process.poll() is None, f"Native app exited while waiting for {phase}")
        try:
            result = check()
            if result:
                return result
        except HTTPError:
            raise
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Native acceptance timed out waiting for {phase}")


def connect(bundle, process, root, deadline, tokens):
    def registered():
        for path in (root / "instances").glob("[0-9]*.json"):
            try:
                value = json.loads(path.read_text())
                if same_path(value.get("folder"), root / "photos") and owns_server(bundle, process, value.get("pid")):
                    return value
            except (OSError, ValueError):
                pass
    instance = wait_for(process, deadline, "private server registration", registered)
    tokens.add(instance["token"])
    api = API(instance, deadline)
    health = wait_for(process, deadline, "HTTP health", lambda: api.request("/api/health"))
    require(health.get("ok") is True and health.get("pid") == instance["pid"]
            and same_path(health.get("catalog"), root / "catalog/library.sqlite3")
            and same_path(health.get("folder"), root / "photos") and not health.get("headless")
            and not health.get("safeMode"), "Native server opened unexpected data or mode")
    return api, health


def send_ui(api, command, args=None):
    try:
        result = api.request("/api/ui/command", {"command": command, "args": args or {}, "timeout": 3})
    except HTTPError as error:
        if error.code != 504:
            raise
        return  # Delivered once; verify resulting state instead of resubmitting.
    require(result.get("ok"), f"Native UI rejected {command}")


def render_photo(process, api, deadline, source):
    def visible():
        state = api.request("/api/ui/state")
        return state.get("client") and state.get("age", 999) < 10 and state.get("visibleCount", 0) == 1
    wait_for(process, deadline, "the one test photo in the native library", visible)
    def imported():
        # /api/images omits the source-path fields needed to prove ownership.
        matches = [item["name"] for item in api.request("/api/catalog/query", {"limit": 10}).get("items", [])
                   if item.get("relpath") == source.name and not item.get("virtual")
                   and same_path(item.get("sourcePath"), source.parent)]
        require(len(matches) <= 1, "The isolated source has ambiguous photo identity")
        return matches[0] if matches else None
    name = wait_for(process, deadline, "the imported photo at the isolated source path", imported)
    send_ui(api, "goto", {"name": name})
    def ready():
        state = api.request("/api/ui/state")
        render = state.get("render") or {}
        return state if (state.get("client") and state.get("age", 999) < 10 and state.get("current") == name
                         and render.get("name") == name and render.get("state") == "ready") else None
    wait_for(process, deadline, "the native photo render", ready)
    return name


def edits_saved(saved):
    grade = saved.get("grade") if isinstance(saved, dict) else None
    params = saved.get("params") if isinstance(saved, dict) else None
    return (isinstance(grade, dict) and grade.get("exposure") == 0.5 and saved.get("rating") == 4
            and isinstance(params, dict) and params.get("profile_enabled") is False)


def verify_export(path):
    import numpy as np
    import tifffile
    with tifffile.TiffFile(path) as image:
        pixels, icc = image.asarray(), image.pages[0].tags.get(34675)
        require(pixels.dtype == np.uint16 and pixels.shape == SHAPE, "Export is not the expected RGB16 TIFF")
        require(icc is not None and icc.value, "Export has no embedded ICC profile")
        levels = int(np.unique(pixels[..., 0]).size)
        require(levels > 256, "RGB16 export lost precision to eight-bit levels")
        return {"shape": list(pixels.shape), "dtype": str(pixels.dtype), "red_levels": levels,
                "icc_bytes": len(icc.value), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def normal_close(process, server_pid, deadline):
    require(process.poll() is None, "Native desktop exited before its normal close request")
    display = X11()
    try:
        receipt = display.delete_window(process.pid, deadline)
    finally:
        display.close()
    process.wait(timeout=max(0.01, min(25, deadline - time.monotonic())))
    require(process.returncode == 0, "Native WM_DELETE_WINDOW did not exit successfully")
    # A zombie is finished; its parent/reaper can remove /proc a little later.
    try:
        state = Path(f"/proc/{server_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        require(state == "Z", "Normal native close left the render server running")
    except FileNotFoundError:
        pass
    return receipt


def startup_snapshot(root):
    records = []
    for path in sorted((root / "instances").glob("[0-9]*.json"))[:16]:
        try:
            value = json.loads(path.read_text())
            records.append({key: value[key] for key in ("pid", "port", "folder", "catalog", "headless")
                            if isinstance(value.get(key), (str, int, bool))})
        except (OSError, ValueError, TypeError):
            pass
    return records


def redact(text, tokens):
    for token in tokens:
        if token:
            text = text.replace(token, "[redacted]")
    return text


def run(bundle, expected, timeout, report_dir):
    deadline, report, tokens = time.monotonic() + timeout, {"ok": False, "desktop": "GTK/WebKitGTK", "backend": "x11"}, set()
    try:
        report["build"] = validate_bundle(bundle, expected)
        import numpy as np
        import tifffile
        with tempfile.TemporaryDirectory(prefix="lighttable-linux-acceptance-") as temporary:
            root, children = Path(temporary).resolve(), []
            for name in ("runtime", "photos", "config/lighttable"):
                (root / name).mkdir(mode=0o700, parents=True)
            env = isolated_environment(root)
            (root / "config/lighttable/prefs.json").write_text(json.dumps({
                "locale": "en", "localeChosen": True, "allowAutomation": True, "viewMode": "detail",
                "firstRunSetup": {"version": 1, "status": "completed", "source": "folder"},
                "newPhotoDefaults": {"filmEnabled": False}, "automaticUpdateChecks": False,
                "writeSidecars": False, "backupDirectory": str(root / "backups")}))
            source = root / "photos/smoke.tif"
            ramp = np.tile(np.linspace(0, 65535, SHAPE[1], dtype=np.uint16)[None, :, None], (SHAPE[0], 1, 3))
            tifffile.imwrite(source, ramp, photometric="rgb", metadata=None)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            try:
                def launch():
                    with (root / "desktop.log").open("a") as log:
                        process = subprocess.Popen([str(bundle / "bin/lighttable-desktop")], env=env,
                            stdout=log, stderr=log, start_new_session=True)
                    children.append(process)
                    return process
                desktop = launch()
                api, initial = connect(bundle, desktop, root, deadline, tokens)
                name = render_photo(desktop, api, deadline, source)
                send_ui(api, "slider", {"key": "exposure", "value": 0.5})
                send_ui(api, "rating:4")
                state_route = "/api/state?" + urlencode({"name": name})
                wait_for(desktop, deadline, "saved exposure/rating and film-disabled state", lambda: edits_saved(api.request(state_route)))
                result = api.request("/api/export", {"names": [name], "format": "tif", "outputSpace": "srgb",
                    "destination": str(root / "exports"), "metadata": "none", "sidecar": False, "collision": "rename"})
                require(result.get("queued"), "TIFF export was not queued")
                def exported():
                    status = api.request("/api/export/status")
                    report["export_progress"] = {key: status.get(key) for key in ("running", "done", "total", "phase")}
                    require(not status.get("error") and not status.get("errors"), "TIFF export reported an error")
                    return not status.get("running") and status.get("done") == 1
                wait_for(desktop, deadline, "neutral RGB16 TIFF export through render_cli.py", exported)
                outputs = list((root / "exports").rglob("*.tif"))
                require(len(outputs) == 1, "Expected exactly one TIFF export")
                report["export"] = verify_export(outputs[0])
                if report_dir:
                    shutil.copy2(outputs[0], report_dir / "export.tif")
                report["first_close"] = normal_close(desktop, initial["pid"], deadline)
                cleanup(desktop)
                children.remove(desktop)
                desktop = launch()
                api, reopened = connect(bundle, desktop, root, deadline, tokens)
                reopened_name = render_photo(desktop, api, deadline, source)
                require(reopened["pid"] != initial["pid"] and reopened_name == name, "Reopen did not preserve photo identity in a new server")
                wait_for(desktop, deadline, "persistent edits after normal reopen", lambda: edits_saved(api.request(state_route)))
                report["persistence"] = {"exposure": 0.5, "rating": 4, "film_enabled": False,
                    "old_server_pid": initial["pid"], "new_server_pid": reopened["pid"]}
                report["final_close"] = normal_close(desktop, reopened["pid"], deadline)
                require(hashlib.sha256(source.read_bytes()).hexdigest() == original, "The source photo changed")
                report.update(source_sha256=original, http=200)
            except BaseException:
                report["startup_records"] = startup_snapshot(root)
                raise
            finally:
                try:
                    for process in reversed(children):
                        cleanup(process)
                finally:
                    if report_dir:
                        for path in (root / "desktop.log", root / "state/lighttable/logs/server.log"):
                            if path.exists():
                                (report_dir / path.name).write_text(redact(path.read_text(errors="replace")[-24000:], tokens))
        report["ok"] = True
    except BaseException as error:
        report["error"] = redact(str(error), tokens)
        raise
    finally:
        text = json.dumps(report, indent=2) + "\n"
        if report_dir:
            (report_dir / "report.json").write_text(text)
        print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        parser.error("Run on Linux under Xvfb and a private dbus-run-session")
    if not 60 <= args.timeout <= 240:
        parser.error("--timeout must be 60–240 seconds; process cleanup adds at most 15 seconds")
    if args.report_dir:
        args.report_dir = args.report_dir.resolve()
        args.report_dir.mkdir(parents=True, exist_ok=False)
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    run(args.bundle.resolve(), args.expected_source, args.timeout, args.report_dir)


if __name__ == "__main__":
    main()
