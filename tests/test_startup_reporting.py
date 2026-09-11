# SPDX-License-Identifier: GPL-3.0-only
"""Keep the launcher's final startup record readable under Windows file sharing."""
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import durable_io
import recovery


class StartupPublicationTests(unittest.TestCase):
    def test_transient_windows_lock_does_not_lose_ready(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "startup.json"
                reporter = recovery.StartupReporter(path)
                reporter.phase("listening")
                original_write = durable_io.atomic_write_json
                failure = PermissionError("temporary Windows file lock")
                failure.winerror = code
                attempts = 0

                def write(*args, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts < 3:
                        raise failure
                    return original_write(*args, **kwargs)

                with mock.patch.object(durable_io, "atomic_write_json", side_effect=write), \
                        mock.patch.object(recovery.time, "sleep") as sleep:
                    reporter.ready(8321)
                record = json.loads(path.read_text())
                self.assertEqual((record["phase"], record["port"]), ("ready", 8321))
                self.assertEqual(attempts, 3)
                self.assertTrue(sleep.called)

    def test_permanent_failure_stays_bounded_and_keeps_previous_report(self):
        for code in (None, 5, 32):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "startup.json"
                path.write_text('{"phase":"listening"}')
                failure = OSError("write unavailable")
                if code is not None:
                    failure.winerror = code
                with mock.patch.object(durable_io, "atomic_write_json", side_effect=failure) as write, \
                        mock.patch.object(recovery.time, "sleep") as sleep:
                    self.assertFalse(recovery._write_json(path, {"phase": "ready"}))
                self.assertEqual(json.loads(path.read_text())["phase"], "listening")
                if code is None:
                    self.assertEqual(write.call_count, 1)
                    sleep.assert_not_called()
                else:
                    self.assertGreater(write.call_count, 1)
                    self.assertLessEqual(write.call_count, 10)
                    self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.5)

    @unittest.skipUnless(os.name == "nt", "requires real Windows sharing semantics")
    def test_ready_survives_a_native_reader_without_delete_sharing(self):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup.json"
            reporter = recovery.StartupReporter(path)
            reporter.phase("listening")
            # Match a polling reader that permits reads/writes but not deletion.
            handle = kernel.CreateFileW(str(path), 0x80000000, 0x3, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            try:
                with self.assertRaises(OSError) as caught:
                    durable_io.atomic_write_json(path, {"phase": "ready"}, keep_backup=False)
                self.assertIn(caught.exception.winerror, (5, 32, 33))

                def release_reader(_delay):
                    nonlocal handle
                    if handle is not None:
                        self.assertTrue(kernel.CloseHandle(handle))
                        handle = None

                with mock.patch.object(recovery.time, "sleep", side_effect=release_reader):
                    reporter.ready(8321)
                record = json.loads(path.read_text())
                self.assertEqual((record["phase"], record["port"]), ("ready", 8321))
            finally:
                if handle is not None:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
