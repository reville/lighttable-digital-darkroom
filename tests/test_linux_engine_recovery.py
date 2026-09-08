"""A failed transport retries once on CPU; rejected edits keep their worker."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import server


class EngineRecoveryTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX executable worker")
    def test_real_worker_exit_recovers_and_serves_later_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = Path(temporary) / "worker"
            worker.write_text('''#!/usr/bin/env python3
import json, os, sys
if os.environ.get("SPEKTRAFILM_BACKEND") != "cpu":
    sys.exit(23)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"ok": True, "id": request["id"], "backend": "CPU", "pid": os.getpid()}), flush=True)
''')
            worker.chmod(0o700)
            client = server.RustEngineClient(worker)
            try:
                with mock.patch.object(server.sys, "platform", "linux"), \
                     mock.patch.dict(os.environ, {"SPEKTRAFILM_BACKEND": "wgpu"}):
                    first = client.render({"input": "first.tif"})
                    second = client.render({"input": "second.tif"})
                self.assertEqual(first["backend"], "CPU")
                self.assertEqual(first["pid"], second["pid"])
                self.assertGreater(second["id"], first["id"])
            finally:
                client.close()

    def test_linux_transport_failure_retries_on_cpu_and_remembers_it(self):
        client = server.RustEngineClient(Path("worker"))
        failed = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        failed.poll.return_value = None
        healthy = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        healthy.poll.return_value = None
        with mock.patch.object(server.sys, "platform", "linux"), \
             mock.patch.object(server.subprocess, "Popen", side_effect=[failed, healthy]) as start, \
             mock.patch.object(client, "_readline", side_effect=["", '{"ok":true}', '{"ok":true}']):
            result = client.render({"input": "photo.tif"})
            client.render({"input": "next.tif"})
        self.assertEqual(start.call_count, 2)
        self.assertIsNone(start.call_args_list[0].kwargs["env"])
        self.assertEqual(start.call_args_list[1].kwargs["env"]["SPEKTRAFILM_BACKEND"], "cpu")
        self.assertIn("using CPU", result["fallback_reason"])
        failed.kill.assert_called_once()
        healthy.kill.assert_not_called()
        self.assertTrue(client.cpu_fallback)

    def test_request_error_does_not_destroy_cache_or_switch_backend(self):
        client = server.RustEngineClient(Path("worker"))
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        with mock.patch.object(client, "_start", return_value=process), \
             mock.patch.object(client, "close_unlocked") as close, \
             mock.patch.object(client, "_readline", return_value=json.dumps({"ok": False, "error": "missing input"})):
            with self.assertRaisesRegex(RuntimeError, "missing input"):
                client.render({})
        close.assert_not_called()
        self.assertFalse(client.cpu_fallback)

    def test_failed_cpu_retry_is_bounded(self):
        client = server.RustEngineClient(Path("worker"))
        process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO())
        with mock.patch.object(server.sys, "platform", "linux"), \
             mock.patch.object(client, "_start", return_value=process) as start, \
             mock.patch.object(client, "close_unlocked"), \
             mock.patch.object(client, "_readline", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                client.render({})
        self.assertEqual(start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
