# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in crash reports.

A report is assembled field by field from an allowlist: the LightTable build,
the operating system, processor architecture, memory and (on a Mac) the model
identifier, and where a crash happened in program code. It never includes
photos, photo metadata, file or folder names, paths, catalog contents, log
text or any identifier for the computer or person.

Nothing is sent unless the person agreed (the ``crashReports`` preference is
true) and the running copy is a packaged build. ``LIGHTTABLE_CRASH_REPORTS=0``
turns the feature off entirely; ``=1`` enables it in a development checkout
for testing against ``LIGHTTABLE_CRASH_REPORT_URL``.

Sources, by platform:

* macOS: the native shell records every unexpected exit of the app or the
  rendering engine as an ``incident-*.json`` in ``LIGHTTABLE_DIAGNOSTICS_DIR``.
  The matching system crash report adds native frames.
* Windows and Linux: the server's session ledger notices that the previous
  run never ended, and ``fatal_diagnostics`` kept that run's fatal stack.
  Only a run that left a fatal stack is reported, so a forced quit is not.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import durable_io

SCHEMA = 1
DEFAULT_ENDPOINT = "https://reports.lighttable.app/v1/crash"
PREFERENCE = "crashReports"
NATIVE_INCIDENT_VERSION = 1

MAX_PENDING = 20
MAX_AGE_SECONDS = 14 * 24 * 3600
MAX_ATTEMPTS = 6
MAX_SENDS_PER_RUN = 5
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_THREADS = 32
MAX_FRAMES = 64
MAX_MODULES = 128
MAX_NATIVE_FRAMES = 64
NETWORK_TIMEOUT = 10
RETRY_DELAYS = (3600, 6 * 3600, 24 * 3600)
SWIFT_REFERENCE_EPOCH = 978307200  # Foundation dates count from 2001-01-01.

PLATFORMS = {"darwin": "macos", "win32": "windows"}
OPERATIONS = {"decode", "render", "export"}
PACKAGING = {"macos-app", "portable", "arch", "snap", "flatpak", "rpm", "deb",
             "package-manager", "windows", "windows-store"}
# Signal numbers as the macOS shell reports them; only it knows an exit's cause.
MAC_SIGNALS = {4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT", 8: "SIGFPE", 9: "SIGKILL",
               10: "SIGBUS", 11: "SIGSEGV", 12: "SIGSYS", 13: "SIGPIPE", 15: "SIGTERM"}
SPECIAL_FUNCTIONS = {"<module>", "<lambda>", "<listcomp>", "<dictcomp>",
                     "<setcomp>", "<genexpr>"}
MARKERS = {"<invalid frame>": "invalid-frame", "<no Python frame>": "no-python-frame",
           "...": "truncated"}

_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}(?:[-+][A-Za-z0-9.]{1,32})?")
_BUILD = re.compile(r"[A-Za-z0-9._-]{1,32}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_OS_VERSION = re.compile(r"[0-9A-Za-z._ -]{1,40}")
_OS_BUILD = re.compile(r"[0-9A-Za-z._-]{1,40}")
_DISTRIBUTION = re.compile(r"[a-z0-9._-]{1,32}(?: [0-9A-Za-z._-]{1,20})?")
_ARCH = re.compile(r"[A-Za-z0-9_]{1,16}")
_MODEL = re.compile(r"[A-Za-z0-9,._ -]{1,40}")
_FATAL = re.compile(r"[A-Za-z0-9 _.,:()'-]{1,100}")
_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_SOURCE = re.compile(r".{1,60}\.py[cw]?")
_MODULE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_EXTENSION = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,80}")
_EXCEPTION_TYPE = re.compile(r"[A-Z0-9_]{1,40}")
_EXCEPTION_CODES = re.compile(r"[A-Za-z0-9 ,x_]{1,80}")
_NATIVE_TEXT = re.compile(r"[A-Za-z0-9 _:,.()x-]{1,100}")
_IMAGE = re.compile(r"[A-Za-z0-9_.+ -]{1,80}")
_SYMBOL = re.compile(r"[A-Za-z0-9_:~<>*&(),.\[\] $+'-]{1,200}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_THREAD = re.compile(r"(Current thread|Thread) 0x[0-9a-fA-F]+ \(most recent call first\):")
_QUOTED_FRAME = re.compile(r'File "(?P<path>[^"\r\n]*)", line (?P<line>\d{1,9}) in (?P<function>\S+)')
_BARE_FRAME = re.compile(r"File (?P<path>[^\s\"]+\.py[cw]?), line (?P<line>\d{1,9}) in (?P<function>\S+)")
_FATAL_LINE = re.compile(r"(?:Fatal Python error|Windows fatal exception): (?P<message>.+)")


def _text(pattern: re.Pattern, value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if pattern.fullmatch(value) else None


def _integer(value, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _now() -> float:
    return time.time()


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def _environment_switch(environ) -> bool | None:
    value = str(environ.get("LIGHTTABLE_CRASH_REPORTS", "")).strip().lower()
    if value in {"0", "off", "false", "no"}:
        return False
    if value in {"1", "on", "true", "yes"}:
        return True
    return None


# ------------------------------------------------------------ build facts --

def build_identity(app_dir: Path) -> dict | None:
    """The packaged build this code belongs to, or None for a checkout."""
    app_dir = Path(app_dir)
    contents = app_dir.parent.parent if app_dir.parent.name == "Resources" else None
    if contents is not None and contents.name == "Contents" \
            and (contents / "Info.plist").is_file():
        try:
            info = plistlib.loads((contents / "Info.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        return {
            "version": _text(_VERSION, info.get("CFBundleShortVersionString")),
            "build": _text(_BUILD, info.get("CFBundleVersion")),
            "revision": _text(_REVISION, info.get("LightTableSourceRevision")),
            "modified": info.get("LightTableSourceDirty")
            if isinstance(info.get("LightTableSourceDirty"), bool) else None,
            "packaging": "macos-app",
        }
    bundle = contents
    if bundle is None or app_dir.name != "LightTable" \
            or not (bundle / "build-manifest.json").is_file():
        return None
    try:
        manifest = json.loads((bundle / "build-manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    packaging = "windows" if sys.platform == "win32" else "portable"
    if sys.platform == "win32" and "WindowsApps" in bundle.parts:
        packaging = "windows-store"
    elif os.environ.get("SNAP"):
        packaging = "snap"
    elif os.environ.get("FLATPAK_ID"):
        packaging = "flatpak"
    try:
        owner = json.loads((bundle / "installation-owner.json").read_text()).get("owner")
    except (OSError, ValueError, AttributeError):
        owner = None
    if owner in PACKAGING:
        packaging = owner
    dirty = manifest.get("source_dirty")
    return {
        "version": _text(_VERSION, manifest.get("version")),
        "build": _text(_BUILD, manifest.get("build")),
        "revision": _text(_REVISION, manifest.get("source_revision")),
        "modified": dirty if isinstance(dirty, bool) else None,
        "packaging": packaging,
    }


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _memory_gb() -> int | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                            ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                            ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                            ("extended", ctypes.c_ulonglong)]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            total = status.total
        else:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    return _integer(round(total / 1024 ** 3), 0, 4096)


def _linux_distribution() -> str | None:
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values = {}
    for line in lines:
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    name = " ".join(filter(None, (values.get("ID"), values.get("VERSION_ID"))))
    return _text(_DISTRIBUTION, name)


def system_facts() -> dict:
    name = PLATFORMS.get(sys.platform, "linux")
    facts = {"platform": name, "osVersion": None, "osBuild": None, "distribution": None,
             "arch": _text(_ARCH, platform.machine().lower() or None),
             "model": None, "memoryGB": _memory_gb(),
             "cpuCount": _integer(os.cpu_count(), 1, 1024)}
    if name == "macos":
        facts["osVersion"] = _text(_OS_VERSION, platform.mac_ver()[0])
        facts["osBuild"] = _text(_OS_BUILD, _sysctl("kern.osversion"))
        facts["model"] = _text(_MODEL, _sysctl("hw.model"))
    elif name == "windows":
        release, version, *_ = platform.win32_ver()
        facts["osVersion"] = _text(_OS_VERSION, release)
        facts["osBuild"] = _text(_OS_BUILD, version)
    else:
        facts["osVersion"] = _text(_OS_VERSION, platform.release())
        facts["distribution"] = _linux_distribution()
    return facts


# ------------------------------------------------------------ fatal stacks --

def _frame_file(path: str, roots: list[tuple[str, Path]]) -> str:
    """Name a code file by where it lives, never by the full path."""
    frozen = re.fullmatch(r"<frozen ([A-Za-z0-9_.]{1,80})>", path)
    if frozen:
        return f"frozen:{frozen.group(1)}"
    normalized = path.replace("\\", "/")
    if "/" not in normalized:
        # The macOS shell already reduced the path to a file name.
        return f"code:{normalized}" if _MODULE_PART.fullmatch(normalized) \
            and _SOURCE.fullmatch(normalized) else "<other>"
    for label, root in roots:
        prefix = str(root).replace("\\", "/").rstrip("/") + "/"
        if normalized.startswith(prefix):
            parts = normalized[len(prefix):].split("/")
            if 0 < len(parts) <= 8 and all(_MODULE_PART.fullmatch(part) for part in parts) \
                    and _SOURCE.fullmatch(parts[-1]):
                return f"{label}:{'/'.join(parts)}"
            break
    return "<other>"


def _function(value: str) -> str:
    return value if value in SPECIAL_FUNCTIONS or _FUNCTION.fullmatch(value) else "<other>"


def code_roots(app_dir: Path) -> list[tuple[str, Path]]:
    """Directories whose relative file names are program code, deepest first.

    A stack names the file the interpreter loaded, which can differ from the
    configured directory by a symlink: a Homebrew interpreter reports its
    Cellar path where sysconfig names the opt path, so both forms count.
    """
    import sysconfig

    paths = sysconfig.get_paths()
    candidates = [("app", Path(app_dir)), ("lib", Path(os.__file__).parent),
                  ("site", paths.get("purelib")), ("site", paths.get("platlib")),
                  ("lib", paths.get("stdlib"))]
    roots: list[tuple[str, Path]] = []
    for label, value in candidates:
        if not value:
            continue
        path = Path(value)
        for form in (path, path.resolve()):
            if all(form != root for _, root in roots):
                roots.append((label, form))
    return sorted(roots, key=lambda item: -len(str(item[1])))


def parse_fault_trace(text: str, roots: list[tuple[str, Path]]) -> dict:
    """Allowlisted facts from faulthandler output; everything else is dropped."""
    result = {"fatalError": None, "threads": [], "extensionModules": []}
    thread = None
    for raw in str(text or "").splitlines()[:4000]:
        line = raw.strip()
        if not line:
            continue
        fatal = _FATAL_LINE.fullmatch(line)
        if fatal and result["fatalError"] is None:
            message = fatal.group("message").strip()
            result["fatalError"] = message if _FATAL.fullmatch(message) \
                and "/" not in message and "\\" not in message else "<other>"
            continue
        header = _THREAD.fullmatch(line)
        if header or line == "Stack (most recent call first):":
            if len(result["threads"]) >= MAX_THREADS:
                thread = None
                continue
            thread = {"current": bool(header and header.group(1) == "Current thread"),
                      "frames": []}
            result["threads"].append(thread)
            continue
        if line.startswith("Extension modules:"):
            names = line.partition(":")[2].split("(total:")[0]
            result["extensionModules"] = [
                name for name in (part.strip() for part in names.split(","))
                if _EXTENSION.fullmatch(name)][:MAX_MODULES]
            thread = None
            continue
        if thread is None or len(thread["frames"]) >= MAX_FRAMES:
            continue
        if line in MARKERS:
            thread["frames"].append({"marker": MARKERS[line]})
            continue
        frame = _QUOTED_FRAME.fullmatch(line) or _BARE_FRAME.fullmatch(line)
        if frame:
            thread["frames"].append({
                "file": _frame_file(frame.group("path"), roots),
                "line": _integer(frame.group("line"), 0, 10 ** 7) or 0,
                "function": _function(frame.group("function")),
            })
    result["threads"] = [item for item in result["threads"] if item["frames"]]
    return result


# ------------------------------------------------------ macOS crash files --

def native_summary(payload: dict) -> dict:
    """Allowlisted fields of one macOS .ips crash payload."""
    exception = payload.get("exception") if isinstance(payload.get("exception"), dict) else {}
    termination = payload.get("termination") if isinstance(payload.get("termination"), dict) else {}
    images = payload.get("usedImages") if isinstance(payload.get("usedImages"), list) else []
    threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
    faulting = _integer(payload.get("faultingThread"), 0, 10 ** 6)
    crashed = threads[faulting] if faulting is not None and faulting < len(threads) else None
    if not isinstance(crashed, dict):
        crashed = next((item for item in threads
                        if isinstance(item, dict) and item.get("triggered")), {})
    frames, used = [], {}
    for frame in (crashed.get("frames") or [])[:MAX_NATIVE_FRAMES]:
        if not isinstance(frame, dict):
            continue
        index = _integer(frame.get("imageIndex"), 0, 10 ** 5)
        image = images[index] if index is not None and index < len(images) else None
        name = _text(_IMAGE, image.get("name")) if isinstance(image, dict) else None
        if isinstance(image, dict) and name and index not in used:
            used[index] = {"name": name, "uuid": _text(_UUID, image.get("uuid")),
                           "arch": _text(_ARCH, image.get("arch"))}
        frames.append({"image": name, "symbol": _text(_SYMBOL, frame.get("symbol")),
                       "offset": _integer(frame.get("imageOffset"), 0, 2 ** 48)})
    indicator = termination.get("indicator")
    return {
        "exceptionType": _text(_EXCEPTION_TYPE, exception.get("type")),
        "exceptionCodes": _text(_EXCEPTION_CODES, exception.get("codes")),
        "exceptionSubtype": _text(_NATIVE_TEXT, exception.get("subtype")),
        "termination": _text(_NATIVE_TEXT, indicator),
        "frames": frames,
        "images": list(used.values()),
    }


def find_mac_crash(pid: int, executable_names: set[str], started: float,
                   detected: float, directory: Path | None = None) -> dict | None:
    """The system crash report for exactly this process and launch interval."""
    directory = directory or Path.home() / "Library/Logs/DiagnosticReports"
    try:
        entries = [(entry, entry.stat()) for entry in directory.iterdir()
                   if entry.suffix == ".ips"]
    except OSError:
        return None
    candidates = sorted(((stat.st_mtime, entry) for entry, stat in entries
                         if stat.st_size <= 2 * 1024 * 1024
                         and started - 5 <= stat.st_mtime <= detected + 600),
                        reverse=True)[:40]
    for _, entry in candidates:
        try:
            header, _, body = entry.read_text(encoding="utf-8").partition("\n")
            meta = json.loads(header)
            payload = json.loads(body)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(meta, dict) or not isinstance(payload, dict):
            continue
        if str(meta.get("bug_type")) != "309" or payload.get("pid") != pid:
            continue
        names = {Path(str(payload.get("procPath") or "")).name, str(payload.get("procName") or "")}
        if names & executable_names:
            return native_summary(payload)
    return None


# ----------------------------------------------------------------- reports --

def assemble(identity: dict, system: dict, *, component: str, detected: float,
             started: float | None, exit_status, operation, trace: dict,
             native: dict | None, exit_reason: str | None = None) -> dict:
    status = _integer(exit_status, -1000, 1000)
    signal = MAC_SIGNALS.get(status) if exit_reason == "signal" else None
    return {
        "schema": SCHEMA,
        "id": str(uuid.uuid4()),
        "app": dict(identity),
        "system": dict(system),
        "crash": {
            "component": component,
            "detectedOn": _day(detected),
            "uptimeSeconds": _integer(round(detected - started), 0, 10 ** 8)
            if started is not None else None,
            "exitStatus": status,
            "signal": signal,
            "operation": operation if operation in OPERATIONS else "unknown",
            "fatalError": trace.get("fatalError"),
            "threads": trace.get("threads", []),
            "extensionModules": trace.get("extensionModules", []),
            "native": native,
        },
    }


def validate(payload: dict) -> list[str]:
    """Shape checks shared with the relay; a failure means a local bug."""
    problems = []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return ["schema"]
    app, system, crash = (payload.get(key) for key in ("app", "system", "crash"))
    if not isinstance(app, dict) or not _text(_VERSION, app.get("version")):
        problems.append("app.version")
    if not isinstance(system, dict) or system.get("platform") not in set(PLATFORMS.values()) | {"linux"}:
        problems.append("system.platform")
    if not isinstance(crash, dict) or crash.get("component") not in {"app", "engine"}:
        problems.append("crash.component")
    elif not crash.get("threads") and not crash.get("native") and crash.get("exitStatus") is None:
        problems.append("crash.evidence")
    if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
        problems.append("size")
    return problems


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CrashReporter:
    """Collect reports at launch, keep them in an outbox, send with consent."""

    def __init__(self, *, root: Path, app_dir: Path, prefs, fault_logs: Path | None = None,
                 own_fault: Path | None = None, native_dir: Path | None = None,
                 mac_reports: Path | None = None, environ=None, identity=None,
                 system=None, opener=None, clock=_now):
        self.environ = os.environ if environ is None else environ
        self.root = Path(root)
        self.app_dir = Path(app_dir)
        self.prefs = prefs
        # Directory of server-owned fault files; None when a host owns them.
        self.fault_logs = Path(fault_logs) if fault_logs else None
        self.own_fault = Path(own_fault) if own_fault else None
        configured = self.environ.get("LIGHTTABLE_DIAGNOSTICS_DIR")
        self.native_dir = Path(configured) if configured else native_dir
        self.mac_reports = mac_reports
        self.endpoint = self.environ.get("LIGHTTABLE_CRASH_REPORT_URL") or DEFAULT_ENDPOINT
        self._identity = identity
        self._system = system
        self._opener = opener or build_opener(NoRedirect)
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._previous: dict | None = None
        self._collect_next = False
        self._last: dict = {}

    # --- policy

    @property
    def pending_dir(self) -> Path:
        return self.root / "pending"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.json"

    def identity(self) -> dict | None:
        if self._identity is None:
            identity = build_identity(self.app_dir)
            if identity is None and _environment_switch(self.environ):
                import app_version

                identity = {"version": _text(_VERSION, app_version.VERSION), "build": None,
                            "revision": None, "modified": None, "packaging": "development"}
            self._identity = identity or {}
        return self._identity or None

    def available(self) -> bool:
        switch = _environment_switch(self.environ)
        if switch is False:
            return False
        identity = self.identity()
        return bool(identity and identity.get("version"))

    def consent(self) -> bool | None:
        try:
            value = (self.prefs() or {}).get(PREFERENCE)
        except Exception:  # noqa: BLE001 - unreadable prefs mean no decision
            return None
        return value if isinstance(value, bool) else None

    def status(self) -> dict:
        with self._lock:
            ledger = self._ledger()
            return {
                "available": self.available(),
                "consent": self.consent(),
                "pending": len(self._pending_files()),
                "lastSentAt": ledger.get("lastSentAt"),
                "lastError": self._last.get("error"),
            }

    # --- ledger and outbox

    def _ledger(self) -> dict:
        value = durable_io.load_json(self.ledger_path, {})
        if not isinstance(value, dict):
            value = {}
        value.setdefault("processed", {})
        return value

    def _save_ledger(self, ledger: dict) -> None:
        processed = ledger.get("processed", {})
        if len(processed) > 200:
            keep = sorted(processed.items(), key=lambda item: item[1])[-200:]
            ledger["processed"] = dict(keep)
        self.root.mkdir(parents=True, exist_ok=True)
        durable_io.atomic_write_json(self.ledger_path, ledger)

    def _pending_files(self) -> list[Path]:
        try:
            return sorted(self.pending_dir.glob("*.json"))
        except OSError:
            return []

    def _queue(self, payload: dict) -> bool:
        if validate(payload):
            return False
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        record = {"createdAt": self._clock(), "attempts": 0, "nextAttemptAt": 0,
                  "payload": payload}
        durable_io.atomic_write_json(self.pending_dir / f"{payload['id']}.json", record)
        for stale in self._pending_files()[:-MAX_PENDING]:
            stale.unlink(missing_ok=True)
        return True

    def purge(self) -> None:
        with self._lock:
            for path in self._pending_files():
                path.unlink(missing_ok=True)

    # --- collection

    def _python_names(self) -> set[str]:
        names = {Path(sys.executable).name}
        try:
            names.add(Path(os.path.realpath(sys.executable)).name)
        except OSError:
            pass
        return names - {""}

    def _native_incidents(self) -> list[dict]:
        if self.native_dir is None:
            return []
        incidents = []
        try:
            paths = sorted(self.native_dir.glob("incident-*.json"))
        except OSError:
            return []
        for path in paths[-20:]:
            try:
                if path.stat().st_size > 512 * 1024:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(record, dict) and record.get("reportVersion") == NATIVE_INCIDENT_VERSION:
                incidents.append(record)
        return incidents

    def _native_report(self, record: dict, roots: list) -> dict | None:
        kind = record.get("kind")
        if kind not in {"app", "engine"}:
            return None
        try:
            started = float(record["startedAt"]) + SWIFT_REFERENCE_EPOCH
            detected = float(record["detectedAt"]) + SWIFT_REFERENCE_EPOCH
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        if self._clock() - detected > MAX_AGE_SECONDS:
            return None
        identity = dict(self.identity() or {})
        for key, field, pattern in (("appVersion", "version", _VERSION),
                                    ("appBuild", "build", _BUILD),
                                    ("sourceRevision", "revision", _REVISION)):
            if key in record:
                identity[field] = _text(pattern, record.get(key))
        if isinstance(record.get("sourceModified"), bool):
            identity["modified"] = record["sourceModified"]
        if not identity.get("version"):
            return None
        executable = Path(str(record.get("executable") or "")).name
        names = {executable} - {""}
        if kind == "engine":
            names |= self._python_names()
        native = (find_mac_crash(pid, names, started, detected, self.mac_reports)
                  if sys.platform == "darwin" else None)
        trace = parse_fault_trace(record.get("errorTrace") or "", roots)
        return assemble(identity, self.system(), component=kind, detected=detected,
                         started=started, exit_status=record.get("exitStatus"),
                         exit_reason=record.get("exitReason"),
                         operation=record.get("operation"), trace=trace, native=native)

    def system(self) -> dict:
        if self._system is None:
            self._system = system_facts()
        return self._system

    def _engine_report(self, crashed: dict, roots: list) -> dict | None:
        pid = _integer(crashed.get("pid"), 1, 2 ** 31)
        if self.fault_logs is None or pid is None:
            return None
        try:
            started = float(crashed.get("startedAt"))
            detected = float(crashed.get("detectedAt") or self._clock())
        except (TypeError, ValueError):
            return None
        path = self.fault_logs / f"server-fault-{pid}.log"
        try:
            if path.stat().st_mtime < started - 60:
                return None  # An older run with a reused process ID.
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 256 * 1024))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        trace = parse_fault_trace(text, roots)
        if not trace["threads"] and trace["fatalError"] is None:
            return None  # A forced quit leaves no stack; only report real crashes.
        inflight = crashed.get("inflight") if isinstance(crashed.get("inflight"), dict) else {}
        return assemble(self.identity(), self.system(), component="engine",
                        detected=detected, started=started,
                        exit_status=crashed.get("exitStatus"),
                        operation=inflight.get("stage"), trace=trace, native=None)

    def _prune_fault_logs(self) -> None:
        """Only this server is alive on this catalog, so older files are done."""
        if self.fault_logs is None:
            return
        try:
            leftovers = list(self.fault_logs.glob("server-fault-*.log"))
        except OSError:
            return
        for path in leftovers:
            if self.own_fault is None or path != self.own_fault:
                path.unlink(missing_ok=True)

    def collect(self, crashed: dict | None) -> int:
        """Turn unprocessed crashes into pending reports; returns how many."""
        if not self.available():
            return 0
        consent = self.consent()
        queued = 0
        with self._lock:
            ledger = self._ledger()
            processed = ledger["processed"]
            roots = code_roots(self.app_dir)
            candidates = []
            if self.native_dir is not None:
                for record in self._native_incidents():
                    candidates.append((f"native:{record.get('id')}",
                                       lambda item=record: self._native_report(item, roots)))
            elif crashed:
                key = f"engine:{crashed.get('startedAt')}:{crashed.get('pid')}"
                candidates.append((key, lambda: self._engine_report(crashed, roots)))
            for key, build in candidates:
                if key in processed:
                    continue
                processed[key] = self._clock()
                if consent is False:
                    continue
                try:
                    payload = build()
                except Exception:  # noqa: BLE001 - a report must never break launch
                    payload = None
                if payload is not None and self._queue(payload):
                    queued += 1
            self._prune_fault_logs()
            self._save_ledger(ledger)
            if consent is False:
                self.purge()
        return queued

    # --- delivery

    def _post(self, payload: dict) -> int:
        body = json.dumps(payload, separators=(",", ":")).encode()
        platform_name = payload.get("system", {}).get("platform", "unknown")
        request = Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent": f"LightTable/{payload['app']['version']} ({platform_name})",
        })
        try:
            with self._opener.open(request, timeout=NETWORK_TIMEOUT) as response:
                response.read(4096)
                return response.status
        except HTTPError as error:
            return error.code

    def send_pending(self) -> dict:
        """Deliver due reports. Returns counts for tests and diagnostics."""
        outcome = {"sent": 0, "dropped": 0, "deferred": 0}
        if not self.available() or self.consent() is not True:
            return outcome
        if not self.endpoint.startswith("https://") and not self.endpoint.startswith("http://127.0.0.1"):
            return outcome
        with self._lock:
            files = list(reversed(self._pending_files()))
        attempts = 0
        for path in files:
            record = durable_io.load_json(path, None)
            now = self._clock()
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict) \
                    or now - float(record.get("createdAt") or 0) > MAX_AGE_SECONDS \
                    or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
                path.unlink(missing_ok=True)
                outcome["dropped"] += 1
                continue
            if float(record.get("nextAttemptAt") or 0) > now or attempts >= MAX_SENDS_PER_RUN:
                outcome["deferred"] += 1
                continue
            if self.consent() is not True:
                break
            attempts += 1
            try:
                status = self._post(record["payload"])
            except (OSError, URLError, ValueError) as error:
                status, self._last = None, {"error": type(error).__name__}
            if status is not None and 200 <= status < 300:
                path.unlink(missing_ok=True)
                with self._lock:
                    ledger = self._ledger()
                    ledger["lastSentAt"] = now
                    self._save_ledger(ledger)
                self._last = {}
                outcome["sent"] += 1
            elif status in {400, 404, 410, 413, 415, 422}:
                path.unlink(missing_ok=True)
                self._last = {"error": f"rejected {status}"}
                outcome["dropped"] += 1
            else:
                if status is not None:
                    self._last = {"error": f"status {status}"}
                tries = int(record.get("attempts") or 0) + 1
                record.update(attempts=tries, nextAttemptAt=now + RETRY_DELAYS[
                    min(tries, len(RETRY_DELAYS)) - 1])
                durable_io.atomic_write_json(path, record)
                outcome["deferred"] += 1
        return outcome

    # --- background work

    def use_fault_logs(self, directory: Path | None, own: Path | None) -> None:
        """Where servers without a host-owned fault file keep theirs."""
        self.fault_logs = Path(directory) if directory else None
        self.own_fault = Path(own) if own else None

    def start(self, crashed: dict | None) -> None:
        """Collect this launch's reports and send them without blocking."""
        self._previous = crashed
        if not self.available():
            self._prune_fault_logs()
            return
        self.trigger(collect=True)

    def trigger(self, *, collect: bool = False) -> None:
        if not self.available():
            return
        with self._lock:
            if collect:
                self._collect_next = True
            self._wake.set()
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, name="lighttable-crash-reports",
                                            daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._wake.is_set():
                    self._worker = None
                    return
                self._wake.clear()
                collect = self._collect_next
                self._collect_next = False
            try:
                if collect:
                    self.collect(self._previous)
                self.send_pending()
            except Exception as error:  # noqa: BLE001 - reporting must never crash the app
                self._last = {"error": type(error).__name__}

    def preference_changed(self, value) -> None:
        if value is True:
            self.trigger()
        elif value is False:
            self.purge()
