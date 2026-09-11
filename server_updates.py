# SPDX-License-Identifier: GPL-3.0-only
"""Coordinate desktop updates with the catalog's only writer.

Network downloads run outside request handlers. Preparing an update freezes new
mutations before checking outstanding work and making a verified catalog backup.
The native shell then waits for a normal server exit; it must never kill it to
make an update proceed.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from server_localization import T


class UpdateCoordinator:
    CONTROL_PATHS = frozenset({"/api/updates/prepare", "/api/updates/cancel",
                               "/api/updates/apply", "/api/updates/shutdown"})

    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.active_requests = 0
        self.phase = "idle"
        self.backup = None
        self.worker = None
        self.operation = None
        self.operation_error = None
        self.apply_process = None
        self.helper_directory = None

    @property
    def blocked(self):
        with self.lock:
            return self.phase != "idle"

    def busy(self, message=None):
        return self.server.APIError(409, message or T(
            "Finish the current work before updating LightTable."), "update-busy")

    def enter(self, path):
        with self.lock:
            if self.phase != "idle" and path not in self.CONTROL_PATHS:
                raise self.busy(T("LightTable is preparing to update. Try again after restarting."))
            self.active_requests += 1

    def leave(self):
        with self.lock:
            self.active_requests -= 1

    @contextmanager
    def background_work(self):
        with self.lock:
            allowed = self.phase == "idle"
            if allowed:
                self.active_requests += 1
        try:
            yield allowed
        finally:
            if allowed:
                self.leave()

    def _busy_jobs(self):
        server = self.server
        scanner = getattr(server, "SCANNER", None)
        if scanner and (scanner.status.get("running") or scanner.status.get("queued")):
            return True
        return any(record.get("state") in {"running", "queued"}
                   for record in server.JOBS.list()) or any(
            getattr(server, name, {}).get("running") for name in
            ("EXPORT", "IMPORT_JOB", "DENOISE", "EXTERNAL_EDIT"))

    def prepare(self):
        with self.lock:
            if self.phase == "ready":
                return {"ok": True, "backup": self.backup}
            if self.phase != "idle" or self.active_requests > 1 or self.operation:
                raise self.busy()
            self.phase = "preparing"
            if self._busy_jobs():
                self.phase = "idle"
                raise self.busy()
        try:
            # Save requests have drained before admission closes. SQLite's
            # backup API provides a consistent snapshot even if an idle index
            # service finishes its final transaction in the background.
            cat = self.server.open_catalog()
            try:
                backup = (str(cat.backup(self.server.configured_backup_directory(cat),
                                        label="before-update")) if cat else None)
            finally:
                if cat:
                    cat.close()  # Release this request thread's SQLite connection.
            with self.lock:
                if self.phase != "preparing":
                    raise self.busy(T("The update was cancelled."))
                if self._busy_jobs():
                    raise self.busy()
                self.backup = backup
                self.phase = "ready"
            return {"ok": True, "backup": backup}
        except Exception:
            with self.lock:
                self.phase = "idle"
            raise

    def cancel(self):
        with self.lock:
            if self.phase == "stopping":
                raise self.busy()
            # This worker cannot touch installed files until this server exits.
            # Reap it before restoring editing, so a later ordinary quit cannot
            # accidentally install an update that the user cancelled.
            if self.apply_process is not None:
                if self.helper_directory is not None:
                    (self.helper_directory / "cancelled").write_text("cancelled")
                if self.apply_process.poll() is None:
                    self.apply_process.terminate()
                    self.apply_process.wait(timeout=5)
                self.apply_process = None
                if self.worker is not None:
                    self.worker.cancel_apply()
                self.operation_error = None
            self.phase = "idle"
            self.backup = None
        return {"ok": True}

    def shutdown(self):
        with self.lock:
            if self.phase != "ready" or self.active_requests > 1 or self._busy_jobs():
                raise self.busy()
            if self.server.HTTPD is None:
                raise RuntimeError("server is not ready")
            self.phase = "stopping"
        threading.Thread(target=self.server.HTTPD.shutdown, daemon=True,
                         name="desktop-update-shutdown").start()
        return {"ok": True}

    def _linux(self):
        if sys.platform != "linux":
            raise ValueError("Portable updates are only available on Linux")
        if self.worker is None:
            from desktop_updater import LinuxUpdater
            self.worker = LinuxUpdater(self.server.APP.parents[1])
        return self.worker

    def status(self):
        if sys.platform != "linux":
            return {"supported": False, "owner": "native", "state": "disabled"}
        result = self._linux().status()
        with self.lock:
            if self.operation:
                result["state"] = self.operation
            if self.operation_error:
                result.update(state="error", error=self.operation_error)
        return result

    def start(self, action, *, automatic=False):
        worker = self._linux()
        if action not in {"check", "download"}:
            raise ValueError("Unknown update operation")
        with self.lock:
            if self.phase != "idle" or self.operation:
                raise self.busy()
            if not worker.status().get("supported"):
                return self.status()
            if automatic:
                prefs = self.server.load_preferences()
                if not prefs.get("automaticUpdateChecks", True):
                    return self.status()
                stamp = self.server.CACHE / "automatic-update-check.json"
                try:
                    last = float(json.loads(stamp.read_text()).get("at", 0))
                except (OSError, ValueError, TypeError):
                    last = 0
                if 0 <= time.time() - last < 86400:
                    return self.status()
                self.server.durable_io.atomic_write_json(stamp, {"at": time.time()},
                                                         keep_backup=False)
            self.operation = "checking" if action == "check" else "downloading"
            self.operation_error = None

        def run():
            try:
                getattr(worker, action)()
            except Exception as error:
                with self.lock:
                    self.operation_error = str(error)
            finally:
                with self.lock:
                    self.operation = None
        threading.Thread(target=run, daemon=True, name="desktop-update-" + action).start()
        return self.status()

    def apply(self):
        worker = self._linux()
        with self.lock:
            if self.phase != "ready" or self.active_requests > 1 or self.operation:
                raise self.busy()
            if self.apply_process is not None:
                raise self.busy()
            if worker.status().get("state") != "ready":
                raise ValueError(T("Download the update before restarting."))
            parent = int(os.environ.get("LIGHTTABLE_PARENT_PID", "0"))
            if parent <= 1:
                raise ValueError(T("Open the desktop app to install this update."))
            acknowledgment = worker.state_dir / ("armed-" + secrets.token_hex(16) + ".json")
            helper_root = self.server.CACHE / "updates"
            helper_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if helper_root.is_symlink():
                raise ValueError("Update helper directory cannot be a symbolic link")
            self.helper_directory = helper_root / ("pending-" + secrets.token_hex(16))
            self.helper_directory.mkdir(mode=0o700)
            launch_env = {name: value for name, value in os.environ.items()
                          if name in {"XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
                          and Path(value).is_absolute()}
            launch_env["LIGHTTABLE_DIR"] = str(self.server.FOLDER)
            if os.environ.get("LIGHTTABLE_CATALOG_FILE"):
                launch_env["LIGHTTABLE_CATALOG_FILE"] = os.environ["LIGHTTABLE_CATALOG_FILE"]
            command = [sys.executable, "-I", "-B", str(self.server.APP / "desktop_updater.py"),
                       "apply", "--bundle", str(self.server.APP.parents[1]),
                       "--wait-pid", str(os.getpid()), "--wait-pid", str(parent),
                       "--authorization-directory", str(self.helper_directory),
                       "--armed-file", str(acknowledgment), "--launch-env-json", json.dumps(launch_env)]
            log_path = self.server.CACHE / "desktop-update.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                self.apply_process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if acknowledgment.is_file():
                    acknowledgment.unlink()
                    return {"ok": True, "helper_directory": str(self.helper_directory)}
                if self.apply_process.poll() is not None:
                    break
                time.sleep(0.05)
            self.cancel()
            acknowledgment.unlink(missing_ok=True)
            raise RuntimeError(T("The updater could not start. LightTable is still open."))
