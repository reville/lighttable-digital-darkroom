#!/usr/bin/env python3
"""Check an extracted Windows bundle in an interactive Windows desktop.

Run with the bundle's Python. A private Windows job contains only this test's
GUI, render server and WebView2 children. The native path probe must prove that
all writable paths are isolated before the first window is created. This is
native UI/API evidence, not a screenshot or display-color certification.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError, URLError

MUTABLE_NATIVE_PATHS = ("support", "settings", "prefs", "cache", "log", "presets", "webview")


def smoke_environment(root: Path, inherited=None) -> dict[str, str]:
    environment = {key: value for key, value in (os.environ if inherited is None else inherited).items()
                   if not key.upper().startswith(("LIGHTTABLE_", "WEBVIEW2_", "PYTHON"))}
    support = root / "support"
    environment.update({
        "LIGHTTABLE_SUPPORT_DIR": str(support), "LIGHTTABLE_DIR": str(root / "photos"),
        "LIGHTTABLE_CATALOG_FILE": str(root / "catalog/library.sqlite3"),
        "LIGHTTABLE_CACHE_DIR": str(root / "cache"),
        "LIGHTTABLE_PREFS_FILE": str(support / "prefs.json"),
        "LIGHTTABLE_PRESETS_FILE": str(support / "presets.json"),
        "LIGHTTABLE_AI_DIR": str(root / "ai"), "LIGHTTABLE_MODEL_DIR": str(root / "models"),
        "LIGHTTABLE_PROFILE_ROOT": str(root / "profiles"),
        "LIGHTTABLE_INSTANCE_DIR": str(root / "instances"),
        "LIGHTTABLE_STARTUP_FILE": str(root / "startup.json"),
        "LIGHTTABLE_SERVER_LOG": str(root / "server.log"), "LIGHTTABLE_LOG_FILE": str(root / "server.log"),
        "LIGHTTABLE_WATCH": "0", "NUMBA_CACHE_DIR": str(root / "compiled"),
        "MPLCONFIGDIR": str(root / "matplotlib"), "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def validate_paths(report: dict, root: Path, bundle: Path) -> None:
    for key in MUTABLE_NATIVE_PATHS:
        value = Path(report.get(key, ""))
        if not value.is_absolute() or not value.resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f"Native {key} is outside the isolated test directory")
    if Path(report.get("python", "")).resolve() != (bundle / "Python/python.exe").resolve():
        raise RuntimeError("The native shell is not using the packaged Python")
    if Path(report.get("project", "")).resolve() != (bundle / "Resources/LightTable").resolve():
        raise RuntimeError("The native shell is not using the packaged resources")


def validate_bundle(bundle: Path) -> None:
    for relative in ("LightTable.exe", "Python/python.exe", "Resources/LightTable/server.py"):
        if not (bundle / relative).is_file():
            raise RuntimeError(f"The Windows bundle is incomplete: {relative}")
    marker = bundle / "install-channel.txt"
    if marker.exists() and marker.read_text(encoding="utf-8-sig").strip() != "portable":
        # A signed direct install configures WinSparkle's global registry state.
        # Validate the extracted ZIP so this smoke cannot alter that preference.
        raise RuntimeError("Use the extracted portable ZIP, not an installed/package-managed copy")


def require_interactive_desktop():
    user = ctypes.WinDLL("user32", use_last_error=True)
    class Flags(ctypes.Structure):
        _fields_ = [("inherit", wintypes.BOOL), ("reserved", wintypes.BOOL), ("flags", wintypes.DWORD)]
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    flags, needed = Flags(), wintypes.DWORD()
    if not user.GetUserObjectInformationW(user.GetProcessWindowStation(), 1, ctypes.byref(flags),
                                         ctypes.sizeof(flags), ctypes.byref(needed)) or not flags.flags & 1:
        raise RuntimeError("Run in the logged-on interactive desktop, not a Windows service session")


class WindowsDesktop:
    """Create the GUI suspended, assign a private job, then allow child creation."""
    # Children inherit membership; no breakaway flag is granted. Windows 10/11
    # support nested jobs used by WebView2 and CI runners:
    # https://learn.microsoft.com/windows/win32/procthread/job-objects
    def __init__(self, executable: Path, environment: dict, cwd: Path, arguments=()):
        if os.name != "nt":
            raise RuntimeError("Native desktop smoke requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        handle = wintypes.HANDLE
        pointer = ctypes.c_void_p
        size = ctypes.c_size_t
        class Startup(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                        *[(name, wintypes.DWORD) for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
                        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                        ("reserved_bytes", pointer), ("stdin", handle), ("stdout", handle), ("stderr", handle)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", handle), ("thread", handle), ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]
        class BasicLimit(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", size), ("max_working", size),
                        ("active_limit", wintypes.DWORD), ("affinity", size),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", size), ("job_memory", size),
                        ("peak_process_memory", size), ("peak_job_memory", size)]
        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("page_faults", wintypes.DWORD),
                        ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
        self.Accounting = Accounting
        signatures = {
            "CreateJobObjectW": ([pointer, wintypes.LPCWSTR], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, pointer, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([handle, ctypes.c_int, pointer, wintypes.DWORD, pointer], wintypes.BOOL),
            "AssignProcessToJobObject": ([handle, handle], wintypes.BOOL),
            "TerminateJobObject": ([handle, wintypes.UINT], wintypes.BOOL),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, pointer, pointer, wintypes.BOOL,
                                wintypes.DWORD, pointer, wintypes.LPCWSTR, ctypes.POINTER(Startup),
                                ctypes.POINTER(ProcessInfo)], wintypes.BOOL),
            "ResumeThread": ([handle], wintypes.DWORD), "CloseHandle": ([handle], wintypes.BOOL),
            "WaitForSingleObject": ([handle, wintypes.DWORD], wintypes.DWORD),
            "TerminateProcess": ([handle, wintypes.UINT], wintypes.BOOL),
            "IsProcessInJob": ([handle, handle, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], handle),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.job, self.process, self.pid = None, None, None
        info = ProcessInfo()
        try:
            self.job = self.kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            startup = Startup()
            startup.cb = ctypes.sizeof(startup)
            block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items(), key=lambda p: p[0].upper())) + "\0\0")
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *map(str, arguments)]))
            self._check(self.kernel.CreateProcessW(str(executable), command, None, None, False,
                         0x4 | 0x400, block, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
            self.process, self.pid = info.process, int(info.pid)
            self._check(self.kernel.AssignProcessToJobObject(self.job, self.process))
            if self.kernel.ResumeThread(info.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if self.process:
                self.kernel.TerminateProcess(self.process, 1)  # Still suspended if assignment failed.
            self.close()
            raise
        finally:
            if info.thread:
                self.kernel.CloseHandle(info.thread)

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def running(self):
        return self.kernel.WaitForSingleObject(self.process, 0) == 258

    def owns_pid(self, pid: int) -> bool:
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            member = wintypes.BOOL()
            self._check(self.kernel.IsProcessInJob(process, self.job, ctypes.byref(member)))
            return bool(member.value)
        finally:
            self.kernel.CloseHandle(process)

    def quit(self, timeout: float):
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        windows = []
        @callback_type
        def find_window(window, _):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.pid and self.user.IsWindowVisible(window):
                windows.append(window)
            return True
        self._check(self.user.EnumWindows(find_window, 0))
        if len(windows) != 1:
            raise RuntimeError(f"Expected exactly one visible native test window to close; found {len(windows)}")
        self._check(self.user.PostMessageW(windows[0], 0x10, 0, 0))  # WM_CLOSE invokes the normal save barrier.
        if self.kernel.WaitForSingleObject(self.process, max(1, int(timeout * 1000))) != 0:
            raise RuntimeError("The native window did not finish its normal save-and-close flow")

    def close(self):
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                accounting = self.Accounting()
                if not self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    break
                if accounting.active == 0:
                    break
                time.sleep(0.05)
            self.kernel.CloseHandle(self.job)
            self.job = None
        if self.process:
            self.kernel.WaitForSingleObject(self.process, 5000)
            self.kernel.CloseHandle(self.process)
            self.process = None


class API:
    def __init__(self, instance: dict, deadline: float):
        self.port, self.token, self.deadline = int(instance["port"]), instance["token"], deadline
        self.opener = build_opener(ProxyHandler({}))

    def request(self, route: str, body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Desktop smoke exceeded its time limit")
        request = Request(f"http://127.0.0.1:{self.port}{route}",
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", "X-LightTable-Token": self.token,
                                   "X-LightTable-Strict": "1"})
        try:
            with self.opener.open(request, timeout=min(5, remaining)) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                with error:
                    payload = json.loads(error.read(65536))
                detail = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(detail, str) and detail:
                    error.msg = f"{error.msg}: {detail[:500]}"
            except (OSError, ValueError, TypeError):
                pass
            raise
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result


def wait_for(desktop, deadline, phase, check):
    while time.monotonic() < deadline:
        if not desktop.running():
            raise RuntimeError(f"Native app exited while waiting for {phase}")
        try:
            result = check()
            if result:
                return result
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Desktop timed out waiting for {phase}")


def same_existing_path(value, expected: Path) -> bool:
    # Rust canonicalize returns Windows extended paths (\\?\C:\...). Python
    # resolve preserves that prefix when supplied, so string equality rejects
    # the same directory. Compare existing filesystem objects, failing closed.
    try:
        return bool(value) and Path(value).is_absolute() and expected.samefile(value)
    except (OSError, TypeError, ValueError):
        return False


def startup_diagnostics(root: Path, desktop) -> dict:
    """Keep only scalar startup/identity fields; never copy an instance token."""
    result = {"expected_folder": str(root / "photos"),
              "expected_catalog": str(root / "catalog/library.sqlite3"), "records": []}
    paths = [root / "startup.json", *sorted((root / "instances").glob("[0-9]*.json"))[:16]]
    fields = ("phase", "code", "pid", "port", "startedAt", "updatedAt", "ok",
              "folder", "catalog", "headless", "safeMode", "sourceRevision")
    for path in paths:
        record = {"file": str(path.relative_to(root))}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            record.update({key: value[key] for key in fields if key in value
                           and isinstance(value[key], (str, int, float, bool, type(None)))})
            record["folder_matches"] = same_existing_path(value.get("folder"), root / "photos")
            record["catalog_matches"] = same_existing_path(value.get("catalog"), root / "catalog/library.sqlite3")
            if desktop and isinstance(value.get("pid"), int):
                record["owned_process"] = desktop.owns_pid(value["pid"])
        except (OSError, ValueError, TypeError, AttributeError) as error:
            record["read_error"] = type(error).__name__
        result["records"].append(record)
    return result


def capture_failure_window(desktop, destination: Path) -> dict:
    """Capture only our foreground HWND, without changing focus or z-order."""
    try:
        if os.name != "nt" or not desktop or not desktop.running():
            return {"available": False, "reason": "No running Windows test window"}
        from PIL import ImageGrab
        user = desktop.user
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        window, pid = user.GetForegroundWindow(), wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not window or pid.value != desktop.pid:
            return {"available": False, "reason": "The test app does not own the foreground window"}
        # Restrict Pillow to this HWND's client area; never grab the desktop.
        with ImageGrab.grab(window=window) as captured:
            captured.save(destination)
        return {"available": True, "file": destination.name}
    except Exception as error:
        return {"available": False, "reason": type(error).__name__}


def connect(desktop, root, deadline):
    def registered():
        for path in (root / "instances").glob("[0-9]*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                if same_existing_path(candidate.get("folder"), root / "photos") and desktop.owns_pid(int(candidate["pid"])):
                    return candidate
            except (OSError, ValueError, KeyError):
                pass
    instance = wait_for(desktop, deadline, "its private render server", registered)
    api = API(instance, deadline)
    # Registration precedes the HTTP serve loop and background-service setup.
    # Keep startup retries inside the same bounded native-acceptance deadline.
    health = wait_for(desktop, deadline, "HTTP health", lambda: api.request("/api/health"))
    if (health.get("ok") is not True or health.get("pid") != instance["pid"]
            or not same_existing_path(health.get("catalog"), root / "catalog/library.sqlite3")
            or not same_existing_path(health.get("folder"), root / "photos")
            or health.get("headless") or health.get("safeMode")):
        raise RuntimeError("Native server identity or isolated catalog did not match")
    return api, health


def render_photo(desktop, api, deadline, basename):
    wait_for(desktop, deadline, "the native WebView2 client", lambda: api.request("/api/ui/state").get("client"))
    def find_photo():
        return next((item["name"] for item in api.request("/api/images").get("images", [])
                     if Path(item["name"]).name == basename), None)
    name = wait_for(desktop, deadline, "the imported test photo", find_photo)
    def photo_visible():
        state = api.request("/api/ui/state")
        return state if (state.get("client") and state.get("age", 999) < 10
                         and state.get("visibleCount", 0) >= 1) else None
    # Server import can finish before the window reloads its own image list.
    # This isolated catalog contains exactly the one test photo.
    wait_for(desktop, deadline, "the test photo in the native window's library", photo_visible)
    send_ui_command(api, "goto", {"name": name})
    def ready():
        state = api.request("/api/ui/state")
        render = state.get("render") or {}
        return state if (state.get("client") and state.get("age", 999) < 10 and state.get("current") == name
                         and render.get("name") == name and render.get("state") == "ready") else None
    return name, wait_for(desktop, deadline, "the photo rendered inside WebView2", ready)


def send_ui_command(api, command, args=None):
    try:
        result = api.request("/api/ui/command", {"command": command, "args": args or {}, "timeout": 3})
    except HTTPError as error:
        if error.code != 504:
            raise
        # The command was delivered, but rendering can exceed its response
        # deadline. Callers verify the resulting state instead of sending again.
        return
    if not result.get("ok"):
        raise RuntimeError(f"Native UI did not accept {command}")


def expected_edits_saved(saved) -> bool:
    grade = saved.get("grade") if isinstance(saved, dict) else None
    return (isinstance(grade, dict) and grade.get("exposure") == 0.5
            and saved.get("rating") == 4)


def verify_export(path: Path, *, require_precision: bool) -> dict:
    import numpy as np
    import tifffile
    with tifffile.TiffFile(path) as image:
        pixels = image.asarray()
        icc = image.pages[0].tags.get(34675)
        if pixels.dtype != np.uint16 or pixels.ndim != 3 or pixels.shape[2] != 3 or min(pixels.shape[:2]) < 1:
            raise RuntimeError("Export is not a nonempty RGB16 TIFF")
        if icc is None or not icc.value:
            raise RuntimeError("Export is missing its ICC profile")
        levels = int(np.unique(pixels[..., 0]).size)
        if require_precision and levels <= 256:
            raise RuntimeError("The RGB16 gradient export lost precision to 8-bit levels")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        return {"shape": list(pixels.shape), "dtype": str(pixels.dtype), "icc_bytes": len(icc.value),
                "red_levels": levels, "sha256": digest}


def cleanup_temporary_directory(temporary) -> None:
    # WebView2 can release its private files after the host/job have exited.
    # Retry only this TemporaryDirectory; never discover or stop other processes.
    deadline, remaining = time.monotonic() + 10, 10
    while remaining > 0:
        try:
            temporary.cleanup()
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.2, remaining))


@contextmanager
def smoke_directory(report: dict, report_dir: Path | None):
    # Explicit cleanup prevents a second implicit deletion attempt from masking
    # the native failure or printing a success receipt before cleanup finishes.
    temporary = tempfile.TemporaryDirectory(prefix="lighttable-native-smoke-", delete=False)
    root = Path(temporary.name)
    original_error = cleanup_error = evidence_error = None
    try:
        yield root
    except BaseException as error:
        original_error = error
        report.update(ok=False, error=str(error))
        raise
    finally:
        try:
            if report_dir and (root / "server.log").exists():
                shutil.copy2(root / "server.log", report_dir / "server.log")
        except OSError as error:
            evidence_error = error
            report.update(ok=False, evidence_error=str(error))
        try:
            cleanup_temporary_directory(temporary)
        except OSError as error:
            cleanup_error = error
            report.update(ok=False, cleanup_error=str(error), retained_directory=str(root))
        try:
            if report_dir:
                (report_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            evidence_error = error
            report.update(ok=False, report_error=str(error))
        print(json.dumps(report))
        if original_error is None:
            if cleanup_error:
                raise cleanup_error
            if evidence_error:
                raise evidence_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Extracted portable Windows ZIP directory containing LightTable.exe")
    parser.add_argument("--photo", type=Path, help="Optional RAW/JPEG/TIFF to copy into the isolated catalog")
    parser.add_argument("--timeout", type=int, default=240,
                        help="Native workflow seconds; cleanup adds at most 15 seconds for processes and 10 for private files")
    parser.add_argument("--report-dir", type=Path, help="New directory for JSON evidence, logs and the exported TIFF")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run with packaged Python in a logged-on Windows desktop")
    if not 60 <= args.timeout <= 270:
        parser.error("--timeout must be between 60 and 270 seconds, plus at most 25 seconds of cleanup")
    require_interactive_desktop()
    bundle = args.bundle.resolve()
    validate_bundle(bundle)
    if Path(sys.executable).resolve() != (bundle / "Python/python.exe").resolve():
        parser.error("Run this script with the tested bundle's Python/python.exe")
    if args.report_dir:
        args.report_dir = args.report_dir.resolve()
        args.report_dir.mkdir(parents=True, exist_ok=False)
    import numpy as np
    import tifffile
    deadline = time.monotonic() + args.timeout
    report = {"ok": False, "desktop": "Windows/WebView2", "bundle": str(bundle)}
    manifest = bundle / "build-manifest.json"
    if manifest.is_file():
        report["build"] = json.loads(manifest.read_text(encoding="utf-8-sig"))
    with smoke_directory(report, args.report_dir) as root:
        environment = smoke_environment(root)
        (root / "support").mkdir()
        (root / "photos").mkdir()
        (root / "support/prefs.json").write_text(json.dumps({
            "firstRunSetup": {"version": 1, "status": "completed", "source": "folder"},
            "locale": "en", "localeChosen": True, "allowAutomation": True, "viewMode": "detail",
            "automaticUpdateChecks": False, "writeSidecars": False,
            "backupDirectory": str(root / "backups"),
        }), encoding="utf-8")
        desktop = None
        try:
            probe = subprocess.run([str(bundle / "LightTable.exe"), "--runtime-paths"], env=environment,
                                   cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
            if probe.returncode:
                raise RuntimeError("Native runtime-path probe failed; use a build supporting isolated desktop smoke")
            paths = json.loads(probe.stdout)
            validate_paths(paths, root, bundle)
            report["isolation"] = {key: str(Path(paths[key]).relative_to(root)) for key in MUTABLE_NATIVE_PATHS}
            if args.photo:
                source = root / "photos" / ("smoke" + args.photo.suffix.lower())
                shutil.copy2(args.photo, source)
            else:
                source = root / "photos/smoke.tif"
                gradient = np.tile(np.linspace(0, 65535, 1024, dtype=np.uint16)[None, :, None], (128, 1, 3))
                tifffile.imwrite(source, gradient, photometric="rgb", metadata=None)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            desktop = WindowsDesktop(bundle / "LightTable.exe", environment, root)
            api, health = connect(desktop, root, deadline)
            name, state = render_photo(desktop, api, deadline, source.name)
            report["initial_render"] = state["render"]
            send_ui_command(api, "slider", {"key": "exposure", "value": 0.5})
            send_ui_command(api, "rating:4")
            def edit_saved():
                saved = api.request("/api/state?" + urlencode({"name": name}))
                return saved if expected_edits_saved(saved) else None
            wait_for(desktop, deadline, "the UI exposure and rating edits to save", edit_saved)
            desktop.quit(min(20, max(1, deadline - time.monotonic())))
            desktop.close()
            desktop = None
            desktop = WindowsDesktop(bundle / "LightTable.exe", environment, root)
            api, restarted = connect(desktop, root, deadline)
            name, state = render_photo(desktop, api, deadline, source.name)
            saved = api.request("/api/state?" + urlencode({"name": name}))
            if not expected_edits_saved(saved):
                raise RuntimeError("The saved exposure/rating did not survive a native quit and relaunch")
            report["edit_persistence"] = {"exposure": 0.5, "rating": 4, "server_restarted": health["pid"] != restarted["pid"]}
            result = api.request("/api/export", {"names": [name], "format": "tif", "outputSpace": "srgb",
                                 "destination": str(root / "exports"), "metadata": "none", "sidecar": False,
                                 "collision": "rename"})
            if not result.get("queued"):
                raise RuntimeError("The native server did not queue the TIFF export")
            def exported():
                status = api.request("/api/export/status")
                if status.get("errors") or status.get("error"):
                    raise RuntimeError(f"TIFF export failed: {status.get('errors') or status.get('error')}")
                return status if not status.get("running") and status.get("done") == 1 else None
            wait_for(desktop, deadline, "RGB16 TIFF export", exported)
            outputs = list((root / "exports").rglob("*.tif"))
            if len(outputs) != 1:
                raise RuntimeError("Expected exactly one exported TIFF")
            report["export"] = verify_export(outputs[0], require_precision=args.photo is None)
            if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                raise RuntimeError("The native workflow modified its source photo")
            if not (root / "support/WebView2").is_dir():
                raise RuntimeError("The native WebView2 profile was not created in the isolated support directory")
            desktop.quit(min(20, max(1, deadline - time.monotonic())))
            report.update(ok=True, http=200, final_render=state["render"], source_sha256=source_hash)
            if args.report_dir:
                shutil.copy2(outputs[0], args.report_dir / "export.tif")
        except BaseException as error:
            report["error"] = str(error)
            report["startup_diagnostics"] = startup_diagnostics(root, desktop)
            if args.report_dir:
                report["failure_window"] = capture_failure_window(desktop, args.report_dir / "window.png")
            raise
        finally:
            if desktop:
                desktop.close()


if __name__ == "__main__":
    main()
