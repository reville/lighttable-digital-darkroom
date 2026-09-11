# SPDX-License-Identifier: GPL-3.0-only
"""Bound native-server stdout/stderr, including writes from native libraries.

Desktop launchers opt in with LIGHTTABLE_LOG_FILE. A single drain thread owns
rotation; the server remains the native launcher's direct child process.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import sys
import threading
import time


class RotatingLog:
    def __init__(self, path, *, max_bytes=8 * 1024 * 1024, backups=3):
        self.path = Path(path)
        self.max_bytes = max(256, int(max_bytes))
        self.backups = max(1, int(backups))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Older versions had no runtime limit. Retain the tail of oversized
        # existing generations so one old log cannot defeat today's budget.
        for candidate in [self.path, *(self.path.with_name(self.path.name + f".{i}")
                                       for i in range(1, self.backups + 1))]:
            if candidate.exists() and candidate.stat().st_size > self.max_bytes:
                with candidate.open("rb") as previous:
                    previous.seek(-self.max_bytes, os.SEEK_END)
                    tail = previous.read(self.max_bytes)
                with candidate.open("wb") as bounded:
                    bounded.write(tail)
        self.file = self.path.open("ab")
        self.size = self.file.tell()
        self.previous = None
        self.repeated = 0
        self.last_summary = time.monotonic()

    def _write(self, value):
        # A single malformed diagnostic cannot defeat the byte budget.
        value = value[:self.max_bytes]
        if self.size + len(value) > self.max_bytes:
            self.file.close()
            oldest = self.path.with_name(self.path.name + f".{self.backups}")
            oldest.unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_name(self.path.name + f".{index}")
                if source.exists():
                    source.replace(self.path.with_name(self.path.name + f".{index + 1}"))
            self.path.replace(self.path.with_name(self.path.name + ".1"))
            self.file = self.path.open("ab")
            self.size = 0
        self.file.write(value)
        self.file.flush()
        self.size += len(value)

    def _flush_repeated(self):
        if self.repeated:
            self._write(f"[previous diagnostic repeated {self.repeated} times]\n".encode())
            self.repeated = 0
        self.last_summary = time.monotonic()

    def write_line(self, value):
        if value == self.previous:
            self.repeated += 1
            if time.monotonic() - self.last_summary >= 5:
                self._flush_repeated()
            return
        self._flush_repeated()
        self.previous = value
        self._write(value)

    def close(self):
        self._flush_repeated()
        self.file.close()


def install(path):
    """Redirect descriptors 1/2, drain with bounded buffering, flush at exit."""
    sink = RotatingLog(path)
    sys.stdout.flush()
    sys.stderr.flush()
    original_out, original_err = os.dup(1), os.dup(2)
    reader, writer = os.pipe()
    os.dup2(writer, 1)
    os.dup2(writer, 2)
    os.close(writer)

    def drain():
        pending = b""
        failed = False
        try:
            while chunk := os.read(reader, 16 * 1024):
                pending += chunk
                while b"\n" in pending or len(pending) >= 64 * 1024:
                    end = pending.find(b"\n") + 1
                    if end <= 0 or end > 64 * 1024:
                        end = 64 * 1024
                    line, pending = pending[:end], pending[end:]
                    if not failed:
                        try:
                            sink.write_line(line)
                        except OSError:
                            # Continue draining if disk/logging fails. A full
                            # pipe must not stop the user from saving or quitting.
                            failed = True
            if pending and not failed:
                sink.write_line(pending)
        finally:
            os.close(reader)
            try:
                sink.close()
            except (OSError, ValueError):
                pass

    thread = threading.Thread(target=drain, name="lighttable-log", daemon=True)
    thread.start()

    def finish():
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(original_out, 1)
        os.dup2(original_err, 2)
        os.close(original_out)
        os.close(original_err)
        thread.join(timeout=2)

    atexit.register(finish)


def from_environment():
    path = os.environ.get("LIGHTTABLE_LOG_FILE")
    if path:
        try:
            install(path)
        except OSError as error:
            print(f"Runtime log rotation unavailable: {error}", file=sys.stderr)
