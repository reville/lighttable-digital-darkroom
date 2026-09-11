# SPDX-License-Identifier: GPL-3.0-only
"""Launcher lifetime control must not become a helper's blocking stdin."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import Mock, patch

import launcher_control

ROOT = Path(__file__).resolve().parents[1]


class LauncherControlTests(unittest.TestCase):
    def test_other_platforms_and_unconfigured_windows_do_not_touch_stdin(self):
        for platform, flag in (("linux", "1"), ("darwin", "1"),
                               ("win32", None), ("win32", "0")):
            with self.subTest(platform=platform, flag=flag), \
                    patch.object(launcher_control.sys, "platform", platform), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(launcher_control.os, "dup") as duplicate:
                if flag is not None:
                    os.environ["LIGHTTABLE_WATCH_STDIN"] = flag
                self.assertIsNone(launcher_control.separate_launcher_stdin())
                duplicate.assert_not_called()

    def test_private_reader_is_non_inheritable_before_stdin_is_replaced(self):
        calls = Mock()
        reader = Mock()
        calls.dup.return_value = 40
        calls.fdopen.return_value = reader
        calls.open.return_value = 41
        with patch.object(launcher_control.sys, "platform", "win32"), \
                patch.dict(os.environ, LIGHTTABLE_WATCH_STDIN="1"), \
                patch.multiple(launcher_control.os, **{
                    name: getattr(calls, name) for name in
                    ("dup", "set_inheritable", "fdopen", "open", "dup2", "close")}), \
                patch.object(launcher_control, "_set_standard_input", calls.set_standard_input):
            self.assertIs(launcher_control.separate_launcher_stdin(), reader)
        from unittest.mock import call
        self.assertEqual(calls.mock_calls, [
            call.dup(0), call.set_inheritable(40, False),
            call.fdopen(40, "rb", buffering=0),
            call.open(os.devnull, os.O_RDONLY | getattr(os, "O_BINARY", 0)),
            call.dup2(41, 0, inheritable=True), call.set_standard_input(0), call.close(41),
        ])
        reader.close.assert_not_called()

    def test_setup_errors_survive_failed_cleanup_and_close_only_owned_descriptors(self):
        for failing in ("dup", "set_inheritable", "fdopen", "open", "dup2", "set_standard_input"):
            with self.subTest(failing=failing):
                failure = OSError(123, "original handle failure")
                cleanup_failure = OSError(456, "cleanup failure")
                calls, reader = Mock(), Mock()
                calls.dup.return_value = 40
                calls.fdopen.return_value = reader
                calls.open.return_value = 41
                calls.close.side_effect = cleanup_failure
                reader.close.side_effect = cleanup_failure
                getattr(calls, failing).side_effect = failure
                with patch.object(launcher_control.sys, "platform", "win32"), \
                        patch.dict(os.environ, LIGHTTABLE_WATCH_STDIN="1"), \
                        patch.multiple(launcher_control.os, **{
                            name: getattr(calls, name) for name in
                            ("dup", "set_inheritable", "fdopen", "open", "dup2", "close")}), \
                        patch.object(launcher_control, "_set_standard_input", calls.set_standard_input), \
                        self.assertRaises(OSError) as raised:
                    launcher_control.separate_launcher_stdin()
                self.assertIs(raised.exception, failure)
                closed = [entry.args[0] for entry in calls.close.call_args_list]
                self.assertEqual(closed, {
                    "dup": [], "set_inheritable": [40], "fdopen": [40],
                    "open": [], "dup2": [41], "set_standard_input": [41],
                }[failing])
                self.assertEqual(reader.close.call_count,
                                 int(failing in ("open", "dup2", "set_standard_input")))

    def test_win32_handle_update_uses_pointer_sized_handle_and_reports_native_error(self):
        handle = 0x123456789ABC
        kernel, crt = Mock(), Mock()
        crt.get_osfhandle.return_value = handle
        kernel.SetStdHandle.return_value = 1
        native_failure = OSError(6, "invalid handle")
        with patch.dict(sys.modules, msvcrt=crt), \
                patch.object(ctypes, "WinDLL", create=True, return_value=kernel), \
                patch.object(ctypes, "get_last_error", create=True, return_value=6), \
                patch.object(ctypes, "WinError", create=True, return_value=native_failure) as error:
            launcher_control._set_standard_input(0)
            self.assertEqual(kernel.SetStdHandle.argtypes, [wintypes.DWORD, wintypes.HANDLE])
            self.assertIs(kernel.SetStdHandle.restype, wintypes.BOOL)
            kernel.SetStdHandle.assert_called_once_with(0xFFFFFFF6, handle)
            crt.get_osfhandle.assert_called_once_with(0)
            kernel.SetStdHandle.return_value = 0
            with self.assertRaises(OSError) as raised:
                launcher_control._set_standard_input(0)
            self.assertIs(raised.exception, native_failure)
            error.assert_called_once_with(6)


@unittest.skipUnless(sys.platform == "win32", "requires Windows standard handles and pipe semantics")
class WindowsLauncherPipeTests(unittest.TestCase):
    def test_helper_reads_eof_before_launcher_closes_its_private_pipe(self):
        # Only this isolated process changes standard handles. The outer test
        # owns its launcher pipe and does not close it until the helper replies.
        source = textwrap.dedent(r'''
            import ctypes
            from ctypes import wintypes
            import json
            import msvcrt
            import os
            from pathlib import Path
            import subprocess
            import sys
            import threading
            sys.path.insert(0, sys.argv[1])
            from launcher_control import separate_launcher_stdin

            reader = separate_launcher_stdin()
            assert reader is not None
            assert not os.get_inheritable(reader.fileno())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            standard = kernel.GetStdHandle(0xFFFFFFF6)
            assert standard == msvcrt.get_osfhandle(0)
            assert standard != msvcrt.get_osfhandle(reader.fileno())
            assert os.read(0, 1) == b""

            eof = threading.Event()
            watching = threading.Event()
            def watch():
                watching.set()
                with reader:
                    assert reader.read() == b""
                eof.set()
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            assert watching.wait(1)
            assert not eof.wait(0.1)
            helper = subprocess.run(
                [sys.executable, "-c",
                 "import sys; assert sys.stdin.buffer.read() == b''; print('helper-eof')"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert helper.returncode == 0, (helper.returncode, helper.stderr)
            assert helper.stdout.strip() == "helper-eof", helper.stdout
            assert not eof.is_set(), "launcher EOF arrived before the outer test closed its pipe"
            Path(sys.argv[2]).write_text(json.dumps({"helper": "helper-eof", "launcher_open": True}))
            assert eof.wait(10), "launcher pipe did not signal EOF"
            watcher.join(1)
            assert not watcher.is_alive()
            assert reader.closed
            print("launcher-eof", flush=True)
        ''')
        with tempfile.TemporaryDirectory(prefix="lighttable-launcher-control-") as directory:
            receipt = Path(directory) / "helper-ready.json"
            process = subprocess.Popen(
                [sys.executable, "-c", source, str(ROOT), str(receipt)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, "LIGHTTABLE_WATCH_STDIN": "1"},
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 10
                while not receipt.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.025)
                ready = receipt.exists()
                if ready:
                    self.assertIsNone(process.poll(), "control watcher exited while launcher remained open")
                # Closing before any timeout cleanup also releases a regressed
                # shared-pipe helper so its parent's bounded cleanup can finish.
                process.stdin.close()
                process.stdin = None
                stdout, stderr = process.communicate(timeout=5)
                self.assertTrue(ready, f"Helper failed to finish with launcher open: {stdout} {stderr}")
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(receipt.read_text()),
                                 {"helper": "helper-eof", "launcher_open": True})
                self.assertEqual(stdout.strip(), "launcher-eof")
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                    process.stdin = None
                if process.poll() is None:
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
