# SPDX-License-Identifier: GPL-3.0-only
"""Windows sharing conflicts must not stop the server, and a launcher exit cleans up."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import durable_io
import server


def sharing_conflict(code=32):
    error = PermissionError(13, "The process cannot access the file")
    error.winerror = code
    return error


class RetryWindowsSharingTests(unittest.TestCase):
    def test_conflicts_are_retried_until_the_other_handle_closes(self):
        operation = mock.Mock(side_effect=[sharing_conflict(32), sharing_conflict(5), "done"])
        with mock.patch.object(durable_io.time, "sleep") as sleep:
            self.assertEqual(durable_io.retry_windows_sharing(operation), "done")
        self.assertEqual(operation.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])

    def test_a_conflict_that_outlasts_the_attempts_is_raised(self):
        operation = mock.Mock(side_effect=sharing_conflict(33))
        with mock.patch.object(durable_io.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                durable_io.retry_windows_sharing(operation)
        self.assertEqual(operation.call_count, 6)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 0.31)

    def test_other_errors_are_raised_without_retrying(self):
        operation = mock.Mock(side_effect=PermissionError(13, "denied"))
        with mock.patch.object(durable_io.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                durable_io.retry_windows_sharing(operation)
        operation.assert_called_once()
        sleep.assert_not_called()


class InstanceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "instances" / "8321.json"
        self.patches = [
            mock.patch.object(server, "instance_path", return_value=self.path),
            mock.patch.object(server, "health_payload", return_value={"ok": True, "pid": os.getpid()}),
            mock.patch.object(durable_io.time, "sleep"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.directory.cleanup()

    def test_a_stale_registration_being_read_is_replaced_after_a_retry(self):
        durable_io.atomic_write_json(self.path, {"pid": os.getpid() + 1}, keep_backup=False)
        real_write = durable_io.atomic_write_json
        attempts = []

        def contended(*args, **kwargs):
            attempts.append(args)
            if len(attempts) < 3:
                raise sharing_conflict()
            return real_write(*args, **kwargs)

        with mock.patch.object(durable_io, "atomic_write_json", side_effect=contended):
            self.assertEqual(server.write_instance_file(), self.path)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(durable_io.load_json(self.path, {})["pid"], os.getpid())

    def test_a_persistent_conflict_leaves_the_server_running_unregistered(self):
        with mock.patch.object(durable_io, "atomic_write_json", side_effect=sharing_conflict(5)), \
                mock.patch("builtins.print") as printed:
            self.assertIsNone(server.write_instance_file())
        self.assertIn("could not register this instance", printed.call_args.args[0])
        self.assertFalse(self.path.exists())

    def test_launcher_exit_removes_this_servers_registration(self):
        durable_io.atomic_write_json(self.path, {"pid": os.getpid()}, keep_backup=False)
        with mock.patch.object(server, "SESSION") as session, \
                mock.patch.object(server, "STARTUP"), \
                mock.patch.object(server.os, "_exit") as exit_process:
            server._exit_with_parent("launcher-closed")
        session.end.assert_called_once_with("launcher-closed")
        exit_process.assert_called_once_with(0)
        self.assertFalse(self.path.exists())

    def test_launcher_exit_keeps_another_servers_registration(self):
        durable_io.atomic_write_json(self.path, {"pid": os.getpid() + 1}, keep_backup=False)
        with mock.patch.object(server, "SESSION"), mock.patch.object(server, "STARTUP"), \
                mock.patch.object(server.os, "_exit"):
            server._exit_with_parent()
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
